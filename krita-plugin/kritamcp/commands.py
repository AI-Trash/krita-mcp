"""Validated command execution on Krita's GUI thread.

Generated pixels are written only to detached nodes, never to existing artwork.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import math
import os
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Final, Mapping, TypeAlias, assert_never

from PyQt5.QtCore import QPoint, QRectF
from PyQt5.QtGui import QColor, QImage, QPainter, QPainterPath
from krita import InfoObject, ManagedColor, Selection

from .bridge import CommandError, Json, Result
from .rendering import MAX_CHORD, MAX_PIXELS, apply_selection_bgra, build_path, polygon_patch, sample_stroke

if TYPE_CHECKING:
    from krita import Document, Node, View


class Action(Enum):
    NEW_CANVAS = "new_canvas"
    SET_COLOR = "set_color"
    SET_BRUSH = "set_brush"
    STROKE = "stroke"
    FILL = "fill"
    DRAW_SHAPE = "draw_shape"
    GET_CANVAS = "get_canvas"
    UNDO = "undo"
    REDO = "redo"
    CLEAR = "clear"
    SAVE = "save"
    OPEN_FILE = "open_file"
    GET_COLOR_AT = "get_color_at"
    LIST_BRUSHES = "list_brushes"
    GET_CURRENT_BRUSH = "get_current_brush"
    FILL_POLYGON = "fill_polygon"
    GRADIENT_POLYGON = "gradient_polygon"
    CREATE_LAYER = "create_layer"
    SET_LAYER_PROPERTIES = "set_layer_properties"
    LIST_LAYERS = "list_layers"
    SET_ACTIVE_LAYER = "set_active_layer"
    DELETE_LAYER = "delete_layer"
    MERGE_DOWN = "merge_down"
    SELECT_RECT = "select_rect"
    SELECT_ELLIPSE = "select_ellipse"
    SELECT_POLYGON = "select_polygon"
    SELECT_BY_ALPHA = "select_by_alpha"
    DESELECT = "deselect"
    FILL_SELECTION = "fill_selection"
    BATCH = "batch"


Value: TypeAlias = Json | tuple[tuple[float, ...], ...]
MAX_NATIVE_CALLS: Final = 1024
SRGB_PROFILE: Final = "sRGB-elle-V2-srgbtrc.icc"
BLENDS: Final = ("normal", "multiply", "screen", "add", "overlay")
UNDO_STEPS_NOTE: Final = "undo_steps is an expected maximum based on completed native calls, not observed history; no-op paths and integer rounding can produce fewer undo entries"
DEFAULTS: Final = {
    Action.NEW_CANVAS: dict(width=800, height=600, name="New Canvas", background="#1a1a2e"),
    Action.SET_COLOR: dict(color="#ffffff"),
    Action.SET_BRUSH: dict(preset=None, size=None, opacity=None),
    Action.STROKE: dict(points=None, pressure=1, size=None, color=None, opacity=None,
                        smooth=True, curve=None, taper_start=0, taper_end=0, preserve_alpha=False),
    Action.FILL: dict(x=0, y=0, radius=50),
    Action.DRAW_SHAPE: dict(shape="rectangle", x=0, y=0, width=100, height=100,
                            fill=True, stroke=False, x2=None, y2=None, line_width=2),
    Action.GET_CANVAS: dict(filename="canvas.png", show_grid=False, grid_spacing=100,
                            diff_with_reference=None),
    Action.UNDO: {}, Action.REDO: {}, Action.CLEAR: dict(color="#1a1a2e"),
    Action.SAVE: dict(path=None), Action.OPEN_FILE: dict(path=None),
    Action.GET_COLOR_AT: dict(x=0, y=0), Action.LIST_BRUSHES: dict(filter="", limit=50),
    Action.GET_CURRENT_BRUSH: {},
    Action.FILL_POLYGON: dict(points=None, color=None, smooth=True, feather=0, preserve_alpha=False),
    Action.GRADIENT_POLYGON: dict(points=None, color=None, gradient_end_color=None,
                                 gradient_type="linear", direction=0, smooth=True, feather=0,
                                 preserve_alpha=False),
    Action.CREATE_LAYER: dict(name="Paint Layer", blending_mode="normal"),
    Action.SET_LAYER_PROPERTIES: dict(layer_id=None, name=None, opacity=None, visible=None,
                                     blending_mode=None, preserve_alpha=None),
    Action.LIST_LAYERS: {}, Action.SET_ACTIVE_LAYER: dict(layer_id=None),
    Action.DELETE_LAYER: dict(layer_id=None), Action.MERGE_DOWN: dict(layer_id=None),
    Action.SELECT_RECT: dict(x=None, y=None, width=None, height=None),
    Action.SELECT_ELLIPSE: dict(x=None, y=None, width=None, height=None),
    Action.SELECT_POLYGON: dict(points=None, smooth=False),
    Action.SELECT_BY_ALPHA: dict(threshold=1), Action.DESELECT: {},
    Action.FILL_SELECTION: dict(color=None, preserve_alpha=False),
    Action.BATCH: dict(commands=None),
}


@dataclass(frozen=True, slots=True)
class Command:
    action: Action
    params: Mapping[str, Value]
    children: tuple[Command, ...] = ()
    samples: tuple[tuple[float, float, float], ...] = ()

    @property
    def is_batch(self) -> bool:
        return self.action is Action.BATCH


def number(value, name: str, low: float, high: float) -> float:
    if (type(value) not in (int, float) or not low <= value <= high
            or not math.isfinite(value)):
        raise CommandError(f"{name} must be finite in {low}..{high}")
    return value


def text(value, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or len(value) > 4096 or "\0" in value:
        raise CommandError(f"Invalid {name}")
    return value


def color(value) -> QColor:
    result = QColor(text(value, "color"))
    if not result.isValid():
        raise CommandError("Invalid color")
    return result


def prepare(raw: Json, *, nested: bool = False) -> Command:
    """Parse the entire envelope before any document, layer or brush mutation."""
    if not isinstance(raw, dict) or set(raw) - {"action", "params"}:
        raise CommandError("Expected action/params object")
    try:
        action = Action(raw.get("action"))
    except (ValueError, TypeError):
        raise CommandError(f"Unknown action: {raw.get('action')}") from None
    supplied = raw.get("params", {})
    if not isinstance(supplied, dict) or set(supplied) - DEFAULTS[action].keys():
        raise CommandError("Invalid or unknown parameters")
    params = DEFAULTS[action] | supplied
    if action is Action.BATCH:
        children = params["commands"]
        if nested or not isinstance(children, list) or not children:
            raise CommandError("Batch requires a nonempty command list; nested batch is forbidden")
        return Command(action, MappingProxyType({}), tuple(prepare(child, nested=True) for child in children))
    for key, value in params.items():
        if value is None and DEFAULTS[action][key] is not None:
            raise CommandError(f"{key} must not be null")
    for key in ("smooth", "preserve_alpha", "fill", "stroke", "show_grid", "visible"):
        if key in params and params[key] is not None and type(params[key]) is not bool:
            raise CommandError(f"{key} must be boolean")
        if key in supplied and supplied[key] is None and DEFAULTS[action][key] is not None:
            raise CommandError(f"{key} must be boolean")
    for key in ("color", "background", "gradient_end_color"):
        if key in params and params[key] is not None:
            color(params[key])
    if action is Action.SET_COLOR and params["color"] is None:
        raise CommandError("color is required")
    if action is Action.GRADIENT_POLYGON and (params["color"] is None or params["gradient_end_color"] is None):
        raise CommandError("Gradient requires color and gradient_end_color")
    for key in ("x", "y", "x2", "y2", "direction"):
        if key in params and params[key] is not None:
            number(params[key], key, -1e9, 1e9)
    for key in ("width", "height", "radius", "size", "line_width", "grid_spacing"):
        if key in params and params[key] is not None:
            number(params[key], key, .01, 1_000_000)
    for key in ("opacity", "pressure", "taper_start", "taper_end"):
        if key in params and params[key] is not None:
            number(params[key], key, 0, 1)
    if "feather" in params:
        number(params["feather"], "feather", 0, 256)
    for key in ("name", "preset", "layer_id", "filter"):
        if key in params and params[key] is not None:
            text(params[key], key, empty=key == "filter")
    if params.get("blending_mode") is not None and params["blending_mode"] not in BLENDS:
        raise CommandError("Unsupported blending_mode")
    if "shape" in params and params["shape"] not in ("rectangle", "ellipse", "line"):
        raise CommandError("Unsupported shape")
    if "curve" in params and params["curve"] not in (None, "linear", "bezier"):
        raise CommandError("curve must be linear, bezier or null")
    if "gradient_type" in params and params["gradient_type"] not in ("linear", "radial"):
        raise CommandError("gradient_type must be linear or radial")
    if action in (Action.SAVE, Action.OPEN_FILE):
        path = os.path.abspath(os.path.expanduser(text(params["path"], "path")))
        if not os.path.splitext(path)[1]:
            raise CommandError("File path requires an extension")
        if action is Action.OPEN_FILE and not os.path.isfile(path):
            raise CommandError("File not found")
        if action is Action.SAVE and not os.path.isdir(os.path.dirname(path)):
            raise CommandError("Output directory does not exist")
        params["path"] = path
    if action is Action.GET_CANVAS:
        filename = text(params["filename"], "filename")
        if (len(filename) > 200 or filename.startswith(".") or any(separator in filename for separator in ("/", "\\", ":"))
                or any(ord(character) < 32 for character in filename)
                or os.path.splitext(filename)[1].lower() not in ("", ".png")):
            raise CommandError("filename must be a PNG basename")
        spacing = number(params["grid_spacing"], "grid_spacing", 8, 32768)
        if int(spacing) != spacing:
            raise CommandError("grid_spacing must be an integer")
        if params["diff_with_reference"] is not None:
            reference = os.path.abspath(os.path.expanduser(text(params["diff_with_reference"], "reference path")))
            if not os.path.isfile(reference):
                raise CommandError("Reference file not found")
            params["diff_with_reference"] = reference
    if action in (Action.NEW_CANVAS, Action.SELECT_RECT, Action.SELECT_ELLIPSE):
        for key in (("width", "height") if action is Action.NEW_CANVAS else ("x", "y", "width", "height")):
            if type(params[key]) is not int:
                raise CommandError(f"{key} must be an integer")
        if params["width"] * params["height"] > MAX_PIXELS:
            raise CommandError("Pixel area exceeds 4M budget")
    if action is Action.GET_COLOR_AT and any(type(params[key]) is not int for key in ("x", "y")):
        raise CommandError("Pixel coordinates must be integers")
    if action in (Action.SET_ACTIVE_LAYER, Action.DELETE_LAYER, Action.MERGE_DOWN) and params["layer_id"] is None:
        raise CommandError("layer_id is required")
    if action is Action.LIST_BRUSHES:
        if type(params["limit"]) is not int or not 1 <= params["limit"] <= 10000:
            raise CommandError("limit must be an integer in 1..10000")
    if action is Action.SELECT_BY_ALPHA:
        if type(params["threshold"]) is not int or not 1 <= params["threshold"] <= 255:
            raise CommandError("threshold must be an integer in 1..255")
    samples = ()
    if "points" in params:
        points = params["points"]
        minimum = 2 if action is Action.STROKE else 3
        if not isinstance(points, list) or not minimum <= len(points) <= 4096:
            raise CommandError(f"Expected {minimum}..4096 points")
        frozen_points = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) not in ((2, 3) if action is Action.STROKE else (2,)):
                raise CommandError("Expected coordinate pairs (stroke also accepts pressure triples)")
            frozen_points.append(tuple(number(value, "point", 0 if index == 2 else -1e9,
                                                1 if index == 2 else 1e9) for index, value in enumerate(point)))
        params["points"] = tuple(frozen_points)
        if action is not Action.STROKE and len(set(params["points"])) < 3:
            raise CommandError("Polygon requires three distinct points")
        build_path(params["points"], smooth=params["smooth"], curve=params.get("curve"),
                   closed=action is not Action.STROKE)
        if action is Action.STROKE and (params["pressure"] != 1 or params["taper_start"] or params["taper_end"]
                                        or any(len(point) == 3 and point[2] != 1 for point in params["points"])):
            knots = params["points"][::3] if params["curve"] == "bezier" else params["points"]
            minimum_length = sum(math.dist(first[:2], second[:2]) for first, second in zip(knots, knots[1:]))
            if len(knots) - 1 > MAX_NATIVE_CALLS or minimum_length > MAX_NATIVE_CALLS * MAX_CHORD:
                raise CommandError("Pressure stroke exceeds 1024 native calls")
            samples = tuple(sample_stroke(params["points"], smooth=params["smooth"], curve=params["curve"],
                                           pressure=params["pressure"], taper_start=params["taper_start"],
                                           taper_end=params["taper_end"]))
            if len(samples) - 1 > MAX_NATIVE_CALLS:
                raise CommandError("Pressure stroke exceeds 1024 native calls")
    if (action is Action.GRADIENT_POLYGON or (action is Action.FILL_POLYGON and params["feather"] > 0)) and params["preserve_alpha"]:
        raise CommandError("Generated independent layers do not support preserve_alpha")
    return Command(action, MappingProxyType(params), samples=samples)


@dataclass(frozen=True, slots=True)
class BatchContext:
    document: Document | None
    view: View | None
    layer: Node | None


class BatchExecution:
    """Mutable cursor retaining one validated batch and its completed prefix."""

    def __init__(self, executor: CommandExecutor, command: Command):
        self.executor = executor
        self.children = command.children
        self.results: list[Result] = []
        self.expected = self.context()
        self.check_request: Callable[[], None] = lambda: None

    def context(self) -> BatchContext:
        app = self.executor.app
        document = app.activeDocument()
        window = app.activeWindow()
        return BatchContext(document, window.activeView() if window else None, document.activeNode() if document else None)

    def check(self) -> None:
        self.check_request()
        self.executor.single_view(self.executor.app.activeDocument())
        if self.context() != self.expected:
            raise CommandError("Batch context changed; stopped before next mutation")

    def accept_context(self, document: Document, layer: Node | None, *, view: View | None = None) -> None:
        self.expected = BatchContext(document, self.expected.view if view is None else view, layer)

    def failure(self, result: Result) -> Result:
        return {"error": result["error"], "completed_count": len(self.results), "results": self.results,
                "failing_index": len(self.results), "failing_result": result, "atomic": False,
                "retry_safe": False, "outcome_uncertain": result.get("outcome_uncertain", False)}

    def stop(self) -> Result:
        return self.failure({"error": "Bridge is shutting down; batch stopped before next command",
                             "execution_started": False, "outcome_uncertain": False, "retry_safe": True})

    def step(self, checkpoint: Callable[[], None]) -> Result | None:
        child = self.children[len(self.results)]
        self.check_request = checkpoint
        self.executor.batch_context = self
        try:
            result = self.executor.execute(child, self.check)
        finally:
            self.executor.batch_context = None
        if "error" in result:
            return self.failure(result)
        self.results.append(result)
        if len(self.results) == len(self.children):
            warnings: list[str] = []
            results: list[Result] = []
            for child_result in self.results:
                compact = dict(child_result)
                for key in ("undo_steps_note", "fidelity_warning"):
                    note = compact.get(key)
                    if isinstance(note, str) and note:
                        if note not in warnings:
                            warnings.append(note)
                        del compact[key]
                results.append(compact)
            return {"status": "ok", "completed_count": len(self.results), "results": results,
                    "atomic": False, **({"warnings": warnings} if warnings else {})}
        return None


class CommandExecutor:
    """Krita adapter; execute is called only by the GUI timer, not HTTP workers."""

    def __init__(self, app, output_dir: str):
        self.app = app
        self.output_dir = output_dir
        self.batch_context: BatchExecution | None = None

    def begin(self, command: Command, checkpoint: Callable[[], None]) -> Result | BatchExecution:
        return BatchExecution(self, command) if command.is_batch else self.execute(command, checkpoint)

    def execute(self, command: Command | Json, checkpoint: Callable[[], None] = lambda: None) -> Result:
        execution_started = False
        try:
            prepared = command if isinstance(command, Command) else prepare(command)
            if prepared.action is Action.BATCH:
                batch = BatchExecution(self, prepared)
                while True:
                    result = batch.step(checkpoint)
                    if result is not None:
                        return result
            checkpoint()
            execution_started = True
            return self.dispatch(prepared, checkpoint)
        except Exception as error:
            if not isinstance(error, (CommandError, RuntimeError, OSError, ValueError, TypeError, ImportError)):
                import traceback
                traceback.print_exc()
            return {"error": str(error), "execution_started": execution_started,
                    "outcome_uncertain": execution_started,
                    "retry_safe": False if execution_started else True}

    def single_view(self, document: Document | None) -> None:
        if document is not None and sum(view.document() == document
                                        for window in self.app.windows() for view in window.views()) > 1:
            raise CommandError("Multiple views of the document are ambiguous; close extra views before executing")

    def check_focus(self, document: Document, view: View, allowed: tuple[Node | None, ...]) -> None:
        window = self.app.activeWindow()
        if (self.app.activeDocument() != document
                or (window.activeView() if window else None) != view):
            raise CommandError("Document/view changed; stopped without changing focus")
        self.single_view(document)
        if document.activeNode() not in allowed:
            raise CommandError("Active layer changed externally; stopped without changing focus")

    def document(self):
        document = self.app.activeDocument()
        if document is None:
            raise CommandError("No active document")
        document.waitForDone()
        self.single_view(document)
        if self.batch_context is not None:
            self.batch_context.check()
        return document

    def view(self):
        if self.batch_context is not None:
            self.batch_context.check()
        window = self.app.activeWindow()
        view = window.activeView() if window else None
        if view is None:
            raise CommandError("No active view")
        return view

    def layer(self, document, layer_id=None):
        def find(node):
            if str(node.uniqueId()) == layer_id:
                return node
            for child in node.childNodes():
                found = find(child)
                if found is not None:
                    return found
            return None
        node = find(document.rootNode()) if layer_id is not None else document.activeNode()
        if node is None:
            raise CommandError("Layer not found")
        return node

    def paint_layer(self, document):
        node = self.layer(document)
        if node.type() != "paintlayer" or node.locked() or not node.visible():
            raise CommandError("Active layer must be an unlocked visible paint layer")
        return node

    def rgba_u8(self, document, node=None):
        source = node if node is not None else document
        if source.colorModel() != "RGBA" or source.colorDepth() != "U8":
            raise CommandError("Raw BGRA operations require RGBA/U8")

    def srgb_components(self, source: ManagedColor) -> tuple[int, int, int, int]:
        model, depth, profile = source.colorModel(), source.colorDepth(), source.colorProfile()
        if (SRGB_PROFILE not in self.app.profiles("RGBA", "U8")
                or profile not in self.app.profiles(model, depth)):
            raise CommandError("Source or standard sRGB color profile is unavailable")
        converted = ManagedColor(model, depth, profile)
        converted.setComponents(source.components())
        if not converted.setColorSpace("RGBA", "U8", SRGB_PROFILE):
            raise CommandError("Color conversion to standard sRGB failed")
        red, green, blue, alpha = (max(0, min(255, round(value * 255)))
                                   for value in converted.componentsOrdered())
        return red, green, blue, alpha

    @contextmanager
    def brush(self, document, node, params):
        view = self.view()
        foreground, size, opacity, alpha = view.foregroundColor(), view.brushSize(), view.paintingOpacity(), node.alphaLocked()
        try:
            if params.get("color") is not None:
                view.setForeGroundColor(ManagedColor.fromQColor(color(params["color"]), None))
            if params.get("size") is not None:
                view.setBrushSize(params["size"])
            if params.get("opacity") is not None:
                view.setPaintingOpacity(params["opacity"])
            if params.get("preserve_alpha"):
                node.setAlphaLocked(True)
            yield
        finally:
            try:
                document.waitForDone()
            finally:
                try:
                    view.setForeGroundColor(foreground)
                finally:
                    try:
                        view.setBrushSize(size)
                    finally:
                        try:
                            view.setPaintingOpacity(opacity)
                        finally:
                            node.setAlphaLocked(alpha)

    def native_path(self, document, node, path, params, *, filled=False, outline=True):
        with self.brush(document, node, params):
            node.paintPath(path, "ForegroundColor" if outline else "None", "ForegroundColor" if filled else "None")
            document.waitForDone()
        return {"status": "ok", "engine": "native", "mode": "path", "native_calls": 1,
                "undo_steps": 1, "undo_steps_verified": False, "undo_steps_note": UNDO_STEPS_NOTE,
                "fidelity_warning": None}

    def generated(self, document, params):
        view = self.view()
        target = self.layer(document)
        parent = target.parentNode()
        if parent is None or parent.locked() or not parent.visible():
            raise CommandError("Target requires an unlocked visible parent")
        self.rgba_u8(document)
        profile = document.colorProfile()
        profiles = self.app.profiles("RGBA", "U8")
        if SRGB_PROFILE not in profiles or profile not in profiles:
            raise CommandError("Document or standard sRGB color profile is unavailable")
        rgba = color(params["color"]).getRgb() if params["color"] is not None else self.srgb_components(view.foregroundColor())
        end = color(params["gradient_end_color"]).getRgb() if params.get("gradient_end_color") else None
        patch = polygon_patch(params["points"], document.width(), document.height(), color=rgba,
                              smooth=params["smooth"], feather=params["feather"], gradient_end_color=end,
                              gradient_type=params.get("gradient_type", "linear"), direction=params.get("direction", 0))
        if patch is None:
            return {"status": "ok", "mode": "generated_layer", "layer_id": None, "changed": False}
        x, y, width, height, pixels = patch
        selected = document.selection()
        if selected is not None:
            pixels = apply_selection_bgra(pixels, selected.pixelData(x, y, width, height))
        layer = document.createNode("Generated Polygon", "paintlayer")
        self.rgba_u8(document, layer)
        if not layer.setColorSpace("RGBA", "U8", SRGB_PROFILE):
            raise CommandError("Detached generated layer conversion to standard sRGB failed")
        if not layer.setPixelData(pixels, x, y, width, height):
            raise CommandError("Detached generated pixel write failed")
        if not layer.setColorSpace("RGBA", "U8", profile):
            raise CommandError("Detached generated layer conversion to document profile failed")
        if not parent.addChildNode(layer, target):
            raise CommandError("Generated layer attachment failed")
        document.refreshProjection()
        document.waitForDone()
        try:
            self.check_focus(document, view, (target, layer))
            if document.activeNode() != layer:
                document.setActiveNode(layer)
            if self.batch_context is not None:
                self.batch_context.accept_context(document, layer)
        except CommandError as error:
            return {"error": f"Generated layer attached, but execution stopped: {error}",
                    "layer_id": str(layer.uniqueId()), "target_layer_id": str(target.uniqueId()),
                    "independent_layer": True, "execution_started": True,
                    "outcome_uncertain": True, "retry_safe": False}
        return {"status": "ok", "mode": "generated_layer", "layer_id": str(layer.uniqueId()),
                "target_layer_id": str(target.uniqueId()), "independent_layer": True,
                "history": "native layer attachment; target pixels and identity unchanged"}

    def select_path(self, document, path):
        box = path.boundingRect().toAlignedRect()
        box = box.intersected(QRectF(0, 0, document.width(), document.height()).toAlignedRect())
        x, y, width, height = box.x(), box.y(), box.width(), box.height()
        selected = Selection()
        if width > 0 and height > 0:
            if width * height > MAX_PIXELS:
                raise CommandError("Selection exceeds 4M pixel budget")
            image = QImage(width, height, QImage.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            try:
                from PyQt5.QtCore import Qt
                painter.setRenderHint(QPainter.Antialiasing)
                painter.translate(-x, -y)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(255, 255, 255))
                painter.drawPath(path)
            finally:
                painter.end()
            selected.setPixelData(image.constBits().asstring(image.sizeInBytes())[3::4], x, y, width, height)
        document.setSelection(selected)
        return {"status": "ok", "bbox": [x, y, width, height]}

    def layer_info(self, node) -> Result:
        return {"layer_id": str(node.uniqueId()), "name": node.name(), "type": node.type(),
                "opacity": node.opacity() / 255, "visible": node.visible(), "blending_mode": node.blendingMode(),
                "preserve_alpha": node.alphaLocked() if node.type() == "paintlayer" else None,
                 "parent_id": str(node.parentNode().uniqueId()) if node.parentNode() else None}

    def layer_operation(self, command: Command) -> Result:
        try:
            document = self.document()
            view = self.view()
            node = self.layer(document, command.params["layer_id"])
            parent = node.parentNode()
            if parent is None or node == document.rootNode():
                raise CommandError("Root or detached nodes cannot be removed or merged")
            if node.locked() or parent.locked():
                raise CommandError("Layer and parent must be unlocked")
            before = parent.childNodes()
            if node not in before:
                raise CommandError("Layer is not attached")
            index = before.index(node)
            lower = before[index - 1] if index else None
            if command.action is Action.MERGE_DOWN and (lower is None or lower.locked()):
                raise CommandError("Merge requires an unlocked lower sibling")
            active = document.activeNode()
            self.check_focus(document, view, (active,))
        except CommandError as error:
            return {"error": str(error), "execution_started": False,
                    "outcome_uncertain": False, "retry_safe": True}
        layer_id = str(node.uniqueId())
        try:
            match command.action:
                case Action.DELETE_LAYER:
                    cursor = active
                    while cursor is not None and cursor != node:
                        cursor = cursor.parentNode()
                    loses_focus = cursor == node
                    survivor = active
                    upper = before[index + 1] if index + 1 < len(before) else None
                    if loses_focus:
                        survivor = lower or upper or (parent if parent != document.rootNode() else None)
                    removed = node.remove()
                    document.waitForDone()
                    if not removed:
                        raise CommandError("Native layer removal failed; outcome unverified")
                    self.check_focus(document, view, (active, lower, upper, survivor, None,
                                                     document.rootNode() if survivor is None else survivor)
                                     if loses_focus else (active,))
                    current = document.activeNode()
                    if loses_focus and current is not None and current in (lower, upper, survivor):
                        survivor = current
                    if survivor is not None:
                        survivor = self.layer(document, str(survivor.uniqueId()))
                    if survivor is not None and current != survivor:
                        document.setActiveNode(survivor)
                    if self.batch_context is not None:
                        self.batch_context.accept_context(document, survivor if survivor is not None else current)
                    return {"status": "ok", "deleted_layer_id": layer_id,
                            "active_layer_id": str(survivor.uniqueId()) if survivor else None}
                case Action.MERGE_DOWN:
                    document.setActiveNode(node)
                    if self.batch_context is not None:
                        self.batch_context.accept_context(document, node)
                    returned = node.mergeDown()
                    document.waitForDone()
                    after = parent.childNodes()
                    if (len(after) != len(before) - 1 or node in after
                            or after[:index - 1] != before[:index - 1]
                            or after[index:] != before[index + 1:]):
                        raise CommandError("Merge outcome unverified; sibling replacement was not confirmed")
                    merged = after[index - 1]
                    if merged in before and merged != lower:
                        raise CommandError("Merge outcome ambiguous; expected lower sibling was not replaced")
                    if returned is not None and returned in after:
                        if returned != merged:
                            raise CommandError("Merge returned an unexpected attached layer; outcome ambiguous")
                        merged = after[after.index(returned)]
                    self.check_focus(document, view, (node, lower, merged, None))
                    document.setActiveNode(merged)
                    if self.batch_context is not None:
                        self.batch_context.accept_context(document, merged)
                    return {"status": "ok", **self.layer_info(merged), "merged_layer_id": layer_id,
                            "active_layer_id": str(merged.uniqueId())}
                case unreachable:
                    assert_never(unreachable)
        except (CommandError, RuntimeError, ValueError, TypeError) as error:
            return {"error": str(error), "layer_id": layer_id, "execution_started": True,
                    "outcome_uncertain": True, "retry_safe": False}

    def dispatch(self, command: Command, checkpoint: Callable[[], None]) -> Result:
        params = command.params
        match command.action:
            case Action.BATCH:
                raise CommandError("Batch must use execute")
            case Action.NEW_CANVAS:
                window = self.app.activeWindow()
                if window is None:
                    raise CommandError("No active window")
                if SRGB_PROFILE not in self.app.profiles("RGBA", "U8"):
                    raise CommandError("Standard sRGB color profile is unavailable")
                document = self.app.createDocument(params["width"], params["height"], params["name"], "RGBA", "U8", SRGB_PROFILE, 120.0)
                if document is None:
                    raise CommandError("Could not create document")
                layer = document.createNode("paint", "paintlayer")
                red, green, blue, alpha = color(params["background"]).getRgb()
                if not layer.setPixelData(bytes((blue, green, red, alpha)) * (params["width"] * params["height"]), 0, 0, params["width"], params["height"]):
                    raise CommandError("Detached background write failed")
                if not document.rootNode().addChildNode(layer, None):
                    raise CommandError("Background attachment failed")
                document.setActiveNode(layer)
                view = window.addView(document)
                if self.batch_context is not None:
                    self.batch_context.accept_context(document, layer, view=view)
                document.refreshProjection()
                document.waitForDone()
                return {"status": "ok", "width": document.width(), "height": document.height(), "name": document.name()}
            case Action.SET_COLOR:
                view = self.view()
                view.setForeGroundColor(ManagedColor.fromQColor(color(params["color"]), None))
                return {"status": "ok", "color": params["color"]}
            case Action.SET_BRUSH:
                view = self.view()
                preset = None
                if params["preset"] is not None:
                    presets = self.app.resources("preset")
                    preset = presets.get(params["preset"]) or next((resource for name, resource in presets.items() if params["preset"].lower() in name.lower()), None)
                    if preset is None:
                        raise CommandError("Brush preset not found")
                    view.setCurrentBrushPreset(preset)
                if params["size"] is not None:
                    view.setBrushSize(params["size"])
                if params["opacity"] is not None:
                    view.setPaintingOpacity(params["opacity"])
                return {"status": "ok", "preset": params["preset"], "size": view.brushSize(), "opacity": view.paintingOpacity()}
            case Action.GET_CURRENT_BRUSH:
                view = self.view()
                preset = view.currentBrushPreset()
                return {"status": "ok", "preset": preset.name() if preset else None, "size": view.brushSize(), "opacity": view.paintingOpacity()}
            case Action.LIST_BRUSHES:
                brushes = [name for name in self.app.resources("preset") if params["filter"].lower() in name.lower()][:params["limit"]]
                return {"status": "ok", "brushes": brushes, "count": len(brushes)}
            case Action.OPEN_FILE:
                window = self.app.activeWindow()
                if window is None:
                    raise CommandError("No active window")
                document = self.app.openDocument(params["path"])
                if document is None:
                    raise CommandError("Failed to open file")
                view = window.addView(document)
                if self.batch_context is not None:
                    self.batch_context.accept_context(document, document.activeNode(), view=view)
                document.waitForDone()
                return {"status": "ok", "path": params["path"], "name": document.name(), "width": document.width(), "height": document.height()}
            case Action.UNDO | Action.REDO:
                document = self.document()
                action = self.app.action("edit_" + command.action.value)
                if action is None or not action.isEnabled():
                    raise CommandError("History action unavailable")
                action.trigger()
                document.waitForDone()
                if self.batch_context is not None:
                    self.batch_context.accept_context(document, document.activeNode())
                return {"status": "ok", "requested": command.action.value, "history_verified": False,
                        "warning": "Native history request issued; enabled QAction does not prove a history change"}
            case Action.SAVE:
                document = self.document()
                batchmode = document.batchmode()
                kra = params["path"].lower().endswith(".kra")
                original_filename = document.fileName() if kra else None
                completed = False
                try:
                    document.setBatchmode(True)
                    saved = document.saveAs(params["path"]) if kra else document.exportImage(params["path"], InfoObject())
                    document.waitForDone()
                    if not saved or not os.path.isfile(params["path"]):
                        raise CommandError("Save/export failed")
                    completed = True
                finally:
                    try:
                        if kra and not completed:
                            document.setFileName(original_filename)
                    finally:
                        document.setBatchmode(batchmode)
                return {"status": "ok", "path": params["path"]}
            case Action.GET_CANVAS:
                from .preview import export_preview
                result = export_preview(self.document(), params["filename"], output_dir=self.output_dir,
                                        show_grid=params["show_grid"], grid_spacing=params["grid_spacing"],
                                        diff_with_reference=params["diff_with_reference"])
                return {"status": "ok", **result}
            case Action.GET_COLOR_AT:
                document = self.document()
                self.rgba_u8(document)
                if not 0 <= params["x"] < document.width() or not 0 <= params["y"] < document.height():
                    raise CommandError("Pixel is outside canvas")
                pixel = bytes(document.rootNode().projectionPixelData(params["x"], params["y"], 1, 1))
                if len(pixel) != 4:
                    raise CommandError("Could not read pixel")
                profile = document.colorProfile()
                if profile not in self.app.profiles("RGBA", "U8"):
                    raise CommandError("Document color profile is unavailable")
                sampled = ManagedColor("RGBA", "U8", profile)
                sampled.setComponents([value / 255 for value in pixel])
                red, green, blue, _converted_alpha = self.srgb_components(sampled)
                alpha = pixel[3]
                return {"status": "ok", "color": f"#{red:02x}{green:02x}{blue:02x}", "r": red, "g": green, "b": blue, "a": alpha}
            case Action.STROKE:
                document = self.document()
                node = self.paint_layer(document)
                if not command.samples:
                    return self.native_path(document, node, build_path(params["points"], smooth=params["smooth"], curve=params["curve"]), params)
                calls = 0
                view = self.view()
                try:
                    with self.brush(document, node, params):
                        for first, second in zip(command.samples, command.samples[1:]):
                            checkpoint()
                            self.check_focus(document, view, (node,))
                            node.paintLine(QPoint(round(first[0]), round(first[1])), QPoint(round(second[0]), round(second[1])), first[2], second[2], "ForegroundColor")
                            calls += 1
                            if calls % 32 == 0:
                                document.waitForDone()
                                self.check_focus(document, view, (node,))
                    self.check_focus(document, view, (node,))
                except (CommandError, RuntimeError, ValueError, TypeError) as error:
                    return {"error": str(error), "engine": "native", "mode": "pressure_segments",
                            "native_calls": calls, "undo_steps": calls, "undo_steps_verified": False,
                            "undo_steps_note": UNDO_STEPS_NOTE, "outcome_uncertain": True,
                            "retry_safe": False, "fidelity_warning": "Earlier segments may remain; failing native call outcome is unverified"}
                return {"status": "ok", "engine": "native", "mode": "pressure_segments", "native_calls": calls,
                        "undo_steps": calls, "undo_steps_verified": False, "undo_steps_note": UNDO_STEPS_NOTE,
                        "fidelity_warning": "Separate native strokes with integer coordinates; not continuous-smudge parity"}
            case Action.FILL_POLYGON | Action.GRADIENT_POLYGON:
                document = self.document()
                if command.action is Action.GRADIENT_POLYGON or params["feather"] > 0:
                    return self.generated(document, params)
                return self.native_path(document, self.paint_layer(document), build_path(params["points"], smooth=params["smooth"], closed=True), params, filled=True, outline=False)
            case Action.FILL | Action.DRAW_SHAPE | Action.CLEAR | Action.FILL_SELECTION:
                document = self.document()
                node = self.paint_layer(document)
                path = QPainterPath()
                match command.action:
                    case Action.FILL:
                        path.addEllipse(QRectF(params["x"] - params["radius"], params["y"] - params["radius"], params["radius"] * 2, params["radius"] * 2))
                        return self.native_path(document, node, path, params, filled=True, outline=False)
                    case Action.DRAW_SHAPE:
                        match params["shape"]:
                            case "rectangle":
                                path.addRect(QRectF(params["x"], params["y"], params["width"], params["height"]))
                            case "ellipse":
                                path.addEllipse(QRectF(params["x"], params["y"], params["width"], params["height"]))
                            case "line":
                                path.moveTo(params["x"], params["y"])
                                path.lineTo(params["x2"] if params["x2"] is not None else params["x"] + params["width"], params["y2"] if params["y2"] is not None else params["y"] + params["height"])
                        filled = params["shape"] != "line" and params["fill"]
                        outline = params["shape"] == "line" or params["stroke"] or not filled
                        return self.native_path(document, node, path, {**params, "size": params["line_width"]}, filled=filled, outline=outline)
                    case Action.CLEAR | Action.FILL_SELECTION:
                        if command.action is Action.FILL_SELECTION and document.selection() is None:
                            raise CommandError("No active selection")
                        path.addRect(QRectF(0, 0, document.width(), document.height()))
                        result = self.native_path(document, node, path, params, filled=True, outline=False)
                        result["semantics"] = "Native foreground fill respecting active selection, opacity and alpha lock; not a raw pixel replacement"
                        return result
                    case unreachable:
                        assert_never(unreachable)
            case Action.CREATE_LAYER:
                document = self.document()
                target = document.activeNode()
                parent = target.parentNode() if target and target.parentNode() else document.rootNode()
                layer = document.createNode(params["name"], "paintlayer")
                layer.setBlendingMode(params["blending_mode"])
                if not parent.addChildNode(layer, target if target and target.parentNode() else None):
                    raise CommandError("Layer attachment failed")
                document.setActiveNode(layer)
                if self.batch_context is not None:
                    self.batch_context.accept_context(document, layer)
                document.waitForDone()
                return {"status": "ok", **self.layer_info(layer)}
            case Action.SET_LAYER_PROPERTIES:
                document = self.document()
                node = self.layer(document, params["layer_id"])
                if params["preserve_alpha"] is not None and node.type() != "paintlayer":
                    raise CommandError("Alpha lock requires paint layer")
                if params["name"] is not None:
                    node.setName(params["name"])
                if params["opacity"] is not None:
                    node.setOpacity(round(params["opacity"] * 255))
                if params["visible"] is not None:
                    node.setVisible(params["visible"])
                if params["blending_mode"] is not None:
                    node.setBlendingMode(params["blending_mode"])
                if params["preserve_alpha"] is not None:
                    node.setAlphaLocked(params["preserve_alpha"])
                document.refreshProjection()
                document.waitForDone()
                return {"status": "ok", **self.layer_info(node)}
            case Action.DELETE_LAYER | Action.MERGE_DOWN:
                return self.layer_operation(command)
            case Action.LIST_LAYERS:
                document = self.document()
                layers = []
                def visit(parent):
                    for child in parent.childNodes():
                        layers.append(self.layer_info(child))
                        visit(child)
                visit(document.rootNode())
                return {"status": "ok", "layers": layers, "active_layer_id": str(document.activeNode().uniqueId()) if document.activeNode() else None}
            case Action.SET_ACTIVE_LAYER:
                document = self.document()
                node = self.layer(document, params["layer_id"])
                document.setActiveNode(node)
                if self.batch_context is not None:
                    self.batch_context.accept_context(document, node)
                return {"status": "ok", "layer_id": str(node.uniqueId())}
            case Action.SELECT_RECT | Action.SELECT_ELLIPSE | Action.SELECT_POLYGON:
                document = self.document()
                path = QPainterPath()
                match command.action:
                    case Action.SELECT_RECT:
                        path.addRect(QRectF(params["x"], params["y"], params["width"], params["height"]))
                    case Action.SELECT_ELLIPSE:
                        path.addEllipse(QRectF(params["x"], params["y"], params["width"], params["height"]))
                    case Action.SELECT_POLYGON:
                        path = build_path(params["points"], smooth=params["smooth"], closed=True)
                    case unreachable:
                        assert_never(unreachable)
                return self.select_path(document, path)
            case Action.SELECT_BY_ALPHA:
                document = self.document()
                node = self.layer(document)
                self.rgba_u8(document, node)
                box = node.bounds().intersected(QRectF(0, 0, document.width(), document.height()).toAlignedRect())
                width, height = box.width(), box.height()
                if width * height > MAX_PIXELS:
                    raise CommandError("Alpha selection exceeds 4M pixels")
                selected = Selection()
                if width > 0 and height > 0:
                    pixels = bytes(node.pixelData(box.x(), box.y(), width, height))
                    if len(pixels) != width * height * 4:
                        raise CommandError("Incomplete alpha pixels")
                    mask = bytes(255 if alpha >= params["threshold"] else 0 for alpha in pixels[3::4])
                    selected.setPixelData(mask, box.x(), box.y(), width, height)
                document.setSelection(selected)
                return {"status": "ok", "bbox": [box.x(), box.y(), width, height]}
            case Action.DESELECT:
                self.document().setSelection(None)
                return {"status": "ok"}
            case unreachable:
                assert_never(unreachable)
