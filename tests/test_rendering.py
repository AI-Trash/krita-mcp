from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "krita-plugin" / "kritamcp"))
import rendering

QT_AVAILABLE = importlib.util.find_spec("PyQt5") is not None


class SamplingTests(unittest.TestCase):
    def test_straight_pressure_stroke_uses_fewer_bounded_segments(self) -> None:
        points = [(10, 20, .2), (410, 20, .8)]

        sampled = rendering.sample_stroke(points, smooth=False, pressure=.5)

        self.assertEqual(len(sampled) - 1, 128)
        self.assertEqual(sampled[0], (10, 20, .1))
        self.assertEqual(sampled[-1], (410, 20, .4))
        self.assertLessEqual(max(math.dist(first[:2], second[:2])
                                 for first, second in zip(sampled, sampled[1:])), 4)
        for horizontal, vertical, force in sampled:
            self.assertEqual(vertical, 20)
            self.assertAlmostEqual(force, .1 + (horizontal - 10) * .3 / 400)

    def test_four_pixel_chords_fit_spatial_sample_budget(self) -> None:
        points = [(0, 0), (64, 0)]

        with patch.object(rendering, "MAX_SAMPLES", 17):
            sampled = rendering.sample_stroke(points, smooth=False, pressure=.5)

        self.assertEqual(len(sampled), 17)
        self.assertEqual(sampled[-1], (64, 0, .5))

    def test_short_tapers_preserve_interior_pressure(self) -> None:
        for length in (.5, 2, 2.01, 3, 4):
            for begin, finish in ((.5, 0), (0, .5), (.5, .5)):
                with self.subTest(length=length, begin=begin, finish=finish):
                    points = [(0, 0, .8), (length, 0, .8)]

                    sampled = rendering.sample_stroke(points, smooth=False, pressure=.5,
                                                       taper_start=begin, taper_end=finish)

                    expected = [(0, 0, 0 if begin else .4)]
                    if length > 2:
                        expected.append((length / 2, 0, .4))
                    expected.append((length, 0, 0 if finish else .4))
                    self.assertEqual(sampled, expected)

    def test_bezier_deviation_and_pressure_errors_remain_bounded(self) -> None:
        points = [(0, 0, .2), (4 / 3, 8, 1), (8 / 3, -8, 0), (4, 0, .8)]

        sampled = rendering.sample_stroke(points, curve="bezier", pressure=.5)

        self.assertEqual(sampled[0], (0, 0, .1))
        self.assertEqual(sampled[-1], (4, 0, .4))
        for first, second in zip(sampled, sampled[1:]):
            self.assertLessEqual(math.dist(first[:2], second[:2]), 4)
            for fraction in (.25, .5, .75):
                horizontal, vertical, force = (start + (end - start) * fraction
                                                for start, end in zip(first, second))
                along = horizontal / 4
                self.assertLessEqual(abs(vertical - 24 * along * (1 - along) * (1 - 2 * along)), .2)
                expected = .5 * (.2 * (1 - along)**3 + 3 * (1 - along)**2 * along + .8 * along**3)
                self.assertLessEqual(abs(force - expected), .005 * .5)

    def test_pressure_multiplies_point_pressure(self) -> None:
        points = [(10, 20, .2), (20, 20, .8)]

        sampled = rendering.sample_stroke(points, smooth=False, pressure=.5)

        self.assertEqual(sampled[0], (10, 20, .1))
        self.assertEqual(sampled[-1], (20, 20, .4))
        self.assertTrue(all(abs(force - (.1 + (horizontal - 10) * .03)) < 1e-12
                            for horizontal, vertical, force in sampled))

    def test_taper_uses_arc_length_across_unequal_spans(self) -> None:
        points = [(0, 0), (0, 2), (8, 2)]

        sampled = rendering.sample_stroke(points, smooth=False, taper_start=.5, taper_end=.2)

        self.assertEqual(sampled[0][2], 0)
        self.assertEqual(sampled[-1][2], 0)
        self.assertAlmostEqual(next(point[2] for point in sampled if point[:2] == (0, 2)), .4)
        self.assertAlmostEqual(max(point[2] for point in sampled), 1)

    def test_bezier_sampling_follows_curve_and_cubic_pressure(self) -> None:
        points = [(10, 20, 0), (10, 40, 0), (30, 40, 1), (30, 20, 1)]

        sampled = rendering.sample_stroke(points, smooth=False, curve="bezier")

        self.assertEqual(sampled[0], (10, 20, 0))
        self.assertEqual(sampled[-1], (30, 20, 1))
        self.assertIn((20, 35, .5), sampled)
        self.assertLessEqual(max(math.dist(first[:2], second[:2])
                                 for first, second in zip(sampled, sampled[1:])), 4)

    def test_curved_taper_ends_at_exact_zero_despite_arc_length_rounding(self) -> None:
        points = [(100, 200, .2), (120, 180, .8), (140, 200, .4)]

        sampled = rendering.sample_stroke(points, taper_start=.2, taper_end=.2)

        self.assertEqual((sampled[0][2], sampled[-1][2]), (0, 0))

    def test_single_point_and_repeated_points_are_finite(self) -> None:
        points = [(20, 30, .4)] * 3

        sampled = rendering.sample_stroke(points, pressure=.5)

        self.assertTrue(sampled)
        self.assertTrue(all(point == (20, 30, .2) for point in sampled))

    def test_invalid_geometry_and_options_are_rejected(self) -> None:
        for points in ([], [(1,)], [(0, float("nan"))], [(float("inf"), 0)],
                       [(0, 0, -1)], [(0, 0, 1.1)], [(True, 1)], [(10**400, 0)], [(0, 0)] * 4097):
            with self.subTest(points=points[:2]), self.assertRaises(rendering.RenderError):
                rendering.sample_stroke(points)
        for options in ({"pressure": -1}, {"pressure": float("nan")}, {"taper_start": 2},
                        {"taper_end": float("inf")}, {"smooth": 1}, {"curve": "catmull-rom"}):
            with self.subTest(options=options), self.assertRaises(rendering.RenderError):
                rendering.sample_stroke([(0, 0), (1, 1)], **options)

    def test_adversarial_coordinates_raise_before_unbounded_sampling(self) -> None:
        points = [(-1e9, 0), (1e9, 0)]

        with self.assertRaises(rendering.RenderError):
            rendering.sample_stroke(points, smooth=False)


class SelectionTests(unittest.TestCase):
    def test_selection_is_per_region_coverage(self) -> None:
        paint, selection = bytes((200, 100, 50, 255)) * 3, bytes((0, 128, 255))

        result = rendering.apply_selection_bgra(paint, selection)

        self.assertEqual(result, bytes((0, 0, 0, 0, 200, 100, 50, 128, 200, 100, 50, 255)))

    def test_hidden_rgb_is_cleared_when_source_alpha_is_zero(self) -> None:
        paint, selection = bytes((7, 8, 9, 0)), bytes((255,))

        result = rendering.apply_selection_bgra(paint, selection)

        self.assertEqual(result, bytes(4))

    def test_hidden_rgb_survives_when_positive_coverage_rounds_alpha_to_zero(self) -> None:
        paint, selection = bytes((7, 8, 9, 1)), bytes((1,))

        result = rendering.apply_selection_bgra(paint, selection)

        self.assertEqual(result, bytes((7, 8, 9, 0)))

    def test_selection_matches_blank_source_over_for_every_alpha_and_mask(self) -> None:
        selection = bytes(range(256)) * 256
        for channels in ((0, 0, 0), (255, 255, 255), (1, 127, 254), (200, 100, 50)):
            with self.subTest(channels=channels):
                paint = b"".join(bytes((*channels, alpha)) for alpha in range(256) for _ in range(256))
                expected = bytearray()
                for alpha in range(256):
                    for mask in range(256):
                        coverage = alpha / 255 * (mask / 255)
                        expected.extend(bytes((*[round(channel * coverage / coverage) for channel in channels],
                                               round(255 * coverage))) if coverage else bytes(4))

                result = rendering.apply_selection_bgra(paint, selection)

                self.assertEqual(result, bytes(expected))

    def test_empty_patch_accepts_empty_selection(self) -> None:
        result = rendering.apply_selection_bgra(b"", b"")

        self.assertEqual(result, b"")

    def test_bytearray_and_memoryview_are_normalized(self) -> None:
        paint, selection = bytearray((200, 100, 50, 128)), memoryview(bytes((128,)))

        result = rendering.apply_selection_bgra(paint, selection)

        self.assertEqual(result, bytes((200, 100, 50, 64)))

    def test_misaligned_buffers_and_stale_selection_are_rejected(self) -> None:
        for paint, selection in ((b"a", b"a"), (bytes(8), bytes(1)), (bytes(4), bytes(2)),
                                 (bytes(4), b""), (b"", bytes(1)), ("abcd", bytes(1)),
                                 (bytes(4), "a"), (4, bytes(1)), (bytes(4), None)):
            with self.subTest(paint=paint, selection=selection), self.assertRaises(rendering.RenderError):
                rendering.apply_selection_bgra(paint, selection)

    def test_patch_at_four_million_pixel_budget_is_accepted(self) -> None:
        paint, selection = bytes(rendering.MAX_PIXELS * 4), bytes(rendering.MAX_PIXELS)

        result = rendering.apply_selection_bgra(paint, selection)

        self.assertEqual(result, paint)

    def test_buffers_over_four_million_pixel_budget_are_rejected(self) -> None:
        oversized = bytes(rendering.MAX_PIXELS * 4 + 4)
        for paint, selection in ((oversized, b""), (b"", oversized)):
            with self.subTest(paint_size=len(paint)), self.assertRaises(rendering.RenderError):
                rendering.apply_selection_bgra(paint, selection)


@unittest.skipUnless(QT_AVAILABLE, "Real PyQt5 is unavailable; no geometry substitute is used")
class QtRenderingTests(unittest.TestCase):
    def pixel(self, patch: rendering.Patch | None, horizontal: int, vertical: int) -> tuple[int, ...]:
        if patch is None:
            self.fail("Expected a visible polygon patch")
        left, top, width, height, pixels = patch
        self.assertTrue(left <= horizontal < left + width and top <= vertical < top + height)
        offset = ((vertical - top) * width + horizontal - left) * 4
        return tuple(pixels[offset:offset + 4])

    def test_default_curve_has_global_endpoints_and_cubic_controls(self) -> None:
        points = [(100, 200), (110, 220), (130, 200)]

        path = rendering.build_path(points)

        self.assertEqual((path.elementAt(0).x, path.elementAt(0).y), points[0])
        self.assertEqual((path.currentPosition().x(), path.currentPosition().y()), points[-1])
        self.assertTrue(path.elementAt(1).isCurveTo())
        self.assertAlmostEqual(path.elementAt(1).x, 100 + 10 / 6)
        self.assertAlmostEqual(path.elementAt(2).x, 110 - 30 / 6)

    def test_linear_override_wins_over_smooth(self) -> None:
        points = [(10, 20), (20, 40), (30, 20)]

        path = rendering.build_path(points, curve="linear", smooth=True)

        self.assertEqual(path.elementCount(), 3)
        self.assertTrue(path.elementAt(1).isLineTo())

    def test_bezier_override_wins_over_smooth_false(self) -> None:
        points = [(10, 20), (10, 40), (30, 40), (30, 20)]

        path = rendering.build_path(points, smooth=False, curve="bezier")

        self.assertEqual((path.pointAtPercent(.5).x(), path.pointAtPercent(.5).y()), (20, 35))
        self.assertEqual((path.currentPosition().x(), path.currentPosition().y()), (30, 20))

    def test_closed_smooth_path_wraps_at_first_knot(self) -> None:
        points = [(100, 100), (120, 100), (120, 120), (100, 120)]

        path = rendering.build_path(points, closed=True)

        self.assertEqual((path.currentPosition().x(), path.currentPosition().y()), points[0])
        self.assertEqual(path.elementCount(), 13)
        self.assertAlmostEqual(path.elementAt(1).y, 100 - 20 / 6)

    def test_invalid_bezier_controls_are_rejected(self) -> None:
        for count in (1, 2, 3, 5, 6):
            with self.subTest(count=count), self.assertRaises(rendering.RenderError):
                rendering.build_path([(0, 0)] * count, curve="bezier")
            with self.subTest(sampling=count), self.assertRaises(rendering.RenderError):
                rendering.sample_stroke([(0, 0)] * count, curve="bezier")

    def test_offset_polygon_returns_document_bbox_and_bgra(self) -> None:
        points = [(100, 200), (120, 200), (120, 220), (100, 220)]

        patch = rendering.polygon_patch(points, 300, 300, smooth=False, color=(30, 60, 90, 255))

        self.assertEqual(patch[:4], (99, 199, 22, 22))
        self.assertEqual(self.pixel(patch, 110, 210), (90, 60, 30, 255))
        self.assertEqual(self.pixel(patch, 99, 199)[3], 0)

    def test_antialiasing_produces_partial_edge_coverage(self) -> None:
        points = [(10.25, 10.25), (30.25, 10.25), (10.25, 30.25)]

        patch = rendering.polygon_patch(points, 50, 50, smooth=False)

        self.assertTrue(any(0 < alpha < 255 for alpha in patch[4][3::4]))

    def test_linear_gradient_uses_global_polygon_coordinates(self) -> None:
        points = [(100, 200), (120, 200), (120, 220), (100, 220)]

        patch = rendering.polygon_patch(points, 300, 300, smooth=False,
                                        color=(255, 0, 0, 255), gradient_end_color=(0, 0, 255, 255))

        self.assertGreater(self.pixel(patch, 102, 210)[2], self.pixel(patch, 117, 210)[2])
        self.assertLess(self.pixel(patch, 102, 210)[0], self.pixel(patch, 117, 210)[0])

    def test_radial_gradient_changes_from_center_to_edge(self) -> None:
        points = [(100, 200), (120, 200), (120, 220), (100, 220)]

        patch = rendering.polygon_patch(points, 300, 300, smooth=False, gradient_type="radial",
                                        color=(255, 0, 0, 255), gradient_end_color=(0, 0, 255, 255))

        self.assertGreater(self.pixel(patch, 110, 210)[2], self.pixel(patch, 118, 210)[2])

    def test_feather_extends_alpha_without_darkening_straight_color(self) -> None:
        points = [(20, 20), (40, 20), (40, 40), (20, 40)]

        patch = rendering.polygon_patch(points, 60, 60, smooth=False, color=(255, 0, 0, 255), feather=3)

        edge = self.pixel(patch, 19, 30)
        self.assertTrue(0 < edge[3] < 255)
        self.assertEqual(edge[:3], (0, 0, 255))

    def test_clipped_feather_matches_same_document_region(self) -> None:
        points = [(-2, -2), (12, -2), (12, 12), (-2, 12)]
        translated = [(horizontal + 20, vertical + 20) for horizontal, vertical in points]
        reference = rendering.polygon_patch(translated, 60, 60, smooth=False, feather=3)

        clipped = rendering.polygon_patch(points, 10, 10, smooth=False, feather=3)

        self.assertEqual(clipped[:4], (0, 0, 10, 10))
        self.assertEqual([self.pixel(clipped, horizontal, vertical) for vertical in range(10) for horizontal in range(10)],
                         [self.pixel(reference, horizontal + 20, vertical + 20) for vertical in range(10) for horizontal in range(10)])

    def test_smooth_polygon_uses_curved_closed_fill(self) -> None:
        points = [(20, 20), (40, 20), (40, 40), (20, 40)]

        patch = rendering.polygon_patch(points, 60, 60)

        self.assertGreater(self.pixel(patch, 30, 19)[3], 0)

    def test_off_canvas_polygon_returns_none(self) -> None:
        points = [(100, 200), (120, 200), (120, 220)]

        patch = rendering.polygon_patch(points, 10, 10)

        self.assertIsNone(patch)

    def test_invalid_fill_options_and_oversized_rasters_are_rejected(self) -> None:
        points = [(0, 0), (10000, 0), (10000, 10000)]
        for options in ({"color": (0, 0, 0, 256)}, {"color": (0, 0, 0)},
                        {"gradient_end_color": (0, 0, -1, 255)}, {"feather": -1},
                        {"direction": float("nan")}, {"gradient_type": "unknown"}, {}):
            with self.subTest(options=options), self.assertRaises(rendering.RenderError):
                rendering.polygon_patch(points, 10000, 10000, **options)

    def test_qbytearray_is_converted_before_selection_clipping(self) -> None:
        from PyQt5.QtCore import QByteArray
        paint, selection = QByteArray(bytes((200, 100, 50, 255))), QByteArray(bytes((128,)))

        result = rendering.apply_selection_bgra(paint, selection)

        self.assertEqual(result, bytes((200, 100, 50, 128)))


if __name__ == "__main__":
    unittest.main()
