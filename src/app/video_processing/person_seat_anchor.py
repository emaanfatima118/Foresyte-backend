"""
Option A: full-frame behaviour detections unchanged; use a base YOLO person pass
only to pick a better bbox for seat polygon lookup (IoU / containment / nearest).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

_AI_ENGINE_DIR = Path(__file__).resolve().parent.parent / "ai_engine"
try:
    from dotenv import load_dotenv

    load_dotenv(_AI_ENGINE_DIR / ".env")
except Exception:
    pass

_person_model = None
_person_path_loaded: str | None = None
_anchor_disabled_logged = False
_model_load_error_logged = False


def _resolve_person_model_path(path: str) -> str:
    p = Path(path.strip())
    if not p.is_absolute():
        p = _AI_ENGINE_DIR / p
    return str(p.resolve())


def _iou_xyxy(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter <= 0:
        return 0.0
    aa = (a[2] - a[0]) * (a[3] - a[1])
    bb = (b[2] - b[0]) * (b[3] - b[1])
    denom = aa + bb - inter
    return inter / denom if denom > 0 else 0.0


def _person_boxes_from_frame(
    frame: np.ndarray,
    *,
    conf: float,
    imgsz: int,
) -> list[tuple[int, int, int, int]]:
    global _person_model, _person_path_loaded, _model_load_error_logged
    path = _resolve_person_model_path(
        os.getenv("PERSON_MODEL_PATH", "yolov8n.pt")
    )
    try:
        from ultralytics import YOLO
    except ImportError:
        if not _model_load_error_logged:
            log.warning("ultralytics not installed — person seat anchor skipped")
            _model_load_error_logged = True
        return []

    if _person_model is None or _person_path_loaded != path:
        try:
            _person_model = YOLO(path)
            _person_path_loaded = path
            log.info("Person model for seat anchoring: %s", path)
        except Exception as e:
            if not _model_load_error_logged:
                log.warning("Could not load person model for seat anchor: %s", e)
                _model_load_error_logged = True
            return []

    h, w = frame.shape[:2]
    out: list[tuple[int, int, int, int]] = []
    try:
        for result in _person_model(
            frame,
            conf=conf,
            classes=[0],
            imgsz=min(imgsz, max(h, w, 640)),
            verbose=False,
        ):
            if not result.boxes or len(result.boxes) == 0:
                continue
            xyxy = result.boxes.xyxy.cpu().numpy()
            for row in xyxy:
                x1, y1, x2, y2 = map(int, row.tolist())
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(w, x2), min(h, y2)
                if x2 > x1 and y2 > y1:
                    out.append((x1, y1, x2, y2))
    except Exception as e:
        log.debug("Person predict failed for seat anchor: %s", e)
        return []
    return out


def _anchor_one_behavior(
    beh: tuple[int, int, int, int],
    persons: list[tuple[int, int, int, int]],
    *,
    min_iou: float,
    max_nearest_frac: float,
    frame_shape: tuple[int, ...],
) -> tuple[int, int, int, int]:
    if not persons:
        return beh
    fh, fw = frame_shape[0], frame_shape[1]
    diag = (fw * fw + fh * fh) ** 0.5
    max_dist = max_nearest_frac * diag

    best_iou = 0.0
    best_p: tuple[int, int, int, int] | None = None
    for p in persons:
        i = _iou_xyxy(beh, p)
        if i > best_iou:
            best_iou, best_p = i, p
    if best_p is not None and best_iou >= min_iou:
        return best_p

    cx = (beh[0] + beh[2]) / 2.0
    cy = (beh[1] + beh[3]) / 2.0
    for p in persons:
        if p[0] <= cx <= p[2] and p[1] <= cy <= p[3]:
            return p

    bcx, bcy = cx, cy
    best_d = 1e18
    nearest = None
    for p in persons:
        pcx = (p[0] + p[2]) / 2.0
        pcy = (p[1] + p[3]) / 2.0
        d = ((pcx - bcx) ** 2 + (pcy - bcy) ** 2) ** 0.5
        if d < best_d:
            best_d, nearest = d, p
    if nearest is not None and best_d <= max_dist:
        return nearest

    return beh


def attach_person_anchors_for_seats(
    frame: np.ndarray,
    student_behaviors: list[dict[str, Any]],
) -> None:
    """
    Mutates each behavior dict: sets '_seat_bbox' to a person-aligned box for
    SeatMapper when possible; otherwise copies 'bbox'.
    """
    global _anchor_disabled_logged
    if not student_behaviors:
        return
    flag = os.getenv("PERSON_SEAT_ANCHOR", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        if not _anchor_disabled_logged:
            log.info("Person seat anchor disabled (PERSON_SEAT_ANCHOR=0)")
            _anchor_disabled_logged = True
        for b in student_behaviors:
            bb = b.get("bbox")
            if bb and "_seat_bbox" not in b:
                b["_seat_bbox"] = tuple(map(int, bb))
        return

    conf = float(os.getenv("PERSON_SEAT_CONF", "0.35"))
    imgsz = int(os.getenv("PERSON_SEAT_IMGSZ", "960"))
    min_iou = float(os.getenv("PERSON_SEAT_MIN_IOU", "0.05"))
    max_near = float(os.getenv("PERSON_SEAT_MAX_NEAREST_FRAC", "0.28"))

    persons = _person_boxes_from_frame(frame, conf=conf, imgsz=imgsz)
    for b in student_behaviors:
        bb = b.get("bbox")
        if not bb or len(bb) != 4:
            continue
        beh_t = tuple(map(int, bb))
        b["_seat_bbox"] = _anchor_one_behavior(
            beh_t,
            persons,
            min_iou=min_iou,
            max_nearest_frac=max_near,
            frame_shape=frame.shape,
        )
