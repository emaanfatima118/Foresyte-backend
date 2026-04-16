"""
Adapter: ForeSyte full-frame behaviour detection (uploads/frames/detect.py)
integrated with video processing.

Converts ClassificationResult rows to the behavior format expected by VideoProcessor.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from .foresyte_detect_pipeline import ForesyteDetectConfig, run_foresyte_on_image
from .run_detection import ClassificationResult
from database.cheating_labels import LABEL_SEVERITY

log = logging.getLogger(__name__)


def run_behaviour_on_frame(
    image: np.ndarray,
    cfg: ForesyteDetectConfig | None = None,
) -> tuple[list[ClassificationResult], np.ndarray]:
    """
    Run behaviour detection on one BGR frame (full image, single model pass stack).
    """
    return run_foresyte_on_image(image, cfg or ForesyteDetectConfig.from_env())


def process_frame(
    frame: np.ndarray,
    frame_number: int,
    timestamp,
    seat_mapping: dict | None = None,
    cfg: ForesyteDetectConfig | None = None,
    return_annotated: bool = False,
) -> dict[str, Any]:
    """
    Process a single extracted frame through full-frame behaviour detection.

    Args:
        frame: BGR image (numpy array from cv2)
        frame_number: Frame index in video
        timestamp: datetime of the frame
        seat_mapping: Optional bbox -> seat_id mapping (for future use)
        cfg: Optional ForesyteDetectConfig (defaults from env)
        return_annotated: If True, include 'annotated_frame' when there are behaviors

    Returns:
        dict with keys: student_behaviors, invigilator_behaviors, and optionally annotated_frame
    """
    results, annotated = run_behaviour_on_frame(frame, cfg=cfg)

    student_behaviors = []
    for r in results:
        if not r.is_suspicious:
            continue

        severity = LABEL_SEVERITY.get(r.label, "medium")
        student_behaviors.append({
            "behavior_type": r.label,
            "severity": severity,
            "confidence": float(r.confidence),
            "details": f"bbox={r.bbox}, student_index={r.student_index}",
            "bbox": r.bbox,
            "student_index": r.student_index,
        })

    invigilator_behaviors = []

    out = {
        "student_behaviors": student_behaviors,
        "invigilator_behaviors": invigilator_behaviors,
    }
    if return_annotated and (student_behaviors or invigilator_behaviors):
        out["annotated_frame"] = annotated
    return out


def map_detection_to_seat(behavior: dict, seat_mapping: dict | None) -> str | None:
    """
    Map a detection (bbox) to a seat_id using seat_mapping.
    For use when seat mapping is available (e.g. from seating plan overlay).

    Args:
        behavior: Behavior dict with 'bbox' key
        seat_mapping: Mapping from bbox or region to seat_id (format TBD)

    Returns:
        seat_id (UUID string) or None if no mapping
    """
    if not seat_mapping:
        return None
    bbox = behavior.get("bbox")
    if not bbox:
        return None
    return seat_mapping.get(tuple(bbox))
