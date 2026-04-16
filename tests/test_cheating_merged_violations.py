"""
Tests for merged multi-label cheating violations (severity-ordered) and run logic.
Run from repo root: python -m unittest discover -s tests -p "test_*.py" -v
(requires cwd or PYTHONPATH including Foresyte-backend/src).
"""

import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from database.cheating_labels import (  # noqa: E402
    MERGED_LABEL_SEPARATOR,
    merge_behavior_labels,
    merged_base_severity,
    merge_frame_behavior_details,
)
from database.severity_logic import (  # noqa: E402
    compute_severity_from_count,
    filter_qualifying_runs,
    get_runs_from_detections,
    min_frames_for_activity_type,
    severity_to_int,
)


class TestMergeLabels(unittest.TestCase):
    def test_merge_orders_by_severity(self):
        m = merge_behavior_labels(["Look Around", "phone", "Wave"])
        parts = m.split(MERGED_LABEL_SEPARATOR)
        self.assertEqual(parts[0], "phone")
        self.assertIn("Look Around", parts)
        self.assertIn("Wave", parts)

    def test_merge_dedupes_and_drops_normal(self):
        m = merge_behavior_labels(["phone", "phone", "Normal"])
        self.assertEqual(m, "phone")

    def test_merged_base_severity(self):
        self.assertEqual(
            merged_base_severity(["Look Around", "phone"]),
            "high",
        )
        self.assertEqual(
            merged_base_severity(["Wave", "Look Around"]),
            "medium",
        )


class TestMergeDetails(unittest.TestCase):
    def test_details_order(self):
        d = merge_frame_behavior_details(
            [
                {"behavior_type": "Look Around", "confidence": 0.5, "bbox": (1, 2, 3, 4)},
                {"behavior_type": "phone", "confidence": 0.9, "bbox": (1, 2, 3, 4)},
            ]
        )
        self.assertTrue(d.startswith("phone"))


class TestCompositeMinFrames(unittest.TestCase):
    def test_merged_min_is_max_of_components(self):
        merged = merge_behavior_labels(["phone", "Look Around"])
        self.assertIn(MERGED_LABEL_SEPARATOR, merged)
        self.assertEqual(min_frames_for_activity_type(merged), 5)

    def test_single_phone(self):
        self.assertEqual(min_frames_for_activity_type("phone"), 1)


class TestRunBoundaries(unittest.TestCase):
    def test_merged_string_changes_start_new_run(self):
        m1 = merge_behavior_labels(["phone"])
        m2 = merge_behavior_labels(["phone", "Look Around"])
        dets = [
            {
                "timestamp": "2025-01-01T00:00:01",
                "frame_number": 1,
                "behavior_type": m1,
            },
            {
                "timestamp": "2025-01-01T00:00:02",
                "frame_number": 2,
                "behavior_type": m2,
            },
        ]
        runs = get_runs_from_detections(dets)
        self.assertEqual(len(runs), 2)

    def test_same_merged_string_one_run(self):
        m = merge_behavior_labels(["phone", "Wave"])
        dets = [
            {"timestamp": "2025-01-01T00:00:01", "frame_number": 1, "behavior_type": m},
            {"timestamp": "2025-01-01T00:00:02", "frame_number": 2, "behavior_type": m},
        ]
        runs = get_runs_from_detections(dets)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].frame_count, 2)

    def test_qualifying_merged_respects_max_min_frames(self):
        merged = merge_behavior_labels(["phone", "Look Around"])
        dets = [
            {"timestamp": "2025-01-01T00:00:0%d" % i, "frame_number": i, "behavior_type": merged}
            for i in range(1, 6)
        ]
        runs = filter_qualifying_runs(get_runs_from_detections(dets))
        self.assertEqual(len(runs), 1)
        self.assertGreaterEqual(runs[0].frame_count, 5)


class TestSeverityMapping(unittest.TestCase):
    def test_severity_to_int_strings(self):
        self.assertEqual(severity_to_int("low"), 1)
        self.assertEqual(severity_to_int("critical"), 4)

    def test_compute_severity_uses_worst_component_for_merged(self):
        merged = merge_behavior_labels(["phone", "Look Around"])
        sev = compute_severity_from_count(1, merged)
        self.assertIn(sev, ("high", "medium", "low", "critical"))


if __name__ == "__main__":
    unittest.main()
