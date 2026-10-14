from __future__ import annotations

from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor
import importlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt5.QtCore import QByteArray, QPoint, QRect
from PyQt5.QtGui import QColor

STANDARD_SRGB = "sRGB-elle-V2-srgbtrc.icc"
LINEAR_PROFILE = "linear-test.icc"


class ManagedColorDouble:
    def __init__(self, model, depth, profile):
        self.model, self.depth, self.profile = model, depth, profile
        self.values = [0, 0, 0, 0]
        self.conversions = []

    def colorModel(self):
        return self.model

    def colorDepth(self):
        return self.depth

    def colorProfile(self):
        return self.profile

    def components(self):
        return list(self.values)

    def setComponents(self, values):
        self.values = list(values)

    def componentsOrdered(self):
        if self.model == "RGBA" and self.depth == "U8":
            blue, green, red, alpha = self.values
            return [red, green, blue, alpha]
        return list(self.values)

    def setColorSpace(self, model, depth, profile):
        if self.model == "CMYKA":
            cyan, magenta, yellow, black, alpha = self.values
            red, green, blue = ((1 - value) * (1 - black) for value in (cyan, magenta, yellow))
        else:
            red, green, blue, alpha = self.componentsOrdered()
        self.conversions.append((self.model, self.depth, self.profile, model, depth, profile))
        if self.profile == LINEAR_PROFILE and profile == STANDARD_SRGB:
            red, green, blue = (value ** .5 for value in (red, green, blue))
        elif self.profile == STANDARD_SRGB and profile == LINEAR_PROFILE:
            red, green, blue = (value ** 2 for value in (red, green, blue))
        self.values = [blue, green, red, alpha] if model == "RGBA" and depth == "U8" else [red, green, blue, alpha]
        self.model, self.depth, self.profile = model, depth, profile
        return True

    def colorForCanvas(self, canvas):
        return QColor.fromRgbF(*self.componentsOrdered())

    @staticmethod
    def fromQColor(color, canvas):
        if canvas is not None:
            red, green, blue, alpha = color.getRgb()
            return QColor(round(red ** 2 / 255), round(green ** 2 / 255), round(blue ** 2 / 255), alpha)
        return QColor(color)


class SelectionDouble:
    def __init__(self):
        self.region = (0, 0, 0, 0)
        self.data = b""
        self.reads = []

    def setPixelData(self, data, x, y, width, height):
        self.region = (x, y, width, height)
        self.data = bytes(data)

    def pixelData(self, x, y, width, height):
        self.reads.append((x, y, width, height))
        left, top, columns, rows = self.region
        return QByteArray(bytes(self.data[(vertical - top) * columns + horizontal - left]
                               if left <= horizontal < left + columns and top <= vertical < top + rows else 0
                               for vertical in range(y, y + height) for horizontal in range(x, x + width)))


PACKAGE = "kritamcp_command_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1] / "krita-plugin" / "kritamcp")]
sys.modules[PACKAGE] = package
krita_double = ModuleType("krita")
krita_double.InfoObject = lambda: None
krita_double.Selection = SelectionDouble
krita_double.ManagedColor = ManagedColorDouble
original_krita = sys.modules.get("krita")
sys.modules["krita"] = krita_double
try:
    commands = importlib.import_module(PACKAGE + ".commands")
finally:
    if original_krita is None:
        sys.modules.pop("krita")
    else:
        sys.modules["krita"] = original_krita
bridge = importlib.import_module(PACKAGE + ".bridge")


class ViewDouble:
    def __init__(self, document=None):
        self.document_ref = document
        self.foreground = QColor("blue")
        self.size = 12
        self.opacity = .6
        self.preset = SimpleNamespace(name=lambda: "Basic")

    def foregroundColor(self):
        snapshot = ManagedColorDouble("RGBA", "U8", STANDARD_SRGB)
        red, green, blue, alpha = self.foreground.getRgbF()
        snapshot.setComponents([blue, green, red, alpha])
        return snapshot

    def setForeGroundColor(self, color):
        self.foreground = QColor(color.colorForCanvas(None)) if hasattr(color, "colorForCanvas") else QColor(color)

    def brushSize(self):
        return self.size

    def setBrushSize(self, value):
        self.size = value

    def paintingOpacity(self):
        return self.opacity

    def setPaintingOpacity(self, value):
        self.opacity = value

    def currentBrushPreset(self):
        return self.preset

    def setCurrentBrushPreset(self, preset):
        self.preset = preset

    def canvas(self):
        return None

    def document(self):
        return self.document_ref


class NodeDouble:
    def __init__(self, document, name, uid, kind="paintlayer"):
        self.document = document
        self.label, self.uid, self.kind = name, uid, kind
        self.parent = None
        self.children = []
        self.alpha = False
        self.shown = True
        self.lock = False
        self.blend = "normal"
        self.opacity_value = 255
        self.calls = []
        self.fail_paint = False
        self.region = (0, 0, 0, 0)
        self.pixels = b""
        self.depth = "U8"
        self.model = "RGBA"
        self.profile = document.colorProfile()
        self.write_profiles = []

    def uniqueId(self):
        return self.uid

    def type(self):
        return self.kind

    def name(self):
        return self.label

    def setName(self, name):
        self.label = name

    def parentNode(self):
        return self.parent

    def childNodes(self):
        return list(self.children)

    def addChildNode(self, node, above):
        if node.parent is not None:
            raise AssertionError("Test attachment must use a detached node")
        index = self.children.index(above) + 1 if above else len(self.children)
        self.children.insert(index, node)
        node.parent = self
        self.document.events.append(("attach", node.uid, above.uid if above else None))
        if self.document.auto_activate:
            self.document.pending_activation = node
        return True

    def setPixelData(self, pixels, x, y, width, height):
        if self.parent is not None:
            raise AssertionError("Attached raw write corrupts native history")
        self.region, self.pixels = (x, y, width, height), bytes(pixels)
        self.write_profiles.append(self.profile)
        self.document.events.append(("detached_write", self.uid))
        return True

    def pixelData(self, x, y, width, height):
        left, top, columns, rows = self.region
        data = bytearray()
        for vertical in range(y, y + height):
            for horizontal in range(x, x + width):
                index = ((vertical - top) * columns + horizontal - left) * 4
                data.extend(self.pixels[index:index + 4] if left <= horizontal < left + columns and top <= vertical < top + rows else b"\0" * 4)
        return QByteArray(bytes(data))

    def projectionPixelData(self, *args):
        return self.pixelData(*args)

    def bounds(self):
        return QRect(*self.region)

    def colorModel(self):
        return self.model

    def colorDepth(self):
        return self.depth

    def colorProfile(self):
        return self.profile

    def setColorSpace(self, model, depth, profile):
        if self.parent is not None:
            raise AssertionError("ICC conversion must remain detached")
        self.document.events.append(("convert", self.uid, self.profile, profile, bool(self.pixels)))
        if profile in self.document.app.node_conversion_failures:
            return False
        converted = bytearray()
        for offset in range(0, len(self.pixels), 4):
            managed = ManagedColorDouble(self.model, self.depth, self.profile)
            managed.setComponents(value / 255 for value in self.pixels[offset:offset + 4])
            managed.setColorSpace(model, depth, profile)
            converted.extend(max(0, min(255, round(value * 255))) for value in managed.components())
        self.pixels = bytes(converted)
        self.model, self.depth, self.profile = model, depth, profile
        return True

    def alphaLocked(self):
        return self.alpha

    def setAlphaLocked(self, value):
        self.alpha = value

    def visible(self):
        return self.shown

    def setVisible(self, value):
        self.shown = value

    def locked(self):
        return self.lock

    def setBlendingMode(self, value):
        self.blend = value

    def blendingMode(self):
        return self.blend

    def setOpacity(self, value):
        self.opacity_value = value

    def opacity(self):
        return self.opacity_value

    def paintPath(self, path, stroke, fill):
        self.calls.append(("path", path, stroke, fill, self.alpha, self.document.app.view.size,
                           self.document.app.view.opacity, self.document.app.view.foreground.getRgb()))
        if self.fail_paint:
            raise RuntimeError("Native paint failed")

    def paintLine(self, first, second, start_pressure, end_pressure, stroke):
        self.calls.append(("line", first, second, start_pressure, end_pressure, stroke))
        if self.fail_paint:
            raise RuntimeError("Native paint failed")


class DocumentDouble:
    def __init__(self, app, width=160, height=100):
        self.app = app
        self.columns, self.rows = width, height
        self.profile = STANDARD_SRGB
        self.events = []
        self.nodes = []
        self.root = NodeDouble(self, "root", "root", "grouplayer")
        self.target = NodeDouble(self, "target", "target")
        self.target.parent = self.root
        self.root.children.append(self.target)
        self.active = self.target
        self.auto_activate = True
        self.pending_activation = None
        self.selected = None
        self.waits = 0
        self.batch = False
        self.export_success = True
        self.filename = "original.kra"
        self.model, self.depth = "RGBA", "U8"

    def width(self):
        return self.columns

    def height(self):
        return self.rows

    def name(self):
        return "Document"

    def colorModel(self):
        return self.model

    def colorDepth(self):
        return self.depth

    def colorProfile(self):
        return self.profile

    def activeNode(self):
        return self.active

    def setActiveNode(self, node):
        self.active = node

    def rootNode(self):
        return self.root

    def createNode(self, name, kind):
        node = NodeDouble(self, name, f"new-{len(self.nodes)}", kind)
        self.nodes.append(node)
        return node

    def waitForDone(self):
        self.waits += 1
        if self.pending_activation is not None:
            self.active = self.pending_activation
            self.pending_activation = None

    def refreshProjection(self):
        self.events.append(("refresh",))

    def selection(self):
        return self.selected

    def setSelection(self, selected):
        self.selected = selected

    def batchmode(self):
        return self.batch

    def setBatchmode(self, value):
        self.batch = value

    def exportImage(self, filename, info):
        if self.export_success:
            Path(filename).write_bytes(b"fake export")
        return self.export_success

    def saveAs(self, filename):
        self.filename = filename
        return self.exportImage(filename, None)

    def fileName(self):
        return self.filename

    def setFileName(self, filename):
        self.filename = filename


class AppDouble:
    def __init__(self):
        self.view = ViewDouble()
        self.document = DocumentDouble(self)
        self.view.document_ref = self.document
        self.extra_views = []
        self.other_windows = []
        self.triggers = []
        self.profile_names = [STANDARD_SRGB, LINEAR_PROFILE]
        self.node_conversion_failures = set()

    def activeDocument(self):
        return self.document

    def activeWindow(self):
        return SimpleNamespace(activeView=lambda: self.view, addView=self.add_view,
                               views=lambda: [self.view, *self.extra_views])

    def windows(self):
        return [self.activeWindow(), *self.other_windows]

    def add_view(self, document):
        self.document = document
        self.view.document_ref = document
        return self.view

    def createDocument(self, width, height, *args):
        document = DocumentDouble(self, width, height)
        document.profile = args[3] or LINEAR_PROFILE
        return document

    def profiles(self, model, depth):
        return list(self.profile_names)

    def resources(self, kind):
        return {"Basic": self.view.preset}

    def action(self, name):
        return SimpleNamespace(isEnabled=lambda: True, trigger=lambda: self.triggers.append(name))

    def openDocument(self, filename):
        return None


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.app = AppDouble()
        self.executor = commands.CommandExecutor(self.app, tempfile.gettempdir())
        self.document = self.app.document
        self.node = self.document.target

    def run_command(self, action, **params):
        return self.executor.execute({"action": action, "params": params})

    def test_default_stroke_uses_one_real_qt_path(self):
        result = self.run_command("stroke", points=[[10, 10], [20, 30], [40, 10]])

        self.assertEqual((result["engine"], result["mode"], result["native_calls"], result["undo_steps"]), ("native", "path", 1, 1))
        self.assertIs(result["undo_steps_verified"], False)
        self.assertNotIn("undo_steps_observed", result)
        self.assertEqual(len(self.node.calls), 1)
        self.assertEqual(self.node.calls[0][2:4], ("ForegroundColor", "None"))
        self.assertGreater(self.node.calls[0][1].elementCount(), 3)

    def test_explicit_linear_overrides_smooth(self):
        result = self.run_command("stroke", points=[[10, 10], [20, 30], [40, 10]], curve="linear")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.node.calls[0][1].elementCount(), 3)

    def test_default_point_pressure_retains_single_native_path(self):
        result = self.run_command("stroke", points=[[10, 10, 1], [20, 20, 1]])

        self.assertEqual(result["mode"], "path")
        self.assertEqual(result["native_calls"], 1)
        self.assertEqual(self.node.calls[0][0], "path")

    def test_pressure_uses_native_integer_line_segments(self):
        result = self.run_command("stroke", points=[[10, 10, .2], [14, 10, .8]], pressure=.5, smooth=False)

        self.assertEqual(result["mode"], "pressure_segments")
        self.assertEqual(result["native_calls"], len(self.node.calls))
        self.assertEqual(result["undo_steps"], len(self.node.calls))
        self.assertIs(result["undo_steps_verified"], False)
        self.assertNotIn("undo_steps_observed", result)
        self.assertTrue(result["fidelity_warning"])
        self.assertTrue(all(isinstance(call[1], QPoint) for call in self.node.calls))
        self.assertAlmostEqual(self.node.calls[0][3], .1)
        self.assertAlmostEqual(self.node.calls[-1][4], .4)

    def test_taper_segments_begin_and_end_at_zero(self):
        result = self.run_command("stroke", points=[[10, 10], [18, 10]], taper_start=.3, taper_end=.3)

        self.assertEqual(result["mode"], "pressure_segments")
        self.assertEqual(self.node.calls[0][3], 0)
        self.assertEqual(self.node.calls[-1][4], 0)

    def test_pressure_deadline_reports_completed_segments_and_restores_state(self):
        raw = {"action": "stroke", "params": {"points": [[10, 10], [18, 10]], "pressure": .5, "size": 40, "color": "red", "preserve_alpha": True}}
        checks = []
        def checkpoint():
            checks.append(1)
            if len(checks) == 4:
                raise bridge.CommandError("deadline")

        result = self.executor.execute(raw, checkpoint)

        self.assertEqual(result["native_calls"], 2)
        self.assertEqual(result["undo_steps"], 2)
        self.assertIs(result["undo_steps_verified"], False)
        self.assertFalse(result["retry_safe"])
        self.assertEqual((self.app.view.size, self.node.alpha, self.app.view.foreground.getRgb()), (12, False, (0, 0, 255, 255)))

    def test_pressure_call_cost_is_rejected_before_mutation(self):
        result = self.run_command("stroke", points=[[0, 0], [10000, 0]], pressure=.5)

        self.assertIn("error", result)
        self.assertEqual(self.node.calls, [])

    def test_enormous_integer_is_a_command_error_not_overflow(self):
        with self.assertRaises(bridge.CommandError):
            commands.number(10 ** 400, "x", -1e9, 1e9)

    def test_http_enormous_json_integer_returns_400_without_queueing(self):
        queue = bridge.CommandQueue()
        server = bridge.BridgeServer(0, queue, commands.prepare, {})
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        try:
            payload = json.dumps({"action": "fill", "params": {"x": 10 ** 400, "y": 0}})

            connection.request("POST", "/", payload, {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read())

            self.assertEqual(response.status, 400)
            self.assertIn("error", result)
            self.assertEqual(len(queue.queue), 0)
        finally:
            connection.close()
            queue.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_http_large_whole_batch_is_validated_and_completed_across_ticks(self):
        queue = bridge.CommandQueue()
        arrived = Event()
        submit = queue.submit
        def signal(*args, **kwargs):
            request = submit(*args, **kwargs)
            arrived.set()
            return request
        server = bridge.BridgeServer(0, queue, commands.prepare, {})
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        child = {"action": "list_brushes", "params": {"filter": "x" * 4096}}
        payload = json.dumps({"action": "batch", "params": {"commands": [child] * 520}}).encode()
        self.assertGreater(len(payload), 2 * 1024 * 1024)
        def post():
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request("POST", "/", payload, {"Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            with patch.object(queue, "submit", side_effect=signal), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(post)
                submitted = arrived.wait(2)
                if submitted:
                    for _ in range(520):
                        queue.process_one(self.executor.begin)
                status, result = future.result(5)

            self.assertTrue(submitted)
            self.assertEqual(status, 200)
            self.assertEqual(result["completed_count"], 520)
            self.assertEqual(len(result["results"]), 520)
        finally:
            queue.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_rounded_no_op_segments_do_not_claim_observed_undo_steps(self):
        result = self.run_command("stroke", points=[[10.01, 10.01], [10.02, 10.02]], pressure=.5)

        self.assertEqual(result["native_calls"], 1)
        self.assertEqual(result["undo_steps"], 1)
        self.assertIs(result["undo_steps_verified"], False)
        self.assertEqual(self.node.calls[0][1], self.node.calls[0][2])
        self.assertNotIn("undo_steps_observed", result)

    def test_temporary_brush_and_alpha_are_restored(self):
        result = self.run_command("stroke", points=[[10, 10], [20, 10]], size=30, opacity=.2, color="red", preserve_alpha=True)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.node.calls[0][4:8], (True, 30, .2, (255, 0, 0, 255)))
        self.assertEqual((self.app.view.size, self.app.view.opacity, self.app.view.foreground.getRgb(), self.node.alpha), (12, .6, (0, 0, 255, 255), False))

    def test_canvas_inverse_double_changes_display_colors_but_not_canvasless_colors(self):
        supplied = QColor("#80804020")

        display_color = ManagedColorDouble.fromQColor(supplied, SimpleNamespace())
        standard_color = ManagedColorDouble.fromQColor(supplied, None)

        self.assertEqual(display_color.getRgb(), (64, 16, 4, 128))
        self.assertEqual(standard_color.getRgb(), (128, 64, 32, 128))

    def test_explicit_stroke_bypasses_canvas_inverse_transform(self):
        with patch.object(self.app.view, "canvas", return_value=SimpleNamespace()):
            result = self.run_command("stroke", points=[[10, 10], [20, 10]], color="#804020")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.node.calls[0][7], (128, 64, 32, 255))
        self.assertEqual(self.app.view.foreground.getRgb(), (0, 0, 255, 255))

    def test_set_color_bypasses_canvas_inverse_transform(self):
        with patch.object(self.app.view, "canvas", return_value=SimpleNamespace()):
            result = self.run_command("set_color", color="#80804020")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.app.view.foreground.getRgb(), (128, 64, 32, 128))

    def test_sampled_srgb_hex_roundtrips_to_native_color_without_display_inverse(self):
        self.document.profile = LINEAR_PROFILE
        self.document.root.region = (10, 20, 1, 1)
        self.document.root.pixels = bytes([16, 64, 144, 37])
        sampled = self.run_command("get_color_at", x=10, y=20)

        with patch.object(self.app.view, "canvas", return_value=SimpleNamespace()):
            selected = self.run_command("set_color", color=sampled["color"])
            painted = self.run_command("stroke", points=[[10, 10], [20, 10]], color=sampled["color"])

        self.assertEqual((selected["status"], painted["status"]), ("ok", "ok"))
        self.assertEqual(self.app.view.foreground.getRgb(), (192, 128, 64, 255))
        self.assertEqual(self.node.calls[0][7], (192, 128, 64, 255))

    def test_brush_is_restored_when_native_call_raises(self):
        self.node.fail_paint = True

        result = self.run_command("stroke", points=[[10, 10], [20, 10]], size=40, opacity=.1, color="red", preserve_alpha=True)

        self.assertIn("error", result)
        self.assertFalse(result["retry_safe"])
        self.assertEqual((self.app.view.size, self.app.view.opacity, self.node.alpha), (12, .6, False))
        self.assertEqual(self.app.view.foreground.getRgb(), (0, 0, 255, 255))

    def test_existing_alpha_lock_is_respected_and_restored(self):
        self.node.alpha = True

        result = self.run_command("fill_polygon", points=[[10, 10], [20, 10], [20, 20]])

        self.assertEqual(result["status"], "ok")
        self.assertTrue(self.node.calls[0][4])
        self.assertTrue(self.node.alpha)

    def test_batch_accepts_one_thousand_actions(self):
        raw = {"action": "batch", "params": {"commands": [{"action": "get_current_brush"}] * 1000}}

        result = self.executor.execute(raw)

        self.assertEqual(result["completed_count"], 1000)
        self.assertEqual(len(result["results"]), 1000)
        self.assertFalse(result["atomic"])

    def test_batch_accepts_more_than_one_thousand_commands(self):
        raw = {"action": "batch", "params": {"commands": [{"action": "set_color", "params": {"color": "red"}}] * 1001}}

        result = self.executor.execute(raw)

        self.assertEqual(result["completed_count"], 1001)
        self.assertEqual(self.app.view.foreground.getRgb(), (255, 0, 0, 255))

    def test_batch_accepts_more_than_4096_aggregate_native_calls(self):
        stroke = {"action": "stroke", "params": {"points": [[0, 0], [1000, 0]], "pressure": .5}}
        raw = {"action": "batch", "params": {"commands": [stroke] * 10}}

        result = self.executor.execute(raw)

        self.assertEqual(result["completed_count"], 10)
        self.assertGreater(len(self.node.calls), 4096)

    def test_batch_validates_last_action_before_first_mutation(self):
        raw = {"action": "batch", "params": {"commands": [{"action": "set_color", "params": {"color": "red"}}] * 1001 + [{"action": "stroke", "params": {"points": [[0, 0], [1, float("nan")]]}}]}}

        result = self.executor.execute(raw)

        self.assertIn("error", result)
        self.assertEqual(self.app.view.foreground.getRgb(), (0, 0, 255, 255))

    def test_batch_failure_returns_completed_prefix(self):
        raw = {"action": "batch", "params": {"commands": [{"action": "set_color", "params": {"color": "red"}}, {"action": "set_active_layer", "params": {"layer_id": "missing"}}, {"action": "set_color", "params": {"color": "green"}}]}}

        result = self.executor.execute(raw)

        self.assertEqual((result["completed_count"], result["failing_index"], len(result["results"])), (1, 1, 1))
        self.assertFalse(result["retry_safe"])
        self.assertEqual(self.app.view.foreground.getRgb(), (255, 0, 0, 255))

    def test_unexpected_runtime_exception_still_preserves_batch_prefix(self):
        raw = {"action": "batch", "params": {"commands": [{"action": "set_color", "params": {"color": "red"}}, {"action": "get_current_brush"}]}}

        with patch.object(self.app.view, "currentBrushPreset", side_effect=AttributeError("unavailable")), patch("traceback.print_exc"):
            result = self.executor.execute(raw)

        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(result["failing_index"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertFalse(result["retry_safe"])

    def test_batch_deadline_preserves_prefix_and_prevents_retry_claim(self):
        raw = {"action": "batch", "params": {"commands": [{"action": "set_color", "params": {"color": "red"}}, {"action": "set_color", "params": {"color": "green"}}]}}
        def checkpoint():
            if self.app.view.foreground == QColor("red"):
                raise bridge.CommandError("deadline")

        result = self.executor.execute(raw, checkpoint)

        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(result["failing_index"], 1)
        self.assertFalse(result["retry_safe"])
        self.assertEqual(self.app.view.foreground.getRgb(), (255, 0, 0, 255))

    def queued_batch(self, children):
        queue = bridge.CommandQueue()
        request = queue.submit(commands.prepare({"action": "batch", "params": {"commands": children}}))
        return queue, request

    def test_batch_yields_one_whole_child_per_tick_without_interleaving(self):
        children = [{"action": "set_color", "params": {"color": name}} for name in ("red", "green", "blue")]
        queue, request = self.queued_batch(children)
        second = queue.submit(commands.prepare({"action": "set_color", "params": {"color": "yellow"}}))

        for index, name in enumerate(("red", "green", "blue")):
            queue.process_one(self.executor.begin)
            self.assertEqual(self.app.view.foreground, QColor(name))
            self.assertEqual(second.state, bridge.State.QUEUED)
            self.assertEqual(request.state, bridge.State.COMPLETE if index == 2 else bridge.State.RUNNING)
        queue.process_one(self.executor.begin)

        self.assertEqual([result["color"] for result in request.wait()["results"]], ["red", "green", "blue"])
        self.assertEqual(self.app.view.foreground, QColor("yellow"))

    def test_pressure_child_finishes_all_native_calls_in_one_tick(self):
        child = {"action": "stroke", "params": {"points": [[10, 10], [18, 10]], "pressure": .5}}
        queue, request = self.queued_batch([child, {"action": "get_current_brush"}])

        queue.process_one(self.executor.begin)

        self.assertEqual(request.state, bridge.State.RUNNING)
        self.assertGreater(len(self.node.calls), 1)
        self.assertEqual(request.batch.results[0]["native_calls"], len(self.node.calls))
        queue.close()

    def test_batch_context_is_captured_on_first_tick_not_submission(self):
        queue, request = self.queued_batch([{"action": "clear"}])
        replacement = DocumentDouble(self.app)
        self.app.document = replacement

        queue.process_one(self.executor.begin)

        self.assertEqual(request.wait()["completed_count"], 1)
        self.assertEqual(self.node.calls, [])
        self.assertEqual(len(replacement.target.calls), 1)

    def test_batch_rejects_multiple_document_views_before_first_mutation(self):
        for separate_window in (False, True):
            with self.subTest(separate_window=separate_window):
                self.setUp()
                unfocused = ViewDouble(self.document)
                unfocused.selected_node = self.document.createNode("unfocused", "paintlayer")
                unfocused.selected_node.parent = self.document.root
                self.document.root.children.append(unfocused.selected_node)
                self.app.view.selected_node = self.node
                if separate_window:
                    self.app.other_windows.append(SimpleNamespace(views=lambda: [unfocused]))
                else:
                    self.app.extra_views.append(unfocused)
                queue, request = self.queued_batch([{"action": "set_color", "params": {"color": "red"}}, {"action": "clear"}])
                with patch.object(self.document, "activeNode", return_value=unfocused.selected_node):
                    queue.process_one(self.executor.begin)
                queue.close()

                result = request.wait()
                self.assertEqual((result["completed_count"], result["failing_index"]), (0, 0))
                self.assertFalse(result["failing_result"]["execution_started"])
                self.assertFalse(result["outcome_uncertain"])
                self.assertEqual(self.app.view.foreground, QColor("blue"))
                self.assertEqual((self.node.calls, unfocused.selected_node.calls), ([], []))

    def test_second_document_view_appearing_between_ticks_stops_prefix(self):
        queue, request = self.queued_batch([{"action": "clear"}, {"action": "clear"}])
        queue.process_one(self.executor.begin)
        self.app.other_windows.append(SimpleNamespace(views=lambda: [ViewDouble(self.document)]))

        queue.process_one(self.executor.begin)

        result = request.wait()
        self.assertEqual((result["completed_count"], result["failing_index"]), (1, 1))
        self.assertFalse(result["failing_result"]["execution_started"])
        self.assertEqual(len(self.node.calls), 1)

    def test_views_of_other_documents_do_not_block_current_document_batch(self):
        other = DocumentDouble(self.app)
        self.app.extra_views.append(ViewDouble(other))
        queue, request = self.queued_batch([{"action": "clear"}])

        queue.process_one(self.executor.begin)

        self.assertEqual(request.wait()["completed_count"], 1)
        self.assertEqual(len(self.node.calls), 1)
        self.assertEqual(other.target.calls, [])

    def test_second_view_during_pressure_child_stops_next_native_segment(self):
        child = {"action": "stroke", "params": {"points": [[10, 10], [18, 10]], "pressure": .5}}
        queue, request = self.queued_batch([child])
        native_paint = self.node.paintLine
        def paint(*args):
            native_paint(*args)
            self.app.extra_views.append(ViewDouble(self.document))

        with patch.object(self.node, "paintLine", side_effect=paint):
            queue.process_one(self.executor.begin)

        result = request.wait()
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(result["failing_result"]["native_calls"], 1)
        self.assertEqual(len(self.node.calls), 1)

    def test_standalone_paint_rejects_ambiguous_document_views(self):
        self.app.extra_views.append(ViewDouble(self.document))

        result = self.run_command("clear")

        self.assertIn("error", result)
        self.assertEqual(self.node.calls, [])

    def test_external_document_view_layer_switch_or_close_stops_prefix(self):
        for change in ("document", "view", "layer", "close"):
            with self.subTest(change=change):
                self.setUp()
                queue, request = self.queued_batch([{"action": "clear"}, {"action": "clear"}])
                queue.process_one(self.executor.begin)
                if change == "document":
                    self.app.document = DocumentDouble(self.app)
                elif change == "view":
                    self.app.view = ViewDouble()
                elif change == "layer":
                    self.document.active = self.document.createNode("other", "paintlayer")
                else:
                    self.app.document = None

                queue.process_one(self.executor.begin)

                result = request.wait()
                self.assertEqual((result["completed_count"], result["failing_index"]), (1, 1))
                self.assertFalse(result["failing_result"]["execution_started"])
                self.assertFalse(result["outcome_uncertain"])
                self.assertEqual(len(self.node.calls), 1)
                if self.app.document is not None and change == "document":
                    self.assertEqual(self.app.document.target.calls, [])

    def test_wait_for_done_switch_stops_before_native_mutation(self):
        queue, request = self.queued_batch([{"action": "clear"}])
        replacement = DocumentDouble(self.app)
        def switch():
            self.app.document = replacement

        with patch.object(self.document, "waitForDone", side_effect=switch):
            queue.process_one(self.executor.begin)

        self.assertEqual(request.wait()["completed_count"], 0)
        self.assertEqual((self.node.calls, replacement.target.calls), ([], []))

    def test_reentrant_wait_for_done_cannot_advance_active_or_queued_request(self):
        queue, request = self.queued_batch([{"action": "clear"}, {"action": "clear"}])
        pending = queue.submit(commands.prepare({"action": "clear"}))
        nested = []
        with patch.object(self.document, "waitForDone", side_effect=lambda: nested.append(queue.process_one(self.executor.begin))):
            queue.process_one(self.executor.begin)

        self.assertTrue(nested)
        self.assertEqual(set(nested), {False})
        self.assertEqual(len(self.node.calls), 1)
        self.assertEqual(pending.state, bridge.State.QUEUED)
        self.assertEqual(request.state, bridge.State.RUNNING)
        queue.close()

    def test_batch_ticks_survive_elapsed_120_seconds(self):
        with patch.object(bridge, "monotonic", return_value=0) as clock:
            queue, request = self.queued_batch([{"action": "clear"}, {"action": "clear"}])
            queue.process_one(self.executor.begin)
            clock.return_value = 121

            queue.process_one(self.executor.begin)

        self.assertEqual(request.wait()["completed_count"], 2)
        self.assertEqual(len(self.node.calls), 2)

    def test_shutdown_between_ticks_finishes_partial_batch_and_cancels_pending(self):
        queue, request = self.queued_batch([{"action": "clear"}, {"action": "clear"}])
        pending = queue.submit(commands.prepare({"action": "clear"}))
        queue.process_one(self.executor.begin)

        queue.close()

        result = request.wait()
        self.assertEqual((result["completed_count"], result["failing_index"]), (1, 1))
        self.assertFalse(result["outcome_uncertain"])
        self.assertEqual(pending.state, bridge.State.CANCELLED)
        self.assertFalse(queue.process_one(self.executor.begin))
        self.assertEqual(len(self.node.calls), 1)

    def test_shutdown_reentrantly_during_pressure_stops_next_segment(self):
        child = {"action": "stroke", "params": {"points": [[10, 10], [18, 10]], "pressure": .5, "size": 40}}
        queue, request = self.queued_batch([child, {"action": "clear"}])
        native = self.node.paintLine
        def paint(*args):
            native(*args)
            queue.close()

        with patch.object(self.node, "paintLine", side_effect=paint):
            queue.process_one(self.executor.begin)

        result = request.wait()
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(result["failing_result"]["native_calls"], 1)
        self.assertEqual(len(self.node.calls), 1)
        self.assertEqual(self.app.view.size, 12)

    def test_shutdown_reentrantly_after_child_finalizes_completed_prefix(self):
        queue, request = self.queued_batch([{"action": "clear"}, {"action": "clear"}])
        native = self.node.paintPath
        def paint(*args):
            native(*args)
            queue.close()

        with patch.object(self.node, "paintPath", side_effect=paint):
            queue.process_one(self.executor.begin)

        self.assertEqual(request.wait()["completed_count"], 1)
        self.assertEqual(len(self.node.calls), 1)
        self.assertFalse(queue.process_one(self.executor.begin))

    def test_explicit_batch_layer_and_history_changes_update_expected_context(self):
        children = [{"action": "create_layer"}, {"action": "clear"},
                    {"action": "set_active_layer", "params": {"layer_id": "target"}},
                    {"action": "undo"}, {"action": "redo"}, {"action": "clear"}]
        queue, request = self.queued_batch(children)

        for _ in children:
            queue.process_one(self.executor.begin)

        self.assertEqual(request.wait()["completed_count"], len(children))
        self.assertEqual(len(self.document.nodes[0].calls), 1)
        self.assertEqual(len(self.node.calls), 1)

    def test_explicit_new_and_open_update_context_for_later_paint(self):
        for action in ("new_canvas", "open_file"):
            with self.subTest(action=action), tempfile.NamedTemporaryFile(suffix=".kra") as fixture:
                self.setUp()
                replacement = DocumentDouble(self.app)
                params = {"path": fixture.name} if action == "open_file" else {"width": 8, "height": 8}
                queue, request = self.queued_batch([{"action": action, "params": params}, {"action": "clear"}])
                with patch.object(self.app, "openDocument", return_value=replacement):
                    queue.process_one(self.executor.begin)
                    queue.process_one(self.executor.begin)

                self.assertEqual(request.wait()["completed_count"], 2)
                self.assertEqual(self.node.calls, [])
                self.assertEqual(len(self.app.document.active.calls), 1)

    def test_external_switch_during_create_layer_is_not_adopted(self):
        queue, request = self.queued_batch([{"action": "create_layer"}, {"action": "clear"}])
        replacement = DocumentDouble(self.app)
        def wait():
            if self.document.active is not self.node:
                self.app.document = replacement
        with patch.object(self.document, "waitForDone", side_effect=wait):
            queue.process_one(self.executor.begin)
        queue.process_one(self.executor.begin)

        self.assertEqual(request.wait()["completed_count"], 1)
        self.assertEqual(replacement.target.calls, [])

    def test_nested_batch_and_atomic_and_hardness_are_rejected(self):
        for raw in ({"action": "batch", "params": {"commands": [{"action": "batch", "params": {"commands": [{"action": "undo"}]}}]}},
                    {"action": "batch", "params": {"commands": [{"action": "undo"}], "atomic": True}},
                    {"action": "stroke", "params": {"points": [[0, 0], [1, 1]], "hardness": .5}}):
            with self.subTest(raw=raw):
                self.assertIn("error", self.executor.execute(raw))

    def test_all_invalid_types_are_rejected_before_mutation(self):
        for action, params in (("stroke", {"points": [[0, 0], [1, 1]], "opacity": 1.1}),
                               ("stroke", {"points": [[0, 0], [1, 1]], "pressure": None}),
                               ("stroke", {"points": [[0, 0], [1, 1]], "smooth": 1}),
                               ("set_color", {"color": "not-a-color"}),
                               ("create_layer", {"blending_mode": "erase"}),
                               ("select_rect", {"x": 0, "y": 0, "width": True, "height": 2}),
                               ("stroke", {"points": [[0, 0], [1, 1]], "curve": "bezier"})):
            with self.subTest(action=action, params=params):
                self.assertIn("error", self.run_command(action, **params))
        self.assertEqual(self.node.calls, [])
        self.assertEqual(self.document.nodes, [])

    def test_parsed_params_cannot_be_changed(self):
        raw = {"action": "stroke", "params": {"points": [[0, 0], [1, 1]]}}
        prepared = commands.prepare(raw)
        raw["params"]["points"][0][0] = 99

        self.assertEqual(prepared.params["points"][0][0], 0)
        with self.assertRaises(TypeError):
            prepared.params["smooth"] = False
        with self.assertRaises(FrozenInstanceError):
            prepared.samples = ()

    def test_native_fill_uses_fill_style_without_outline(self):
        result = self.run_command("fill_polygon", points=[[10, 10], [20, 10], [20, 20]], color="red", smooth=False)

        self.assertEqual(result["engine"], "native")
        self.assertEqual(self.node.calls[0][2:4], ("None", "ForegroundColor"))
        self.assertEqual(self.document.nodes, [])

    def test_generated_gradient_writes_detached_then_attaches_above_target(self):
        before = (self.node.uid, self.node.pixels, self.document.active)

        result = self.run_command("gradient_polygon", points=[[10, 10], [20, 10], [20, 20]], color="red", gradient_end_color="blue", smooth=False)

        self.assertTrue(result["independent_layer"])
        self.assertEqual(result["target_layer_id"], "target")
        generated = self.document.nodes[0]
        self.assertEqual(self.document.root.children, [self.node, generated])
        self.assertEqual(self.document.events[:4], [("convert", generated.uid, STANDARD_SRGB, STANDARD_SRGB, False),
                                                   ("detached_write", generated.uid),
                                                   ("convert", generated.uid, STANDARD_SRGB, STANDARD_SRGB, True),
                                                   ("attach", generated.uid, "target")])
        self.assertEqual((self.node.uid, self.node.pixels, self.document.active), before)

    def test_generated_feather_is_independent_layer(self):
        result = self.run_command("fill_polygon", points=[[10, 10], [20, 10], [20, 20]], feather=3)

        self.assertEqual(result["mode"], "generated_layer")
        self.assertEqual(self.node.calls, [])
        self.assertEqual(len(self.document.nodes), 1)

    def test_generated_autoactivation_restores_target_for_batch_preview_and_stroke(self):
        preview = ModuleType(PACKAGE + ".preview")
        preview_targets = []
        def export_preview(document, filename, **kwargs):
            preview_targets.append(document.activeNode())
            return {"path": "output.png", "width": 160, "height": 100}
        preview.export_preview = export_preview
        children = [{"action": "gradient_polygon", "params": {"points": [[10, 10], [20, 10], [20, 20]],
                      "color": "red", "gradient_end_color": "blue"}},
                    {"action": "get_canvas"},
                    {"action": "stroke", "params": {"points": [[10, 10], [20, 10]]}}]
        queue, request = self.queued_batch(children)

        with patch.dict(sys.modules, {preview.__name__: preview}):
            for _ in children:
                queue.process_one(self.executor.begin)

        self.assertEqual(request.wait()["completed_count"], 3)
        self.assertEqual(preview_targets, [self.node])
        self.assertIs(self.document.active, self.node)
        self.assertEqual(len(self.node.calls), 1)
        self.assertEqual(self.document.nodes[0].calls, [])

    def test_standalone_generated_autoactivation_restores_original_target_after_wait(self):
        with patch.object(self.document, "setActiveNode", wraps=self.document.setActiveNode) as activate:
            result = self.run_command("gradient_polygon", points=[[10, 10], [20, 10], [20, 20]],
                                      color="red", gradient_end_color="blue")

        self.assertEqual(result["status"], "ok")
        activate.assert_called_once_with(self.node)
        self.assertIs(self.document.active, self.node)

    def test_generated_leaves_already_active_target_without_reactivation(self):
        self.document.auto_activate = False
        with patch.object(self.document, "setActiveNode", wraps=self.document.setActiveNode) as activate:
            result = self.run_command("gradient_polygon", points=[[10, 10], [20, 10], [20, 20]],
                                      color="red", gradient_end_color="blue")

        self.assertEqual(result["status"], "ok")
        activate.assert_not_called()
        self.assertIs(self.document.active, self.node)

    def test_standalone_attachment_view_switch_does_not_restore_target(self):
        native_wait = self.document.waitForDone
        def wait():
            native_wait()
            if any(node.parent is not None for node in self.document.nodes):
                self.app.view = ViewDouble(self.document)
        with patch.object(self.document, "waitForDone", side_effect=wait), patch.object(self.document, "setActiveNode", wraps=self.document.setActiveNode) as activate:
            result = self.run_command("gradient_polygon", points=[[10, 10], [20, 10], [20, 20]],
                                      color="red", gradient_end_color="blue")

        self.assertIn("error", result)
        self.assertEqual(result["layer_id"], self.document.nodes[0].uid)
        activate.assert_not_called()
        self.assertIs(self.document.active, self.document.nodes[0])

    def test_attachment_context_changes_are_not_adopted_or_overwritten(self):
        for change in ("document", "view", "layer", "extra_view"):
            with self.subTest(change=change):
                self.setUp()
                unrelated = NodeDouble(self.document, "unrelated", "unrelated")
                replacement = DocumentDouble(self.app)
                native_wait = self.document.waitForDone
                def wait():
                    native_wait()
                    if not any(node.parent is not None for node in self.document.nodes):
                        return
                    if change == "document":
                        self.app.document = replacement
                    elif change == "view":
                        self.app.view = ViewDouble(self.document)
                    elif change == "layer":
                        self.document.active = unrelated
                    else:
                        self.app.extra_views.append(ViewDouble(self.document))
                children = [{"action": "set_color", "params": {"color": "red"}},
                            {"action": "gradient_polygon", "params": {"points": [[10, 10], [20, 10], [20, 20]],
                              "color": "red", "gradient_end_color": "blue"}}, {"action": "clear"}]
                queue, request = self.queued_batch(children)
                queue.process_one(self.executor.begin)

                with patch.object(self.document, "waitForDone", side_effect=wait), patch.object(self.document, "setActiveNode", wraps=self.document.setActiveNode) as activate:
                    queue.process_one(self.executor.begin)
                    queue.process_one(self.executor.begin)

                result = request.wait()
                self.assertEqual((result["completed_count"], result["failing_index"]), (1, 1))
                self.assertEqual(result["failing_result"]["layer_id"], self.document.nodes[0].uid)
                self.assertFalse(result["retry_safe"])
                activate.assert_not_called()
                self.assertEqual(self.node.calls, [])
                self.assertEqual(replacement.target.calls, [])
                if change == "layer":
                    self.assertIs(self.document.active, unrelated)
                else:
                    self.assertIs(self.document.active, self.document.nodes[0])

    def test_generated_preserve_alpha_is_rejected_for_both_operations(self):
        for action, extra in (("fill_polygon", {"feather": 2}), ("gradient_polygon", {"color": "red", "gradient_end_color": "blue"})):
            with self.subTest(action=action):
                result = self.run_command(action, points=[[10, 10], [20, 10], [20, 20]], preserve_alpha=True, **extra)

                self.assertIn("error", result)
        self.assertEqual(self.document.nodes, [])

    def test_generated_selection_reads_exact_global_patch_bbox(self):
        selected = SelectionDouble()
        selected.setPixelData(bytes((128, 255)), 15, 10, 2, 1)
        self.document.selected = selected
        expected = b"".join(bytes((0, 0, 255, 128 if horizontal == 15 else 255))
                            if vertical == 10 and horizontal in (15, 16) else bytes(4)
                            for vertical in range(9, 21) for horizontal in range(9, 21))

        result = self.run_command("gradient_polygon", points=[[10, 10], [20, 10], [20, 20], [10, 20]], color="red", gradient_end_color="red", smooth=False)

        self.assertEqual(result["status"], "ok")
        layer = self.document.nodes[0]
        self.assertEqual(layer.region, (9, 9, 12, 12))
        self.assertEqual(selected.reads, [(9, 9, 12, 12)])
        self.assertEqual(layer.pixels, expected)

    def test_generated_rejects_unsupported_color_depth_without_attachment(self):
        self.document.depth = "U16"

        result = self.run_command("gradient_polygon", points=[[10, 10], [20, 10], [20, 20]], color="red", gradient_end_color="blue")

        self.assertIn("error", result)
        self.assertEqual(self.document.nodes, [])
        self.assertEqual(self.document.root.children, [self.node])

    def test_managed_color_double_converts_nonlinear_rgb_and_preserves_alpha(self):
        managed = ManagedColorDouble("RGBA", "F32", LINEAR_PROFILE)
        managed.setComponents([.25, .0625, 1, .4])

        managed.setColorSpace("RGBA", "U8", STANDARD_SRGB)

        self.assertEqual(managed.components(), [1, .25, .5, .4])
        self.assertEqual(managed.componentsOrdered(), [.5, .25, 1, .4])

    def test_generated_gradient_converts_whole_raster_after_srgb_write(self):
        self.document.profile = LINEAR_PROFILE
        points = [[10, 10], [20, 10], [20, 20], [10, 20]]
        patch = commands.polygon_patch(points, 160, 100, smooth=False,
                                       color=(64, 128, 192, 255), gradient_end_color=(192, 128, 64, 255))
        expected = bytes(value if index % 4 == 3 else round((value / 255) ** 2 * 255)
                         for index, value in enumerate(patch[4]))
        before = (self.node.pixels, self.node.profile, self.document.active)

        result = self.run_command("gradient_polygon", points=points, color="#4080c0",
                                  gradient_end_color="#c08040", smooth=False)

        self.assertEqual(result["status"], "ok")
        generated = self.document.nodes[0]
        self.assertEqual(generated.pixels, expected)
        self.assertEqual(generated.write_profiles, [STANDARD_SRGB])
        self.assertEqual(generated.profile, LINEAR_PROFILE)
        self.assertEqual(self.document.events[:4], [("convert", generated.uid, LINEAR_PROFILE, STANDARD_SRGB, False),
                                                   ("detached_write", generated.uid),
                                                   ("convert", generated.uid, STANDARD_SRGB, LINEAR_PROFILE, True),
                                                   ("attach", generated.uid, "target")])
        self.assertEqual((self.node.pixels, self.node.profile, self.document.active), before)

    def test_feathered_foreground_copies_original_space_without_display_conversion(self):
        self.document.profile = LINEAR_PROFILE
        foreground = ManagedColorDouble("RGBA", "F32", LINEAR_PROFILE)
        foreground.setComponents([.25, .0625, 1, .4])
        original = foreground.components()
        points = [[10, 10], [20, 10], [20, 20], [10, 20]]
        selected = SelectionDouble()
        selected.setPixelData(bytes([128]) * 160 * 100, 0, 0, 160, 100)
        self.document.selected = selected
        raster = commands.polygon_patch(points, 160, 100, smooth=False, feather=2, color=(128, 64, 255, 102))
        x, y, width, height, pixels = raster
        masked = commands.apply_selection_bgra(pixels, selected.pixelData(x, y, width, height))
        expected = bytes(value if index % 4 == 3 else round((value / 255) ** 2 * 255)
                         for index, value in enumerate(masked))

        with patch.object(self.app.view, "foregroundColor", return_value=foreground), patch.object(foreground, "colorForCanvas", side_effect=AssertionError("Display conversion must not be used")):
            result = self.run_command("fill_polygon", points=points, feather=2, smooth=False)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.document.nodes[0].pixels, expected)
        self.assertEqual(foreground.components(), original)
        self.assertEqual((foreground.model, foreground.depth, foreground.profile), ("RGBA", "F32", LINEAR_PROFILE))
        self.assertEqual(foreground.conversions, [])

    def test_unavailable_generated_source_or_target_profile_prevents_attachment(self):
        for missing in (STANDARD_SRGB, LINEAR_PROFILE):
            with self.subTest(missing=missing):
                self.setUp()
                self.document.profile = LINEAR_PROFILE
                self.app.profile_names.remove(missing)

                result = self.run_command("gradient_polygon", points=[[10, 10], [20, 10], [20, 20]],
                                          color="#808080", gradient_end_color="#404040")

                self.assertIn("error", result)
                self.assertEqual(self.document.nodes, [])
                self.assertEqual(self.document.root.children, [self.node])
                self.assertEqual(self.node.pixels, b"")

    def test_failed_detached_profile_conversion_does_not_attach_or_mutate_target(self):
        for failing in (STANDARD_SRGB, LINEAR_PROFILE):
            with self.subTest(failing=failing):
                self.setUp()
                self.document.profile = LINEAR_PROFILE
                self.app.node_conversion_failures.add(failing)

                result = self.run_command("gradient_polygon", points=[[10, 10], [20, 10], [20, 20]],
                                          color="#808080", gradient_end_color="#404040")

                self.assertIn("error", result)
                self.assertEqual(self.document.root.children, [self.node])
                self.assertEqual(self.node.pixels, b"")
                self.assertIsNone(self.document.nodes[0].parent)
                self.assertEqual(self.document.nodes[0].write_profiles, [] if failing == STANDARD_SRGB else [STANDARD_SRGB])

    def test_foreground_conversion_failure_prevents_generated_attachment(self):
        with patch.object(ManagedColorDouble, "setColorSpace", return_value=False):
            result = self.run_command("fill_polygon", points=[[10, 10], [20, 10], [20, 20]], feather=2)

        self.assertIn("error", result)
        self.assertEqual(self.document.nodes, [])
        self.assertEqual(self.document.root.children, [self.node])

    def test_foreground_conversion_copies_non_rgba_source_components(self):
        source = ManagedColorDouble("CMYKA", "F32", LINEAR_PROFILE)
        source.setComponents([.75, .9375, 0, 0, .4])

        rgba = self.executor.srgb_components(source)

        self.assertEqual(rgba, (128, 64, 255, 102))
        self.assertEqual(source.components(), [.75, .9375, 0, 0, .4])
        self.assertEqual((source.model, source.depth, source.profile), ("CMYKA", "F32", LINEAR_PROFILE))
        self.assertEqual(source.conversions, [])

    def test_unregistered_foreground_profile_prevents_generated_attachment(self):
        foreground = ManagedColorDouble("RGBA", "F32", "unavailable.icc")
        foreground.setComponents([.25, .5, .75, 1])
        with patch.object(self.app.view, "foregroundColor", return_value=foreground):
            result = self.run_command("fill_polygon", points=[[10, 10], [20, 10], [20, 20]], feather=2)

        self.assertIn("error", result)
        self.assertEqual(self.document.nodes, [])
        self.assertEqual(self.document.root.children, [self.node])
        self.assertEqual(foreground.components(), [.25, .5, .75, 1])

    def test_get_color_at_converts_document_bgra_to_srgb_preserving_alpha(self):
        self.document.profile = LINEAR_PROFILE
        self.document.root.region = (10, 20, 1, 1)
        self.document.root.pixels = bytes([16, 64, 144, 37])

        result = self.run_command("get_color_at", x=10, y=20)

        self.assertEqual(result, {"status": "ok", "color": "#c08040", "r": 192, "g": 128, "b": 64, "a": 37})
        self.assertEqual(self.document.root.pixels, bytes([16, 64, 144, 37]))

    def test_get_color_at_rounds_clamps_rgb_and_keeps_original_alpha(self):
        self.document.root.region = (10, 20, 1, 1)
        self.document.root.pixels = bytes([16, 64, 144, 37])
        with patch.object(ManagedColorDouble, "componentsOrdered", return_value=[-.1, .501, 1.2, .9]):
            result = self.run_command("get_color_at", x=10, y=20)

        self.assertEqual(result, {"status": "ok", "color": "#0080ff", "r": 0, "g": 128, "b": 255, "a": 37})

    def test_get_color_at_rejects_unavailable_profiles_and_failed_conversion(self):
        for failure in ("source", "destination", "conversion"):
            with self.subTest(failure=failure):
                self.setUp()
                self.document.profile = LINEAR_PROFILE
                self.document.root.region = (10, 20, 1, 1)
                self.document.root.pixels = bytes([16, 64, 144, 37])
                if failure != "conversion":
                    self.app.profile_names.remove(LINEAR_PROFILE if failure == "source" else STANDARD_SRGB)
                with patch.object(ManagedColorDouble, "setColorSpace", return_value=failure != "conversion"):
                    result = self.run_command("get_color_at", x=10, y=20)

                self.assertIn("error", result)
                self.assertEqual(self.document.root.pixels, bytes([16, 64, 144, 37]))

    def test_get_color_at_retains_non_rgba_u8_rejection(self):
        self.document.depth = "U16"

        result = self.run_command("get_color_at", x=10, y=20)

        self.assertIn("error", result)

    def test_select_rect_global_bbox_and_deselect_none(self):
        result = self.run_command("select_rect", x=20, y=30, width=4, height=3)

        self.assertEqual(result["bbox"], [20, 30, 4, 3])
        self.assertEqual(self.document.selected.region, (20, 30, 4, 3))
        self.assertEqual(self.document.selected.data, bytes([255]) * 12)
        self.assertEqual(self.run_command("deselect")["status"], "ok")
        self.assertIsNone(self.document.selected)

    def test_select_ellipse_has_real_qt_coverage(self):
        result = self.run_command("select_ellipse", x=20, y=30, width=10, height=10)

        self.assertEqual(result["bbox"], [20, 30, 10, 10])
        self.assertEqual(self.document.selected.data[55], 255)
        self.assertLess(self.document.selected.data[0], 255)

    def test_select_polygon_defaults_to_linear(self):
        result = self.run_command("select_polygon", points=[[20, 30], [30, 30], [30, 40]])

        self.assertEqual(result["bbox"], [20, 30, 10, 10])
        self.assertGreater(sum(self.document.selected.data), 0)

    def test_select_by_alpha_thresholds_bytes_in_node_bbox(self):
        self.node.region = (20, 30, 2, 2)
        self.node.pixels = bytes([0, 0, 255, 0, 0, 0, 255, 20, 0, 0, 255, 128, 0, 0, 255, 255])

        result = self.run_command("select_by_alpha", threshold=128)

        self.assertEqual(result["bbox"], [20, 30, 2, 2])
        self.assertEqual(self.document.selected.data, bytes([0, 0, 255, 255]))

    def test_fill_selection_requires_selection_before_paint(self):
        result = self.run_command("fill_selection")

        self.assertIn("error", result)
        self.assertEqual(self.node.calls, [])

    def test_fill_selection_is_native_and_restores_alpha(self):
        self.document.selected = SelectionDouble()

        result = self.run_command("fill_selection", color="red", preserve_alpha=True)

        self.assertEqual(result["engine"], "native")
        self.assertEqual(self.node.calls[0][2:5], ("None", "ForegroundColor", True))
        self.assertFalse(self.node.alpha)

    def test_legacy_fill_circle_and_clear_never_write_attached_pixels(self):
        fill_result = self.run_command("fill", x=10, y=20, radius=5)
        clear_result = self.run_command("clear")

        self.assertEqual((fill_result["engine"], clear_result["engine"]), ("native", "native"))
        self.assertEqual(self.node.calls[0][1].boundingRect().toRect(), QRect(5, 15, 10, 10))
        self.assertEqual(self.document.events, [])

    def test_draw_shape_filled_outline_and_line(self):
        for shape in ("rectangle", "ellipse", "line"):
            with self.subTest(shape=shape):
                result = self.run_command("draw_shape", shape=shape, x=10, y=20, width=20, height=10, fill=False)

                self.assertEqual(result["status"], "ok")
                self.assertEqual(self.node.calls[-1][2:4], ("ForegroundColor", "None"))

    def test_layers_preserve_ids_and_properties(self):
        created = self.run_command("create_layer", name="Overlay", blending_mode="overlay")
        layer_id = created["layer_id"]

        changed = self.run_command("set_layer_properties", layer_id=layer_id, opacity=.5, visible=False, preserve_alpha=True, name="Changed")

        self.assertEqual(changed["layer_id"], layer_id)
        self.assertEqual(changed["name"], "Changed")
        self.assertAlmostEqual(changed["opacity"], 128 / 255)
        self.assertFalse(changed["visible"])
        self.assertTrue(changed["preserve_alpha"])
        self.assertEqual(self.run_command("set_active_layer", layer_id="target")["layer_id"], "target")
        self.assertEqual(len(self.run_command("list_layers")["layers"]), 2)

    def test_get_current_brush_and_persistent_opacity(self):
        self.run_command("set_brush", size=33, opacity=.25)

        result = self.run_command("get_current_brush")

        self.assertEqual((result["preset"], result["size"], result["opacity"]), ("Basic", 33, .25))

    def test_undo_and_redo_report_request_not_proven_history(self):
        results = [self.run_command(action) for action in ("undo", "redo")]

        self.assertEqual(self.app.triggers, ["edit_undo", "edit_redo"])
        self.assertTrue(all(result["history_verified"] is False for result in results))
        self.assertEqual([result["requested"] for result in results], ["undo", "redo"])

    def test_new_canvas_background_written_before_attachment(self):
        result = self.run_command("new_canvas", width=8, height=8, background="#80ff0000")

        self.assertEqual(result["status"], "ok")
        document = self.app.document
        self.assertEqual(document.events[0][0], "detached_write")
        self.assertEqual(document.events[1][0], "attach")
        self.assertEqual(document.nodes[0].pixels[:4], bytes([0, 0, 255, 128]))
        self.assertEqual(document.profile, STANDARD_SRGB)

    def test_new_canvas_rejects_unavailable_standard_srgb_before_creation(self):
        self.app.profile_names.remove(STANDARD_SRGB)
        with patch.object(self.app, "createDocument", wraps=self.app.createDocument) as create:
            result = self.run_command("new_canvas", width=8, height=8)

        self.assertIn("error", result)
        create.assert_not_called()
        self.assertIs(self.app.document, self.document)

    def test_save_failure_restores_batch_mode(self):
        self.document.export_success = False
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_command("save", path=str(Path(directory) / "test.png"))

        self.assertIn("error", result)
        self.assertFalse(self.document.batch)

    def test_save_kra_uses_checked_file_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = str(Path(directory) / "test.kra")

            result = self.run_command("save", path=filename)

            self.assertEqual(result["path"], filename)
            self.assertTrue(Path(filename).is_file())
            self.assertEqual(self.document.filename, filename)
        self.assertFalse(self.document.batch)

    def test_kra_false_or_exception_restores_original_filename(self):
        for raises in (False, True):
            with self.subTest(raises=raises), tempfile.TemporaryDirectory() as directory:
                self.setUp()
                def save(filename):
                    self.document.filename = filename
                    if raises:
                        raise RuntimeError("Save failed")
                    return False
                with patch.object(self.document, "saveAs", side_effect=save):
                    result = self.run_command("save", path=str(Path(directory) / "failed.kra"))

                self.assertIn("error", result)
                self.assertEqual(self.document.filename, "original.kra")
                self.assertFalse(self.document.batch)

    def test_open_failure_is_not_reported_as_success(self):
        with tempfile.NamedTemporaryFile(suffix=".kra") as fixture:
            result = self.run_command("open_file", path=fixture.name)

        self.assertIn("error", result)
        self.assertIs(self.app.document, self.document)

    def test_preview_uses_lazy_worker_contract(self):
        preview = ModuleType(PACKAGE + ".preview")
        calls = []
        def export_preview(document, filename, **kwargs):
            calls.append((document, filename, kwargs))
            return {"path": "output.png", "width": 160, "height": 100}
        preview.export_preview = export_preview

        with patch.dict(sys.modules, {preview.__name__: preview}):
            result = self.run_command("get_canvas", show_grid=True, grid_spacing=20)

        self.assertEqual(result["path"], "output.png")
        self.assertEqual(calls[0][1], "canvas.png")
        self.assertEqual(calls[0][2]["show_grid"], True)
        self.assertEqual(self.document.events, [])

    def test_preview_path_traversal_is_rejected(self):
        result = self.run_command("get_canvas", filename="../escape.png")

        self.assertIn("error", result)

    def test_preview_shape_errors_prevalidate_before_batch_mutation(self):
        for params in ({"filename": "canvas.jpg"}, {"filename": ".hidden.png"}, {"grid_spacing": 2}, {"grid_spacing": 10.5}):
            raw = {"action": "batch", "params": {"commands": [{"action": "set_color", "params": {"color": "red"}}, {"action": "get_canvas", "params": params}]}}

            with self.subTest(params=params):
                result = self.executor.execute(raw)

                self.assertIn("error", result)
                self.assertEqual(self.app.view.foreground.getRgb(), (0, 0, 255, 255))


if __name__ == "__main__":
    unittest.main()
