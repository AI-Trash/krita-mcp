"""Non-destructive PNG previews; grid/diff operate only on the exported image.

Limits: 16M pixels, 32768px per side, 128MiB encoded images, 2048 grid lines.
Diffs compare 8-bit sRGB RGBA: black is identical, red is maximum channel
error (minimum visible intensity 48). Untagged references are assumed sRGB.
The public keyword signature is fixed by the command integration contract.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Final, Protocol

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QColor, QColorSpace, QFont, QImage, QImageReader, QPainter

if TYPE_CHECKING:
    from krita import InfoObject

MAX_PIXELS: Final = 16_000_000
MAX_SIDE: Final = 32_768
MAX_FILE_BYTES: Final = 128 * 1024 * 1024
MAX_GRID_LINES: Final = 2048


class PreviewError(ValueError):
    """Invalid preview request or failed image operation."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PreviewDocument(Protocol):
    def width(self) -> int: ...
    def height(self) -> int: ...
    def fileName(self) -> str: ...
    def waitForDone(self) -> None: ...
    def batchmode(self) -> bool: ...
    def setBatchmode(self, value: bool) -> None: ...
    def exportImage(self, filename: str, exportConfiguration: InfoObject) -> bool: ...


def _dimensions(width: int, height: int) -> None:
    if (not 1 <= width <= MAX_SIDE or not 1 <= height <= MAX_SIDE
            or width * height > MAX_PIXELS):
        raise PreviewError("Image exceeds preview limits: 16M pixels, 32768px per side")


def _read_image(path: Path, dimensions: tuple[int, int]) -> QImage:
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise PreviewError(f"Image must be a readable file no larger than 128MiB: {path}")
    reader = QImageReader(str(path))
    size = reader.size()
    if not reader.canRead() or not size.isValid():
        raise PreviewError(f"Invalid or unreadable image: {path}: {reader.errorString()}")
    _dimensions(size.width(), size.height())
    if (size.width(), size.height()) != dimensions:
        raise PreviewError(f"Image dimensions must be exactly {dimensions[0]}x{dimensions[1]}: {path}")
    image = reader.read()
    if image.isNull() or (image.width(), image.height()) != dimensions:
        raise PreviewError(f"Image decoding failed: {path}: {reader.errorString()}")
    image.convertToColorSpace(QColorSpace(QColorSpace.SRgb))
    converted = image.convertToFormat(QImage.Format_RGBA8888)
    if converted.isNull():
        raise PreviewError("Could not allocate preview image")
    return converted


def _heatmap(image: QImage, reference: QImage) -> QImage:
    heatmap = QImage(image.width(), image.height(), QImage.Format_RGBA8888)
    if heatmap.isNull():
        raise PreviewError("Could not allocate difference heatmap")
    source_bits, reference_bits, heatmap_bits = image.constBits(), reference.constBits(), heatmap.bits()
    source_bits.setsize(image.sizeInBytes())
    reference_bits.setsize(reference.sizeInBytes())
    heatmap_bits.setsize(heatmap.sizeInBytes())
    source, baseline, target = memoryview(source_bits), memoryview(reference_bits), memoryview(heatmap_bits)
    for offset in range(0, image.sizeInBytes(), 4):
        delta = max(abs(source[offset + channel] - baseline[offset + channel]) for channel in range(4))
        target[offset:offset + 4] = bytes((max(48, delta) if delta else 0, 0, 0, 255))
    return heatmap


def _grid(image: QImage, spacing: int) -> None:
    painter = QPainter(image)
    if not painter.isActive():
        raise PreviewError("Could not paint preview grid")
    try:
        font = QFont()
        font.setPixelSize(11)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        origin_width, label_height = metrics.horizontalAdvance("(0,0)") + 6, metrics.height() + 4
        if image.width() < origin_width + 4 or image.height() < label_height + 4:
            raise PreviewError("Preview is too small for visible grid coordinate labels")
        painter.setPen(QColor(0, 0, 0, 180))
        for horizontal in range(0, image.width(), spacing):
            painter.drawLine(horizontal, 0, horizontal, image.height() - 1)
        for vertical in range(0, image.height(), spacing):
            painter.drawLine(0, vertical, image.width() - 1, vertical)
        painter.setPen(QColor(255, 255, 255, 210))
        for horizontal in range(0, image.width(), spacing):
            painter.drawLine(horizontal + 1, 0, horizontal + 1, image.height() - 1)
        for vertical in range(0, image.height(), spacing):
            painter.drawLine(0, vertical + 1, image.width() - 1, vertical + 1)
        labels = [(2, 2, "(0,0)")]
        right, bottom = origin_width + 4, label_height + 4
        for horizontal in range(spacing, image.width(), spacing):
            text = f"({horizontal},0)"
            width = metrics.horizontalAdvance(text) + 6
            if horizontal + 2 >= right and horizontal + width + 2 <= image.width():
                labels.append((horizontal + 2, 2, text))
                right = horizontal + width + 4
        for vertical in range(spacing, image.height(), spacing):
            text = f"(0,{vertical})"
            if (vertical + 2 >= bottom and vertical + label_height + 2 <= image.height()
                    and metrics.horizontalAdvance(text) + 8 <= image.width()):
                labels.append((2, vertical + 2, text))
                bottom = vertical + label_height + 4
        painter.setPen(QColor(255, 255, 255))
        for horizontal, vertical, text in labels:
            box = QRect(horizontal, vertical, metrics.horizontalAdvance(text) + 6, label_height)
            painter.fillRect(box, QColor(0, 0, 0, 230))
            painter.drawText(box, Qt.AlignCenter, text)
    finally:
        painter.end()


def export_preview(doc: PreviewDocument, filename: str, *, output_dir: str | Path,
                   show_grid: bool = False, grid_spacing: float = 100,
                   diff_with_reference: str | Path | None = None) -> dict[str, str | int]:
    """Export on Krita's main thread without editing document pixels or save-name.

    A suffixless basename gains .png; other suffixes and path components fail.
    Returns path/width/height and, for a diff, reference_path/diff_path.
    """
    from krita import InfoObject

    if (not isinstance(filename, str) or not filename or len(filename) > 200
            or any(character in filename for character in ("/", "\\", ":"))
            or any(ord(character) < 32 for character in filename)
            or filename in (".", "..") or filename.startswith(".")):
        raise PreviewError("filename must be a PNG basename, not an absolute path or traversal")
    name = Path(filename)
    if name.suffix and name.suffix.lower() != ".png":
        raise PreviewError("filename must have a .png extension")
    if type(show_grid) is not bool:
        raise PreviewError("show_grid must be boolean")
    if (type(grid_spacing) not in (int, float) or not 8 <= grid_spacing <= MAX_SIDE
            or not math.isfinite(grid_spacing) or int(grid_spacing) != grid_spacing):
        raise PreviewError("grid_spacing must be a finite integer in 8..32768 pixels")
    width, height = doc.width(), doc.height()
    _dimensions(width, height)
    spacing = int(grid_spacing)
    if show_grid and math.ceil(width / spacing) + math.ceil(height / spacing) > MAX_GRID_LINES:
        raise PreviewError("Grid exceeds the 2048-line limit; increase grid_spacing")
    directory = Path(output_dir).expanduser().resolve()
    path = directory / (filename if name.suffix else filename + ".png")
    diff_path = path.with_name(path.stem + "_diff.png")
    destinations = (path, diff_path) if diff_with_reference is not None else (path,)
    source_name = doc.fileName()
    if source_name and any(target.resolve() == Path(source_name).resolve() for target in destinations):
        raise PreviewError("Preview output cannot overwrite the source document")
    reference_path = Path(diff_with_reference).expanduser().resolve() if diff_with_reference is not None else None
    if reference_path is not None and any(target.resolve() == reference_path for target in destinations):
        raise PreviewError("Preview output cannot overwrite the reference image")
    reference = _read_image(reference_path, (width, height)) if reference_path is not None else None
    directory.mkdir(parents=True, exist_ok=True)
    result: dict[str, str | int] = {"path": str(path), "width": width, "height": height}
    with TemporaryDirectory(prefix=".preview-", dir=directory) as temporary:
        raw_path = Path(temporary) / "projection.png"
        heatmap_path = Path(temporary) / "difference.png"
        config = InfoObject()
        for key, value in (("alpha", True), ("forceSRGB", True), ("downsample", True),
                           ("storeColorSpaceInfo", True), ("indexed", False)):
            config.setProperty(key, value)
        batchmode = doc.batchmode()
        doc.waitForDone()
        try:
            doc.setBatchmode(True)
            if not doc.exportImage(str(raw_path), config):
                raise PreviewError("Krita PNG export failed")
        finally:
            doc.setBatchmode(batchmode)
        image = _read_image(raw_path, (width, height))
        if reference is not None:
            if not _heatmap(image, reference).save(str(heatmap_path), "PNG"):
                raise PreviewError("Could not save difference heatmap")
            result.update(reference_path=str(reference_path), diff_path=str(diff_path))
        if show_grid:
            _grid(image, spacing)
            if not image.save(str(raw_path), "PNG"):
                raise PreviewError("Could not save gridded preview")
        if reference is not None:
            os.replace(heatmap_path, diff_path)
        os.replace(raw_path, path)
    return result
