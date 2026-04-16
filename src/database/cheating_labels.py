"""
Canonical ordering and merge helpers for the 7-class cheating behaviour model.

Used when multiple suspicious labels appear for the same student on the same frame:
merge into one severity-ordered `behavior_type` string (e.g. "phone; Hand Under Table").
"""

from __future__ import annotations

from typing import Any

# Separator must match severity_logic merged-activity detection.
MERGED_LABEL_SEPARATOR = "; "

# Per-label base severity (StudentActivity / display); aligns with model semantics.
LABEL_SEVERITY: dict[str, str] = {
    "phone": "high",
    "Hand Under Table": "high",
    "Bend Over The Desk": "medium",
    "Stand Up": "high",
    "Wave": "medium",
    "Look Around": "medium",
    "Normal": "low",
}

# Higher rank = more severe, listed first in merged strings. Tie-breaks are stable.
CHEATING_LABEL_RANK: dict[str, int] = {
    "phone": 60,
    "Hand Under Table": 59,
    "Stand Up": 58,
    "Bend Over The Desk": 45,
    "Wave": 44,
    "Look Around": 43,
    "Normal": 0,
}

SUPPORTED_CHEATING_LABELS: set[str] = set(CHEATING_LABEL_RANK.keys()) - {"Normal"}

_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def is_merged_activity_type(activity_type: str) -> bool:
    """True if this activity string is a merged multi-label value."""
    s = (activity_type or "").strip()
    return MERGED_LABEL_SEPARATOR in s


def split_merged_activity(activity_type: str) -> list[str]:
    """Split a merged `behavior_type` into trimmed component labels."""
    s = (activity_type or "").strip()
    if not s:
        return []
    return [p.strip() for p in s.split(MERGED_LABEL_SEPARATOR) if p.strip()]


def is_supported_cheating_activity_type(activity_type: str) -> bool:
    """True if activity_type is one of the 7 model labels or a merged combination of them."""
    s = (activity_type or "").strip()
    if not s:
        return False
    if s in SUPPORTED_CHEATING_LABELS:
        return True
    if not is_merged_activity_type(s):
        return False
    parts = split_merged_activity(s)
    return bool(parts) and all(p in SUPPORTED_CHEATING_LABELS for p in parts)


def merge_behavior_labels(labels: list[str]) -> str:
    """
    Dedupe, drop Normal/empty, sort by severity rank (descending), join with MERGED_LABEL_SEPARATOR.
    Preserves exact model spellings for stored activity_type.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for raw in labels:
        lab = (raw or "").strip()
        if not lab or lab == "Normal":
            continue
        if lab in seen:
            continue
        seen.add(lab)
        unique.append(lab)
    if not unique:
        return ""
    unique.sort(key=lambda x: (-CHEATING_LABEL_RANK.get(x, -1), x))
    return MERGED_LABEL_SEPARATOR.join(unique)


def merged_base_severity(labels: list[str]) -> str:
    """Max base severity across component labels (low < medium < high < critical)."""
    best = "low"
    best_i = 1
    for raw in labels:
        lab = (raw or "").strip()
        if not lab or lab == "Normal":
            continue
        sev = LABEL_SEVERITY.get(lab, "medium")
        i = _SEVERITY_ORDER.get(sev, 1)
        if i > best_i:
            best_i = i
            best = sev
    return best


def merge_frame_behavior_details(behaviors: list[dict[str, Any]]) -> str:
    """Human-readable breakdown for DB details (components sorted by severity rank)."""
    if not behaviors:
        return ""
    ranked = sorted(
        behaviors,
        key=lambda b: (
            -CHEATING_LABEL_RANK.get((b.get("behavior_type") or "").strip(), -1),
            b.get("behavior_type") or "",
        ),
    )
    parts: list[str] = []
    for b in ranked:
        bt = b.get("behavior_type") or ""
        conf = float(b.get("confidence") or 0.0)
        bbox = b.get("bbox")
        parts.append(f"{bt} conf={conf:.2f} bbox={bbox}")
    return " | ".join(parts)
