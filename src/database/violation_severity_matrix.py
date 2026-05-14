"""
Curated major-infraction combinations with duration-based severity tiers.

Each key is the canonical `behavior_type` string (single label or merged with `"; "`).
Values are (min_consecutive_frames, severity) pairs, same semantics as
`ACTIVITY_SEVERITY_CONFIG`: for run length N, the applied severity is the last
pair where N >= min_frames.

Any activity_type not listed falls back to `compute_severity_from_count` (legacy).
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from database.cheating_labels import (
    is_merged_activity_type,
    merge_behavior_labels,
    split_merged_activity,
)
from database.severity_logic import compute_severity_from_count

# (min_frames_inclusive, severity) — ascending min_frames; highest matching tier wins.
CURATED_SEVERITY_MATRIX: Dict[str, List[Tuple[int, str]]] = {
    # Singles (device / concealment)
    "phone": [(1, "high"), (6, "critical")],
    "Hand Under Table": [(1, "medium"), (4, "high"), (10, "critical")],
    # Pairs — stricter than lone behaviours once sustained
    "phone; Hand Under Table": [(1, "high"), (3, "critical")],
    "phone; Look Around": [(1, "high"), (5, "critical")],
    "phone; Bend Over The Desk": [(1, "high"), (4, "critical")],
    "Hand Under Table; Look Around": [(1, "medium"), (3, "high"), (8, "critical")],
    "Hand Under Table; Bend Over The Desk": [(1, "medium"), (3, "high"), (8, "critical")],
    "Hand Under Table; Stand Up": [(1, "high"), (4, "critical")],
    "Hand Under Table; Wave": [(1, "medium"), (3, "high"), (8, "critical")],
    "Hand Under Table; Hand Under Table": [(1, "medium"), (3, "high"), (8, "critical")],
    "Hand Under Table; Hand Under Table; Look Around": [(1, "medium"), (3, "high"), (8, "critical")],
    "Hand Under Table; Hand Under Table; Bend Over The Desk": [(1, "medium"), (3, "high"), (8, "critical")],
    "Hand Under Table; Hand Under Table; Stand Up": [(1, "high"), (4, "critical")],
    "Hand Under Table; Hand Under Table; Wave": [(1, "medium"), (3, "high"), (8, "critical")],
    # Triple: Hand Under Table + Look Around + Bend Over The Desk (canonical merge order)
    "Hand Under Table; Bend Over The Desk; Look Around": [(1, "high"), (2, "critical")],
    "phone; Hand Under Table; Look Around": [(1, "high"), (2, "critical")],
    "phone; Hand Under Table; Bend Over The Desk": [(1, "high"), (2, "critical")],
    "phone; Hand Under Table; Stand Up": [(1, "high"), (2, "critical")],
}


def canonical_matrix_key(activity_type: str) -> str:
    """Normalize to the same string produced by merge_behavior_labels for lookup."""
    raw = (activity_type or "").strip()
    if not raw:
        return ""
    if is_merged_activity_type(raw):
        parts = split_merged_activity(raw)
        return merge_behavior_labels(parts)
    return raw


def matrix_severity_for_frames(matrix_key: str, frame_count: int) -> str | None:
    """Return severity from matrix if key exists; else None."""
    thresholds = CURATED_SEVERITY_MATRIX.get(matrix_key)
    if not thresholds:
        return None
    n = max(1, int(frame_count))
    severity = "low"
    for min_frames, sev in thresholds:
        if n >= min_frames:
            severity = sev
    return severity


def resolve_activity_severity(activity_type: str, frame_count: int) -> tuple[str, str]:
    """
    Resolve severity for a qualifying run.

    Returns:
        (severity_string, rule_source) where rule_source is \"matrix\" or \"legacy\".
    """
    key = canonical_matrix_key(activity_type)
    msev = matrix_severity_for_frames(key, frame_count)
    if msev is not None:
        return msev, "matrix"
    return compute_severity_from_count(frame_count, activity_type), "legacy"
