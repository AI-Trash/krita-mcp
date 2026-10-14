from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Literal, assert_never
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "krita-plugin" / "kritamcp"))
sys.path.insert(0, "/Applications/krita.app/Contents/Frameworks/PyKrita.framework/Versions/5.3.4.1/lib")

from PyQt5.QtGui import QColor, QColorSpace, QGuiApplication, QImage
from PyKrita.krita import InfoObject
import preview


class ImageDocument:
    def __init__(self, image: QImage) -> None:
        self.image = image
        self.save_name = "source.kra"
        self.batch = False
        self.exports = 0
        self.mode: Literal["normal", "failure", "corrupt", "missing", "exception"] = "normal"
        self.layers = ("paint", "background")
        self.selection = b"selection coverage"
        self.history = ("brush stroke",)

    def width(self) -> int:
        return self.image.width()

    def height(self) -> int:
        return self.image.height()

    def fileName(self) -> str:
        return self.save_name

    def waitForDone(self) -> None:
        self.image.detach()

    def batchmode(self) -> bool:
        return self.batch

    def setBatchmode(self, value: bool) -> None:
        self.batch = value

    def exportImage(self, filename: str, exportConfiguration: InfoObject) -> bool:
        self.exports += 1
        if not self.batch or not exportConfiguration.property("alpha"):
            return False
        match self.mode:
            case "normal":
                return self.image.save(filename, "PNG")
            case "failure":
                return False
            case "corrupt":
                Path(filename).write_bytes(b"not an image")
                return True
            case "missing":
                return True
            case "exception":
                raise OSError("export device failure")
            case unreachable:
                assert_never(unreachable)


class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QGuiApplication.instance() or QGuiApplication([])

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "output"
        source = QImage(240, 180, QImage.Format_RGBA8888)
        source.fill(QColor(20, 40, 60, 128))
        source.setColorSpace(QColorSpace(QColorSpace.SRgb))
        self.doc = ImageDocument(source)
        native_module = importlib.import_module("PyKrita.krita")
        module_patch = patch.dict(sys.modules, {"krita": native_module})
        module_patch.start()
        self.addCleanup(module_patch.stop)

    def reference(self, image: QImage | None = None) -> Path:
        path = self.root / "reference.png"
        self.assertTrue((self.doc.image if image is None else image).save(str(path), "PNG"))
        return path

    def test_projection_when_plain_export_preserves_rgba(self) -> None:
        source = self.doc.image.copy()

        result = preview.export_preview(self.doc, "canvas", output_dir=self.output)

        self.assertEqual(result, {"path": str(self.output / "canvas.png"), "width": 240, "height": 180})
        self.assertEqual(QImage(result["path"]).convertToFormat(QImage.Format_RGBA8888), source)
        self.assertEqual(list(self.output.iterdir()), [self.output / "canvas.png"])

    def test_grid_when_enabled_changes_preview_only_with_visible_origin_and_axis_labels(self) -> None:
        source = self.doc.image.copy()
        state = (self.doc.save_name, self.doc.layers, self.doc.selection, self.doc.history)

        result = preview.export_preview(self.doc, "grid.png", output_dir=self.output, show_grid=True)

        image = QImage(result["path"])
        self.assertNotEqual(image, source)
        self.assertEqual(self.doc.image, source)
        self.assertEqual((self.doc.save_name, self.doc.layers, self.doc.selection, self.doc.history), state)
        self.assertGreater(image.pixelColor(100, 80).alpha(), source.pixelColor(100, 80).alpha())
        for left, top in ((3, 3), (103, 3), (3, 103)):
            self.assertTrue(any(image.pixelColor(horizontal, vertical).red() > 230
                                for vertical in range(top, top + 15)
                                for horizontal in range(left, left + 28)))

    def test_heatmap_when_identical_reference_remains_black_despite_grid(self) -> None:
        reference = self.reference()

        result = preview.export_preview(self.doc, "grid.png", output_dir=self.output,
                                        show_grid=True, diff_with_reference=reference)

        heatmap = QImage(result["diff_path"])
        self.assertEqual(result["reference_path"], str(reference))
        self.assertTrue(all(heatmap.pixelColor(horizontal, vertical) == QColor(0, 0, 0, 255)
                            for vertical in range(180) for horizontal in range(240)))
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_heatmap_when_rgb_or_alpha_differs_is_visible_and_local(self) -> None:
        changed = self.doc.image.copy()
        changed.setPixelColor(50, 50, QColor(21, 40, 60, 128))
        changed.setPixelColor(60, 60, QColor(20, 40, 60, 0))
        changed.setPixelColor(80, 80, QColor(20, 41, 60, 128))
        changed.setPixelColor(90, 90, QColor(20, 40, 61, 128))
        reference = self.reference(changed)

        result = preview.export_preview(self.doc, "diff.png", output_dir=self.output,
                                        diff_with_reference=reference)

        heatmap = QImage(result["diff_path"])
        self.assertGreaterEqual(heatmap.pixelColor(50, 50).red(), 48)
        self.assertGreaterEqual(heatmap.pixelColor(60, 60).red(), 128)
        self.assertGreaterEqual(heatmap.pixelColor(80, 80).red(), 48)
        self.assertGreaterEqual(heatmap.pixelColor(90, 90).red(), 48)
        self.assertEqual(heatmap.pixelColor(70, 70), QColor(0, 0, 0, 255))
        self.assertEqual(QImage(result["path"]).convertToFormat(QImage.Format_RGBA8888), self.doc.image)

    def test_reference_when_dimensions_mismatch_is_rejected_before_export(self) -> None:
        image = QImage(239, 180, QImage.Format_RGBA8888)
        image.fill(0)
        reference = self.reference(image)

        with self.assertRaisesRegex(preview.PreviewError, "exactly 240x180"):
            preview.export_preview(self.doc, "preview.png", output_dir=self.output, diff_with_reference=reference)

        self.assertEqual(self.doc.exports, 0)

    def test_reference_when_missing_invalid_or_directory_is_rejected(self) -> None:
        invalid = self.root / "invalid.png"
        invalid.write_bytes(b"invalid PNG")
        for reference in (self.root / "missing.png", invalid, self.root):
            with self.subTest(reference=reference), self.assertRaises(preview.PreviewError):
                preview.export_preview(self.doc, "preview.png", output_dir=self.output, diff_with_reference=reference)
        self.assertEqual(self.doc.exports, 0)

    def test_filename_when_traversal_absolute_or_wrong_extension_is_rejected(self) -> None:
        for name in ("../escape.png", "/tmp/escape.png", "sub/escape.png", "..\\escape.png",
                     "C:\\escape.png", "", ".", "..", "bad\x00.png", "canvas.jpg"):
            with self.subTest(name=name), self.assertRaises(preview.PreviewError):
                preview.export_preview(self.doc, name, output_dir=self.output)
        self.assertEqual(self.doc.exports, 0)

    def test_spacing_when_nonfinite_fractional_or_out_of_bounds_is_rejected(self) -> None:
        for spacing in (0, -1, 7, 8.5, True, float("nan"), float("inf"), 10**400):
            with self.subTest(spacing=spacing), self.assertRaises(preview.PreviewError):
                preview.export_preview(self.doc, "preview.png", output_dir=self.output, grid_spacing=spacing)
        self.assertEqual(self.doc.exports, 0)

    def test_export_when_failed_missing_corrupt_or_exception_restores_batch_and_cleans_temporary(self) -> None:
        for mode in ("failure", "missing", "corrupt", "exception"):
            for batch in (False, True):
                self.doc.mode, self.doc.batch = mode, batch
                with self.subTest(mode=mode, batch=batch), self.assertRaises((preview.PreviewError, OSError)):
                    preview.export_preview(self.doc, "preview.png", output_dir=self.output)
                self.assertEqual(self.doc.batch, batch)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_failed_export_when_previous_preview_exists_leaves_it_unchanged(self) -> None:
        self.output.mkdir()
        target = self.output / "preview.png"
        target.write_bytes(b"previous output")
        self.doc.mode = "failure"

        with self.assertRaises(preview.PreviewError):
            preview.export_preview(self.doc, "preview.png", output_dir=self.output)

        self.assertEqual(target.read_bytes(), b"previous output")

    def test_heatmap_when_png_write_fails_reports_failure_and_cleans_up(self) -> None:
        reference = self.reference()
        save = QImage.save

        def save_except_heatmap(image: QImage, filename: str, image_format: str) -> bool:
            return False if Path(filename).name == "difference.png" else save(image, filename, image_format)

        with patch.object(QImage, "save", save_except_heatmap), self.assertRaisesRegex(preview.PreviewError, "save difference"):
            preview.export_preview(self.doc, "preview.png", output_dir=self.output, diff_with_reference=reference)

        self.assertEqual(list(self.output.iterdir()), [])

    def test_output_when_aliases_source_or_reference_is_rejected(self) -> None:
        reference = self.reference()
        self.doc.save_name = str(self.root / "source.png")
        with self.assertRaisesRegex(preview.PreviewError, "source document"):
            preview.export_preview(self.doc, "source.png", output_dir=self.root)
        with self.assertRaisesRegex(preview.PreviewError, "reference image"):
            preview.export_preview(self.doc, "reference.png", output_dir=self.root, diff_with_reference=reference)
        self.assertEqual(self.doc.exports, 0)

    def test_symlink_when_output_points_outside_directory_replaces_link_not_reference(self) -> None:
        reference = self.reference()
        original = reference.read_bytes()
        self.output.mkdir()
        (self.output / "preview.png").symlink_to(reference)

        result = preview.export_preview(self.doc, "preview.png", output_dir=self.output, show_grid=True)

        self.assertEqual(reference.read_bytes(), original)
        self.assertFalse(Path(result["path"]).is_symlink())

    def test_limits_when_large_document_dense_grid_or_tiny_labels_fail(self) -> None:
        with self.assertRaises(preview.PreviewError):
            preview._dimensions(4001, 4000)
        wide = QImage(32768, 1, QImage.Format_RGBA8888)
        with self.assertRaisesRegex(preview.PreviewError, "2048-line"):
            preview.export_preview(ImageDocument(wide), "preview.png", output_dir=self.output,
                                   show_grid=True, grid_spacing=8)
        tiny = QImage(2, 2, QImage.Format_RGBA8888)
        tiny.fill(0)
        with self.assertRaisesRegex(preview.PreviewError, "too small"):
            preview.export_preview(ImageDocument(tiny), "preview.png", output_dir=self.output, show_grid=True)
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
