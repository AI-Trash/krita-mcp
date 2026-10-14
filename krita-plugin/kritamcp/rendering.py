"""Geometry for native painting; bounded offline polygon patches, never strokes.

Coordinates stay in document space. Sampling uses <=2px chords and <=0.2px
cubic deviation; pressure controls interpolate cubically for explicit Beziers,
linearly between Catmull-Rom knots. Tapers are fractions of sampled arc length.
Limits: 4096 controls, coordinates +/-1e9, 131072 samples, 4M raster pixels,
32M feather channel visits. Requests exceeding a limit raise RenderError.
SIZE_OK: the task's fixed single-module integration boundary owns these helpers.
Public keyword signatures are fixed by the downstream integration contract.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import Enum
from itertools import accumulate
from typing import TYPE_CHECKING, Final, TypeAlias, assert_never

if TYPE_CHECKING:
    from PyQt5.QtCore import QByteArray
    from PyQt5.QtGui import QPainterPath

Color = tuple[int, int, int, int]
Point = tuple[float, float, float]
Span = tuple[Point, Point, Point, Point]
Patch = tuple[int, int, int, int, bytes]
Buffer: TypeAlias = "bytes | bytearray | memoryview | QByteArray"
MAX_PIXELS: Final = 4_000_000
MAX_SAMPLES: Final = 131_072
MAX_WORK: Final = 32_000_000


class RenderError(ValueError):
    """An invalid request or exceeded resource budget."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _Curve(Enum):
    LINEAR = "linear"
    CATMULL_ROM = "catmull-rom"
    BEZIER = "bezier"


def _number(value: float, limits: tuple[float, float]) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not limits[0] <= value <= limits[1] or not math.isfinite(value)):
        raise RenderError(f"Expected finite number in {limits}")
    return float(value)


def _color(value: Color) -> Color:
    if len(value) != 4 or any(type(part) is not int or not 0 <= part <= 255 for part in value):
        raise RenderError("Colors must contain four RGBA integers in 0..255")
    return value[0], value[1], value[2], value[3]


def _points(points: Sequence[Sequence[float]]) -> list[Point]:
    if not 1 <= len(points) <= 4096:
        raise RenderError("Expected 1..4096 points")
    result: list[Point] = []
    for point in points:
        if len(point) not in (2, 3):
            raise RenderError("Points must be coordinate pairs or pressure triples")
        result.append((_number(point[0], (-1e9, 1e9)), _number(point[1], (-1e9, 1e9)),
                       _number(point[2], (0, 1)) if len(point) == 3 else 1.0))
    return result


def _mode(smooth: bool, curve: str | None) -> _Curve:
    if type(smooth) is not bool or curve not in (None, "linear", "bezier"):
        raise RenderError("Expected boolean smooth and curve None, linear or bezier")
    return _Curve(curve) if curve is not None else (_Curve.CATMULL_ROM if smooth else _Curve.LINEAR)


def _mix(first: Point, second: Point, amount: float) -> Point:
    return (first[0] + (second[0] - first[0]) * amount,
            first[1] + (second[1] - first[1]) * amount,
            first[2] + (second[2] - first[2]) * amount)


def _spans(points: list[Point], mode: _Curve, closed: bool) -> list[Span]:
    match mode:
        case _Curve.BEZIER:
            if len(points) < 4 or (len(points) - 1) % 3:
                raise RenderError("Bezier requires exactly 3n+1 points, n >= 1")
            return [(points[index], points[index + 1], points[index + 2], points[index + 3])
                    for index in range(0, len(points) - 1, 3)]
        case _Curve.LINEAR | _Curve.CATMULL_ROM:
            spans: list[Span] = []
            for index in range(len(points) if closed else len(points) - 1):
                start, end = points[index], points[(index + 1) % len(points)]
                first, second = _mix(start, end, 1 / 3), _mix(start, end, 2 / 3)
                if mode is _Curve.CATMULL_ROM:
                    before = points[(index - 1) % len(points)] if closed else points[max(0, index - 1)]
                    after = points[(index + 2) % len(points)] if closed else points[min(len(points) - 1, index + 2)]
                    first = (start[0] + (end[0] - before[0]) / 6,
                             start[1] + (end[1] - before[1]) / 6, first[2])
                    second = (end[0] - (after[0] - start[0]) / 6,
                              end[1] - (after[1] - start[1]) / 6, second[2])
                spans.append((start, first, second, end))
            return spans
        case unreachable:
            assert_never(unreachable)


def build_path(points: Sequence[Sequence[float]], *, smooth: bool = True,
               curve: str | None = None, closed: bool = False) -> QPainterPath:
    """Build a global-coordinate Qt path; Bezier points are explicit 3n+1 controls."""
    from PyQt5.QtGui import QPainterPath

    controls, mode = _points(points), _mode(smooth, curve)
    if type(closed) is not bool:
        raise RenderError("closed must be boolean")
    if closed and mode is not _Curve.BEZIER and len(controls) > 1 and controls[0][:2] == controls[-1][:2]:
        controls.pop()
    path = QPainterPath()
    path.moveTo(*controls[0][:2])
    for start, first, second, end in _spans(controls, mode, closed):
        match mode:
            case _Curve.LINEAR:
                path.lineTo(*end[:2])
            case _Curve.CATMULL_ROM | _Curve.BEZIER:
                path.cubicTo(*first[:2], *second[:2], *end[:2])
            case unreachable:
                assert_never(unreachable)
    if closed:
        path.closeSubpath()
    return path


def sample_stroke(points: Sequence[Sequence[float]], *, smooth: bool = True,
                  curve: str | None = None, pressure: float = 1.0,
                  taper_start: float = 0.0, taper_end: float = 0.0) -> list[Point]:
    """Approximate geometry/pressure for segmented native paintLine, not raster stamps."""
    controls, mode = _points(points), _mode(smooth, curve)
    force, begin, finish = (_number(value, (0, 1)) for value in (pressure, taper_start, taper_end))
    path = [controls[0]]
    for span in _spans(controls, mode, False):
        stack = [(span, 0)]
        while stack:
            (start, first, second, end), depth = stack.pop()
            chord = math.hypot(end[0] - start[0], end[1] - start[1])
            if chord > 2 * (MAX_SAMPLES - 1):
                raise RenderError("Stroke exceeds spatial sampling budget")
            expected = (_mix(start, end, 1 / 3), _mix(start, end, 2 / 3))
            error = max(math.hypot(point[0] - linear[0], point[1] - linear[1])
                        for point, linear in zip((first, second), expected))
            pressure_error = max(abs(point[2] - linear[2]) for point, linear in zip((first, second), expected))
            if chord <= 2 and error <= .2 and pressure_error <= .005:
                path.append(end)
            else:
                if depth >= 28:
                    raise RenderError("Curve exceeds subdivision budget")
                left, center, right = _mix(start, first, .5), _mix(first, second, .5), _mix(second, end, .5)
                inner_left, inner_right = _mix(left, center, .5), _mix(center, right, .5)
                middle = _mix(inner_left, inner_right, .5)
                stack.extend((((middle, inner_right, right, end), depth + 1),
                              ((start, left, inner_left, middle), depth + 1)))
            if len(path) + len(stack) > MAX_SAMPLES:
                raise RenderError("Stroke exceeds sample budget")
    distances = list(accumulate((math.hypot(end[0] - start[0], end[1] - start[1])
                                 for start, end in zip(path, path[1:])), initial=0.0))
    total = distances[-1]
    result: list[Point] = []
    for point, distance in zip(path, distances):
        along = distance / total if total else .5
        taper = max(0.0, min(1.0, along / begin if begin else 1.0,
                             (1 - along) / finish if finish else 1.0))
        result.append((point[0], point[1], force * point[2] * taper))
    return result


def _buffer(value: Buffer) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        from PyQt5.QtCore import QByteArray

        if not isinstance(value, QByteArray):
            raise RenderError("Expected a byte buffer")
    size = value.nbytes if isinstance(value, memoryview) else len(value)
    if size > MAX_PIXELS * 4:
        raise RenderError("Buffer exceeds 4M pixels")
    return bytes(value)


def apply_selection_bgra(paint: Buffer, selection: Buffer) -> bytes:
    """Clip straight BGRA to patch-local coverage, clearing RGB only at zero coverage."""
    source_data, mask = _buffer(paint), _buffer(selection)
    if len(source_data) % 4:
        raise RenderError("Expected a BGRA buffer")
    if len(mask) != len(source_data) // 4:
        raise RenderError("Selection must have one coverage byte per pixel in this region")
    output = bytearray(len(source_data))
    for offset in range(0, len(source_data), 4):
        coverage = source_data[offset + 3] / 255 * (mask[offset // 4] / 255)
        if coverage:
            output[offset:offset + 3] = source_data[offset:offset + 3]
            output[offset + 3] = round(255 * coverage)
    return bytes(output)


def _blur(data: bytes, dimensions: tuple[int, int], radius: int) -> bytes:
    width, height = dimensions
    current = data
    for stride, count, lines in ((4, width, height), (4 * width, height, width)):
        output = bytearray(len(current))
        for line in range(lines):
            base = line * width * 4 if stride == 4 else line * 4
            for channel in range(4):
                summed = sum(current[base + position * stride + channel]
                             for position in range(min(count, radius + 1)))
                for position in range(count):
                    output[base + position * stride + channel] = round(summed / (2 * radius + 1))
                    if position - radius >= 0:
                        summed -= current[base + (position - radius) * stride + channel]
                    if position + radius + 1 < count:
                        summed += current[base + (position + radius + 1) * stride + channel]
        current = bytes(output)
    return current


def polygon_patch(points: Sequence[Sequence[float]], canvas_width: int, canvas_height: int, *,
                  color: Color = (0, 0, 0, 255), smooth: bool = True, feather: float = 0.0,
                  gradient_end_color: Color | None = None, gradient_type: str = "linear",
                  direction: float = 0.0) -> Patch | None:
    """AA closed polygon, bbox-local rasterization, premultiplied feather, straight BGRA."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor, QImage, QLinearGradient, QPainter, QRadialGradient

    controls = _points(points)
    if any(type(side) is not int or not 1 <= side <= 1_000_000 for side in (canvas_width, canvas_height)):
        raise RenderError("Canvas dimensions must be integers in 1..1000000")
    rgba = _color(color)
    end_color = _color(gradient_end_color) if gradient_end_color is not None else None
    softness = _number(feather, (0, 256))
    angle = math.radians(_number(direction, (-1e9, 1e9)) % 360)
    if len({point[:2] for point in controls}) < 3:
        raise RenderError("Polygon requires at least three distinct vertices")
    if gradient_type not in ("linear", "radial"):
        raise RenderError("Expected linear/radial gradient")
    path = build_path(controls, smooth=smooth, closed=True)
    box = path.boundingRect()
    radius = math.ceil(softness / 3)
    padding = 3 * radius + 1
    left, top = max(0, math.floor(box.left() - padding)), max(0, math.floor(box.top() - padding))
    right = min(canvas_width, math.ceil(box.right() + padding))
    bottom = min(canvas_height, math.ceil(box.bottom() + padding))
    if right <= left or bottom <= top or (rgba[3] == 0 and (end_color is None or end_color[3] == 0)):
        return None
    width, height = right - left, bottom - top
    render_width, render_height = width + 2 * padding, height + 2 * padding
    area = render_width * render_height
    if area > MAX_PIXELS or area * (24 if radius else 1) > MAX_WORK:
        raise RenderError("Polygon exceeds raster/feather work budget")
    image = QImage(render_width, render_height, QImage.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise RenderError("Could not allocate polygon raster")
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(padding - left, padding - top)
        painter.setPen(Qt.NoPen)
        if end_color is None:
            painter.setBrush(QColor(*rgba))
        else:
            center = box.center()
            match gradient_type:
                case "linear":
                    dx, dy = math.cos(angle), math.sin(angle)
                    half = (abs(dx) * box.width() + abs(dy) * box.height()) / 2
                    gradient = QLinearGradient(center.x() - dx * half, center.y() - dy * half,
                                               center.x() + dx * half, center.y() + dy * half)
                case "radial":
                    gradient = QRadialGradient(center, max(box.width(), box.height()) / 2)
                case _:
                    raise RenderError("Unsupported gradient")
            gradient.setColorAt(0, QColor(*rgba))
            gradient.setColorAt(1, QColor(*end_color))
            painter.setBrush(gradient)
        painter.drawPath(path)
    finally:
        painter.end()
    image = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    data = image.constBits().asstring(image.sizeInBytes())
    if radius:
        for _ in range(3):
            data = _blur(data, (render_width, render_height), radius)
    output = bytearray(width * height * 4)
    for row in range(height):
        for column in range(width):
            source = ((row + padding) * render_width + column + padding) * 4
            target = (row * width + column) * 4
            red, green, blue, alpha = data[source:source + 4]
            if alpha:
                output[target:target + 4] = bytes((min(255, round(blue * 255 / alpha)),
                                                  min(255, round(green * 255 / alpha)),
                                                  min(255, round(red * 255 / alpha)), alpha))
    return (left, top, width, height, bytes(output)) if any(output[3::4]) else None
