"""
First-frame person→seat mapping (YOLO person + greedy scoring) for exam video pipeline.

Used by SeatMapper.install_first_frame_anchors(); tunables match the seat_mapping_lab
sandbox via FFMAP_* and PERSON_MODEL_PATH env vars.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from ultralytics import YOLO

logger = logging.getLogger(__name__)


@dataclass
class PersonDetection:
    person_key: str
    bbox: tuple[int, int, int, int]
    confidence: float
    center: tuple[float, float]


def _load_seat_map(seat_map_path: str) -> dict[str, list[list[float]]]:
    with open(seat_map_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    seats = raw.get("seats", {}) if isinstance(raw, dict) else {}
    return {k: v for k, v in seats.items() if isinstance(v, list) and len(v) >= 3}


def _extract_seat_col(seat_key: str) -> Optional[int]:
    m = re.search(r"seat_c(\d+)r\d+", (seat_key or "").lower())
    if not m:
        return None
    return int(m.group(1))


def _filter_seats_by_room_column_mapping(
    seats: dict[str, list[list[float]]],
    *,
    room_no: Optional[str],
    seat_plan_max_col: Optional[int],
) -> tuple[dict[str, list[list[float]]], Optional[set[int]]]:
    if not room_no or not seat_plan_max_col or seat_plan_max_col <= 0:
        return seats, None

    try:
        from app.seating_plan.seat_mapping import get_column_mapping
    except Exception:
        return seats, None

    col_mapping = get_column_mapping(room_no, seat_plan_max_col)
    allowed_cols = set(col_mapping.values())
    if not allowed_cols:
        return seats, None

    filtered: dict[str, list[list[float]]] = {}
    for seat_key, poly in seats.items():
        col = _extract_seat_col(seat_key)
        if col is None or col in allowed_cols:
            filtered[seat_key] = poly
    return filtered, allowed_cols


def _poly_bounds(poly: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_intersection_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    return iw * ih


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    inter = _bbox_intersection_area(a, b)
    if inter <= 0:
        return 0.0
    area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / max(1.0, area_a + area_b - inter)


def _bbox_ios(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    inter = _bbox_intersection_area(a, b)
    if inter <= 0:
        return 0.0
    area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / max(1.0, min(area_a, area_b))


def _poly_centroid(poly: list[list[float]]) -> tuple[float, float]:
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return (sum(xs) / max(1, len(xs)), sum(ys) / max(1, len(ys)))


def _point_in_poly(poly: list[list[float]], pt: tuple[float, float]) -> bool:
    cnt = np.array(poly, dtype=np.float32)
    return cv2.pointPolygonTest(cnt, pt, False) >= 0


def detect_persons(
    frame: np.ndarray,
    model_path: str,
    conf: float = 0.30,
    imgsz: int = 1280,
    *,
    iou: float = 0.55,
) -> list[PersonDetection]:
    model = YOLO(model_path)
    detections: list[PersonDetection] = []
    idx = 1
    h, w = frame.shape[:2]
    for result in model(frame, conf=conf, iou=iou, classes=[0], imgsz=imgsz, verbose=False):
        if not result.boxes:
            continue
        for box in result.boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            x1, y1 = max(0, int(round(x1))), max(0, int(round(y1)))
            x2, y2 = min(w, int(round(x2))), min(h, int(round(y2)))
            if x2 <= x1 or y2 <= y1:
                continue
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            detections.append(
                PersonDetection(
                    person_key=f"person_{idx}",
                    bbox=(x1, y1, x2, y2),
                    confidence=float(box.conf[0]),
                    center=(cx, cy),
                )
            )
            idx += 1
    return detections


def _bbox_to_flt(b: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = b
    return (float(x1), float(y1), float(x2), float(y2))


def _person_boxes_duplicate(
    pb: tuple[float, float, float, float],
    kb: tuple[float, float, float, float],
    *,
    iou_thresh: float,
    ios_thresh: float,
) -> bool:
    if iou_thresh > 0 and bbox_iou(pb, kb) >= iou_thresh:
        return True
    if ios_thresh > 0 and _bbox_ios(pb, kb) >= ios_thresh:
        return True
    return False


def dedupe_persons(
    persons: list[PersonDetection],
    *,
    iou_thresh: float,
    ios_thresh: float,
) -> tuple[list[PersonDetection], int]:
    if iou_thresh <= 0 and ios_thresh <= 0:
        return _renumber_person_keys(persons), 0
    if len(persons) <= 1:
        return _renumber_person_keys(persons), 0

    sorted_p = sorted(persons, key=lambda p: -p.confidence)
    kept: list[PersonDetection] = []
    for p in sorted_p:
        pb = _bbox_to_flt(p.bbox)
        dup = False
        for k in kept:
            kb = _bbox_to_flt(k.bbox)
            if _person_boxes_duplicate(pb, kb, iou_thresh=iou_thresh, ios_thresh=ios_thresh):
                dup = True
                break
        if not dup:
            kept.append(p)

    kept.sort(key=lambda q: (q.center[0], q.center[1]))
    removed = len(persons) - len(kept)
    return _renumber_person_keys(kept), removed


def _renumber_person_keys(persons: list[PersonDetection]) -> list[PersonDetection]:
    out: list[PersonDetection] = []
    for i, p in enumerate(persons, start=1):
        x1, y1, x2, y2 = p.bbox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        out.append(
            PersonDetection(
                person_key=f"person_{i}",
                bbox=p.bbox,
                confidence=p.confidence,
                center=(cx, cy),
            )
        )
    return out


def build_candidates(
    persons: list[PersonDetection],
    seats: dict[str, list[list[float]]],
    frame_w: int,
    frame_h: int,
) -> list[dict[str, Any]]:
    w_anchor = float(os.getenv("FFMAP_W_ANCHOR", "0.25"))
    w_overlap = float(os.getenv("FFMAP_W_OVERLAP", "0.65"))
    w_centroid = float(os.getenv("FFMAP_W_CENTROID", "0.10"))
    min_score = float(os.getenv("FFMAP_MIN_SCORE", "0.15"))
    frame_diag = math.hypot(frame_w, frame_h)

    candidates: list[dict[str, Any]] = []
    for p in persons:
        x1, y1, x2, y2 = p.bbox
        pb = (float(x1), float(y1), float(x2), float(y2))
        anchors = [
            ((x1 + x2) / 2.0, y2),
            (x1 + 0.25 * (x2 - x1), y2),
            (x1 + 0.75 * (x2 - x1), y2),
        ]
        for seat_key, poly in seats.items():
            anchor_hits = sum(1 for a in anchors if _point_in_poly(poly, a))
            anchor_ratio = anchor_hits / 3.0
            seat_bbox = _poly_bounds(poly)
            overlap = bbox_iou(pb, seat_bbox)
            scx, scy = _poly_centroid(poly)
            dist = math.hypot(p.center[0] - scx, p.center[1] - scy)
            centroid_score = max(0.0, 1.0 - (dist / max(1.0, frame_diag)))
            score = (w_anchor * anchor_ratio) + (w_overlap * overlap) + (w_centroid * centroid_score)
            if score >= min_score:
                candidates.append(
                    {
                        "person_key": p.person_key,
                        "seat_key": seat_key,
                        "score": round(score, 4),
                        "anchor_ratio": round(anchor_ratio, 4),
                        "overlap_iou": round(overlap, 4),
                        "centroid_score": round(centroid_score, 4),
                    }
                )
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def greedy_assign(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    assigned_people: set[str] = set()
    assigned_seats: set[str] = set()
    out: dict[str, dict[str, Any]] = {}
    for c in candidates:
        pk = c["person_key"]
        sk = c["seat_key"]
        if pk in assigned_people or sk in assigned_seats:
            continue
        assigned_people.add(pk)
        assigned_seats.add(sk)
        out[pk] = c
    return out


def build_seat_anchors_for_frame(
    frame_bgr: np.ndarray,
    seat_map: dict[str, list],
    *,
    room_no: str,
    seat_plan_max_col: Optional[int],
    seat_key_to_student: dict[str, tuple[str, str]],
    person_model_path: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """
    Run YOLO + greedy person→seat on one frame; return anchors only for seat_map keys
    that exist in seat_key_to_student (DB-occupied seats).
    """
    if frame_bgr is None or frame_bgr.size == 0 or not seat_map or not seat_key_to_student:
        return {}

    seats_all: dict[str, list[list[float]]] = {
        k: v for k, v in seat_map.items() if isinstance(v, list) and len(v) >= 3
    }
    seats, _allowed = _filter_seats_by_room_column_mapping(
        seats_all,
        room_no=room_no or None,
        seat_plan_max_col=seat_plan_max_col,
    )
    h, w = frame_bgr.shape[:2]
    model_path = person_model_path or os.getenv("PERSON_MODEL_PATH", "yolov8l.pt")
    yolo_iou = float(os.getenv("FFMAP_YOLO_IOU", "0.55"))
    conf = float(os.getenv("FFMAP_PERSON_CONF", "0.05"))
    imgsz = int(os.getenv("FFMAP_PERSON_IMGSZ", "1280"))

    persons_raw = detect_persons(
        frame_bgr,
        model_path=model_path,
        conf=conf,
        imgsz=imgsz,
        iou=yolo_iou,
    )
    dedupe_iou = float(os.getenv("FFMAP_DEDUPE_IOU", "0.45"))
    dedupe_ios = float(os.getenv("FFMAP_DEDUPE_IOS", "0.62"))
    persons, removed = dedupe_persons(
        persons_raw,
        iou_thresh=dedupe_iou,
        ios_thresh=dedupe_ios,
    )
    if removed:
        logger.info(
            "First-frame person dedupe removed %d overlapping box(es) (iou>=%s ios>=%s)",
            removed,
            dedupe_iou,
            dedupe_ios,
        )

    candidates = build_candidates(persons, seats, w, h)
    assigned = greedy_assign(candidates)

    by_key = {p.person_key: p for p in persons}
    anchors: dict[str, dict[str, Any]] = {}
    for pk, rec in assigned.items():
        sk = rec.get("seat_key")
        if not sk:
            continue
        pair = seat_key_to_student.get(sk)
        if not pair:
            continue
        seat_id, student_id = pair
        p = by_key.get(pk)
        if not p:
            continue
        x1, y1, x2, y2 = p.bbox
        foot = ((x1 + x2) / 2.0, float(y2))
        anchors[sk] = {
            "seat_id": seat_id,
            "student_id": student_id,
            "bbox": (x1, y1, x2, y2),
            "bbox_flt": _bbox_to_flt(p.bbox),
            "foot": foot,
        }
    logger.info(
        "First-frame mapping: persons_raw=%d persons=%d anchors(DB seats)=%d",
        len(persons_raw),
        len(persons),
        len(anchors),
    )
    return anchors


def match_behavior_bbox_to_anchor(
    bbox: tuple[int, int, int, int],
    anchors: dict[str, dict[str, Any]],
) -> Optional[tuple[str, str]]:
    """
    Map a behaviour/person bbox to (seat_id, student_id) using the same IoU-then-foot
    rules as SeatMapper first-frame matching, against per-frame sandbox anchors.
    """
    if not bbox or len(bbox) < 4 or not anchors:
        return None
    x1, y1, x2, y2 = (int(round(float(bbox[i]))) for i in range(4))
    qb = (float(x1), float(y1), float(x2), float(y2))
    foot_q = ((x1 + x2) / 2.0, float(y2))
    min_iou = float(os.getenv("SEAT_MAPPER_FF_MIN_IOU", "0.12"))
    max_foot = float(os.getenv("SEAT_MAPPER_FF_MAX_FOOT_DIST", "140"))

    best_rec: Optional[dict[str, Any]] = None
    best_iou = -1.0
    for _sk, rec in anchors.items():
        pb = rec.get("bbox_flt")
        if not pb:
            continue
        iou = bbox_iou(qb, pb)
        if iou > best_iou:
            best_iou = iou
            best_rec = rec

    if best_rec is not None and best_iou >= min_iou:
        return (str(best_rec["seat_id"]), str(best_rec["student_id"]))

    best_d = float("inf")
    best_rec2: Optional[dict[str, Any]] = None
    for _sk, rec in anchors.items():
        fx, fy = rec.get("foot", (0.0, 0.0))
        d = math.hypot(foot_q[0] - fx, foot_q[1] - fy)
        if d < best_d:
            best_d = d
            best_rec2 = rec
    if best_rec2 is not None and best_d <= max_foot:
        return (str(best_rec2["seat_id"]), str(best_rec2["student_id"]))
    return None


def build_identification_rows_from_anchors(
    db_session: Any,
    anchors: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """One overlay row per sandbox-mapped seat: bbox + student name + roll."""
    if not db_session or not anchors:
        return []
    try:
        from uuid import UUID

        from database.models import Student
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    for _sk, rec in anchors.items():
        sid = rec.get("student_id")
        if not sid:
            continue
        try:
            st = db_session.query(Student).filter(Student.student_id == UUID(str(sid))).first()
        except Exception:
            st = None
        name = (st.name or "").strip() if st else ""
        roll = (str(st.roll_number).strip() if st and st.roll_number else "")
        rows.append(
            {
                "bbox": rec["bbox"],
                "student_id": str(sid),
                "full_name": name or "Unknown",
                "roll_number": roll or "—",
            }
        )
    return rows


def enrich_with_db_mapping(
    assigned: dict[str, dict[str, Any]],
    *,
    room_id: Optional[str],
    room_no: Optional[str],
) -> dict[str, dict[str, Any]]:
    if not room_id or not room_no:
        return assigned
    try:
        from uuid import UUID

        from database.db import SessionLocal
        from database.models import Seat, Student
        from app.seating_plan.seat_mapping import get_max_column_from_seats, seat_number_to_seat_map_key
    except Exception:
        return assigned

    db = SessionLocal()
    try:
        seats = db.query(Seat).filter(Seat.room_id == UUID(str(room_id))).all()
        seat_numbers = [s.seat_number for s in seats if s.seat_number]
        max_col = get_max_column_from_seats(seat_numbers)
        seat_key_to_info: dict[str, dict[str, str]] = {}
        for seat in seats:
            if not seat.seat_number:
                continue
            key = seat_number_to_seat_map_key(seat.seat_number, room_no, max_col)
            if not key:
                continue
            roll = ""
            if seat.student_id:
                st = db.query(Student).filter(Student.student_id == seat.student_id).first()
                if st and st.roll_number:
                    roll = st.roll_number
            seat_key_to_info[key] = {
                "seat_id": str(seat.seat_id),
                "student_id": str(seat.student_id) if seat.student_id else "",
                "roll_number": roll,
            }
        for pk, rec in assigned.items():
            info = seat_key_to_info.get(rec["seat_key"])
            if info:
                rec.update(info)
        return assigned
    finally:
        db.close()


def _person_index_label(person_key: str) -> str:
    m = re.match(r"person_(\d+)$", (person_key or "").strip().lower())
    if m:
        return f"person {int(m.group(1))}"
    return (person_key or "person ?").strip()


def _person_sort_key(person_key: str) -> tuple[int, str]:
    m = re.match(r"person_(\d+)$", (person_key or "").strip().lower())
    if m:
        return (int(m.group(1)), person_key)
    return (10**9, person_key)


def print_person_assignment_debug(
    persons: list[PersonDetection],
    assigned: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> None:
    best_by_person: dict[str, float] = {}
    for c in candidates:
        pk = c.get("person_key")
        if not pk:
            continue
        sc = float(c.get("score") or 0.0)
        best_by_person[pk] = max(best_by_person.get(pk, 0.0), sc)

    print("\n--- Seat mapping lab: per-person (bbox order = person 1, 2, …) ---", flush=True)
    for p in sorted(persons, key=lambda x: _person_sort_key(x.person_key)):
        pk = p.person_key
        label = _person_index_label(pk)
        if pk in assigned:
            a = assigned[pk]
            sc = a.get("score")
            seat = a.get("seat_key", "")
            roll = (a.get("roll_number") or "").strip()
            roll_part = f" roll={roll}" if roll else ""
            print(f"{label:14s}  mapped     score={sc}  seat={seat}{roll_part}", flush=True)
        else:
            best = best_by_person.get(pk)
            best_s = f"{best:.4f}" if best is not None else "n/a"
            print(f"{label:14s}  unmapped   best_candidate_score={best_s}", flush=True)
    print("--- end per-person debug ---\n", flush=True)


def draw_debug(
    frame: np.ndarray,
    persons: list[PersonDetection],
    assigned: dict[str, dict[str, Any]],
    seats: dict[str, list[list[float]]],
    output_path: str,
) -> None:
    img = frame.copy()
    for seat_key, poly in seats.items():
        pts = np.array(poly, dtype=np.int32)
        cv2.polylines(img, [pts], True, (255, 180, 0), 1)
        if any(v.get("seat_key") == seat_key for v in assigned.values()):
            cx, cy = _poly_centroid(poly)
            cv2.putText(
                img,
                seat_key,
                (int(cx), int(cy)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                (255, 180, 0),
                1,
                cv2.LINE_AA,
            )

    font = cv2.FONT_HERSHEY_SIMPLEX
    for p in persons:
        x1, y1, x2, y2 = p.bbox
        pid = _person_index_label(p.person_key)
        if p.person_key in assigned:
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 220, 0), 2)
            a = assigned[p.person_key]
            sub = (a.get("roll_number") or a.get("seat_key") or "").strip() or "mapped"
            cv2.putText(img, pid, (x1, max(22, y1 - 6)), font, 0.48, (0, 220, 0), 1, cv2.LINE_AA)
            cv2.putText(img, sub[:48], (x1, max(22, y1 - 30)), font, 0.38, (0, 200, 0), 1, cv2.LINE_AA)
        else:
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 2)
            cv2.putText(img, pid, (x1, max(22, y1 - 6)), font, 0.48, (0, 0, 255), 1, cv2.LINE_AA)
            cv2.putText(img, "unmapped", (x1, max(22, y1 - 30)), font, 0.38, (0, 0, 255), 1, cv2.LINE_AA)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(output_path, img)


def run_lab_from_paths(
    *,
    frame_path: str,
    seat_map_path: str,
    output_dir: str,
    room_id: Optional[str] = None,
    room_no: Optional[str] = None,
    seat_plan_max_col: Optional[int] = None,
    person_model_path: Optional[str] = None,
) -> dict[str, Any]:
    """CLI / sandbox harness: load files, run pipeline, write debug image + JSON."""
    person_model_path = person_model_path or os.getenv("PERSON_MODEL_PATH", "yolov8l.pt")
    frame = cv2.imread(frame_path)
    if frame is None:
        raise FileNotFoundError(f"Could not read frame: {frame_path}")
    seats_all = _load_seat_map(seat_map_path)
    seats, allowed_cols = _filter_seats_by_room_column_mapping(
        seats_all,
        room_no=room_no,
        seat_plan_max_col=seat_plan_max_col,
    )
    h, w = frame.shape[:2]
    yolo_iou = float(os.getenv("FFMAP_YOLO_IOU", "0.55"))
    persons_raw = detect_persons(
        frame,
        model_path=person_model_path,
        conf=float(os.getenv("FFMAP_PERSON_CONF", "0.05")),
        imgsz=int(os.getenv("FFMAP_PERSON_IMGSZ", "1280")),
        iou=yolo_iou,
    )
    dedupe_iou = float(os.getenv("FFMAP_DEDUPE_IOU", "0.45"))
    dedupe_ios = float(os.getenv("FFMAP_DEDUPE_IOS", "0.62"))
    persons, dedupe_removed = dedupe_persons(
        persons_raw,
        iou_thresh=dedupe_iou,
        ios_thresh=dedupe_ios,
    )
    if dedupe_removed:
        print(
            f"Person dedupe: removed {dedupe_removed} overlapping detection(s) "
            f"(iou>={dedupe_iou}, ios>={dedupe_ios}); YOLO NMS iou={yolo_iou}",
            flush=True,
        )
    candidates = build_candidates(persons, seats, w, h)
    assigned = greedy_assign(candidates)
    assigned = enrich_with_db_mapping(assigned, room_id=room_id, room_no=room_no)
    if os.getenv("FFMAP_DEBUG_PRINT", "").strip().lower() in ("1", "true", "yes", "on"):
        print_person_assignment_debug(persons, assigned, candidates)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_img = str(out_dir / "first_frame_mapping_debug.jpg")
    draw_debug(frame, persons, assigned, seats, debug_img)

    result: dict[str, Any] = {
        "frame_path": frame_path,
        "seat_map_path": seat_map_path,
        "person_model_path": person_model_path,
        "room_no": room_no,
        "seat_plan_max_col": seat_plan_max_col,
        "allowed_seat_map_cols": sorted(allowed_cols) if allowed_cols else None,
        "seat_polygons_total": len(seats_all),
        "seat_polygons_considered": len(seats),
        "persons_detected_raw": len(persons_raw),
        "persons_detected": len(persons),
        "persons_dedupe_removed": dedupe_removed,
        "assigned_count": len(assigned),
        "unmapped_count": max(0, len(persons) - len(assigned)),
        "assignments": assigned,
        "debug_image": debug_img,
    }
    out_json = out_dir / "first_frame_mapping.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    result["json_output"] = str(out_json)
    return result
