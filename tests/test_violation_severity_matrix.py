"""Tests for curated combination + duration severity matrix."""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import unittest  # noqa: E402

from database.cheating_labels import merge_behavior_labels  # noqa: E402
from database.violation_severity_matrix import (  # noqa: E402
    CURATED_SEVERITY_MATRIX,
    canonical_matrix_key,
    matrix_severity_for_frames,
    resolve_activity_severity,
)


class TestCanonicalKey(unittest.TestCase):
    def test_reorders_merged_string(self):
        raw = "Hand Under Table; phone"
        key = canonical_matrix_key(raw)
        self.assertEqual(key, merge_behavior_labels(["Hand Under Table", "phone"]))


class TestMatrixVsLegacy(unittest.TestCase):
    def test_phone_combo_escalates_with_duration(self):
        sev1, src1 = resolve_activity_severity("phone; Hand Under Table", 2)
        self.assertEqual(src1, "matrix")
        self.assertEqual(sev1, "high")
        sev2, src2 = resolve_activity_severity("phone; Hand Under Table", 5)
        self.assertEqual(src2, "matrix")
        self.assertEqual(sev2, "critical")

    def test_unknown_combo_uses_legacy(self):
        sev, src = resolve_activity_severity("Suspicious Movement", 10)
        self.assertEqual(src, "legacy")
        self.assertIn(sev, ("low", "medium", "high", "critical"))


class TestMatrixKeys(unittest.TestCase):
    def test_all_matrix_keys_are_canonical(self):
        for k in CURATED_SEVERITY_MATRIX:
            self.assertEqual(k, canonical_matrix_key(k), msg=f"non-canonical key: {k!r}")


if __name__ == "__main__":
    unittest.main()
