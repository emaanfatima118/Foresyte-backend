#!/usr/bin/env python3
"""
Single-frame pipeline: ForeSyte behaviour (cheating) detection + first-frame seat mapping,
with roster from a plain-text attendance list (no DB).

Run from repo root or anywhere; adds Foresyte-backend/src to sys.path.

Example:
  cd Foresyte-backend/src
  python ..\\sandbox\\seat_mapping_lab\\run_frame_cheating_and_seatmap.py ^
    --frame path\\to\\frame.jpg ^
    --seat-map ..\\src\\app\\seating_plan\\CSFYP\\D314-25112025\\seat_map.json ^
    --roster path\\to\\roster_d302.txt ^
    --room-no D-302 ^
    --out ..\\outputs\\seat_mapping_lab\\d302_combined.jpg
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

# src/ on path for `app` and `database` imports
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from app.ai_engine.detection_adapter import run_behaviour_on_frame
from app.ai_engine.foresyte_detect_pipeline import ForesyteDetectConfig
from app.video_processing.first_frame_seat_mapping import (
    bbox_iou,
    build_candidates,
    dedupe_persons,
    detect_persons,
    draw_debug,
    greedy_assign,
    _filter_seats_by_room_column_mapping,
    _load_seat_map,
)


def _seat_token_to_key(token: str) -> Optional[str]:
    t = (token or "").strip().upper()
    m = re.fullmatch(r"C(\d+)R(\d+)", t, flags=re.I)
    if not m:
        return None
    return f"seat_c{int(m.group(1))}r{int(m.group(2))}"


def parse_roster_text(text: str) -> dict[str, dict[str, str]]:
    """
    Parse lines like: 1 24I-2023 Saim Zaib C1R1
    Returns seat_c1r1 -> {"roll": "24I-2023", "name": "Saim Zaib"}
    """
    roster: dict[str, dict[str, str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("signature") or low.startswith("date:"):
            continue
        if "seating plan" in low or "examination" in low or "sessional" in low:
            continue
        if "ee1005" in low or "room no" in low or "digital logic" in low:
            continue

        m = re.search(r"\s(C\d+R\d+)\s*$", line, flags=re.I)
        if not m:
            continue
        seat_key = _seat_token_to_key(m.group(1))
        if not seat_key:
            continue
        prefix = line[: m.start()].strip()
        parts = prefix.split(None, 2)
        if len(parts) < 3:
            continue
        _idx, roll, name = parts[0], parts[1], parts[2]
        if not re.match(r"^24I-\S+$", roll, flags=re.I):
            continue
        roster[seat_key] = {"roll": roll.strip(), "name": name.strip()}
    return roster


def enrich_assigned_from_roster(
    assigned: dict[str, dict[str, Any]],
    roster: dict[str, dict[str, str]],
) -> dict[str, dict[str, Any]]:
    for _pk, rec in assigned.items():
        sk = rec.get("seat_key")
        if not sk:
            continue
        info = roster.get(sk)
        if info:
            rec["roll_number"] = info["roll"]
            rec["full_name"] = info["name"]
    return assigned


def _max_column_from_roster(roster: dict[str, dict[str, str]]) -> int:
    best = 0
    for k in roster:
        m = re.search(r"seat_c(\d+)r\d+", k.lower())
        if m:
            best = max(best, int(m.group(1)))
    return best


def draw_suspicious_overlay(
    img: np.ndarray,
    suspicious: list[Any],
    person_boxes: list[tuple[tuple[int, int, int, int], str, str]],
    *,
    min_iou: float = 0.08,
) -> None:
    """Draw behaviour boxes + label; second line roll + name from best IoU person match."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    color = (40, 40, 255)  # red-ish BGR
    for r in suspicious:
        x1, y1, x2, y2 = (int(round(float(v))) for v in r.bbox)
        qb = (float(x1), float(y1), float(x2), float(y2))
        best_iou = 0.0
        best_roll, best_name = "", ""
        for pb, roll, name in person_boxes:
            iou = bbox_iou(qb, tuple(float(x) for x in pb))
            if iou > best_iou:
                best_iou = iou
                best_roll, best_name = roll, name
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 3)
        line1 = f"{r.label} {float(r.confidence):.2f}"
        y_text = max(20, y1 - 8)
        cv2.putText(img, line1, (x1, y_text), font, 0.55, color, 2, cv2.LINE_AA)
        if best_iou >= min_iou and (best_roll or best_name):
            line2 = f"{best_roll}  {best_name}".strip()
            if len(line2) > 60:
                line2 = line2[:57] + "..."
            cv2.putText(
                img,
                line2,
                (x1, max(20, y_text - 26)),
                font,
                0.5,
                (255, 220, 180),
                2,
                cv2.LINE_AA,
            )
        elif best_iou < min_iou:
            cv2.putText(
                img,
                "(seat unknown)",
                (x1, max(20, y_text - 26)),
                font,
                0.45,
                (180, 180, 255),
                1,
                cv2.LINE_AA,
            )


def run(
    *,
    frame_path: str,
    seat_map_path: str,
    roster_path: str,
    out_path: str,
    room_no: Optional[str],
    seat_plan_max_col: Optional[int],
    person_model_path: Optional[str],
    detect_cfg: Optional[ForesyteDetectConfig],
) -> dict[str, Any]:
    roster_text = Path(roster_path).read_text(encoding="utf-8")
    roster = parse_roster_text(roster_text)
    if not roster:
        raise ValueError(f"No roster rows parsed from {roster_path}")

    frame = cv2.imread(frame_path)
    if frame is None:
        raise FileNotFoundError(frame_path)

    max_col = seat_plan_max_col
    if max_col is None:
        max_col = _max_column_from_roster(roster) or None

    seats_all = _load_seat_map(seat_map_path)
    seats, _allowed = _filter_seats_by_room_column_mapping(
        seats_all,
        room_no=room_no,
        seat_plan_max_col=max_col,
    )
    h, w = frame.shape[:2]

    pm = person_model_path or os.getenv("PERSON_MODEL_PATH", "yolov8l.pt")
    yolo_iou = float(os.getenv("FFMAP_YOLO_IOU", "0.55"))
    persons_raw = detect_persons(
        frame,
        model_path=pm,
        conf=float(os.getenv("FFMAP_PERSON_CONF", "0.05")),
        imgsz=int(os.getenv("FFMAP_PERSON_IMGSZ", "1280")),
        iou=yolo_iou,
    )
    persons, _removed = dedupe_persons(
        persons_raw,
        iou_thresh=float(os.getenv("FFMAP_DEDUPE_IOU", "0.45")),
        ios_thresh=float(os.getenv("FFMAP_DEDUPE_IOS", "0.62")),
    )
    candidates = build_candidates(persons, seats, w, h)
    assigned = greedy_assign(candidates)
    enrich_assigned_from_roster(assigned, roster)

    # Behaviour / cheating-style detection (full-frame ForeSyte model)
    cfg = detect_cfg or ForesyteDetectConfig.from_env()
    beh_results, _beh_annot = run_behaviour_on_frame(frame, cfg=cfg)
    suspicious = [r for r in beh_results if r.is_suspicious]

    person_boxes: list[tuple[tuple[int, int, int, int], str, str]] = []
    by_key = {p.person_key: p for p in persons}
    for pk, rec in assigned.items():
        p = by_key.get(pk)
        if not p:
            continue
        roll = (rec.get("roll_number") or "").strip()
        name = (rec.get("full_name") or "").strip()
        person_boxes.append((p.bbox, roll, name))

    fd, seatmap_tmp = tempfile.mkstemp(suffix="_seatmap.jpg")
    os.close(fd)
    try:
        draw_debug(frame, persons, assigned, seats, seatmap_tmp)
        composite = cv2.imread(seatmap_tmp)
    finally:
        try:
            os.unlink(seatmap_tmp)
        except OSError:
            pass
    if composite is None:
        composite = frame.copy()

    draw_suspicious_overlay(composite, suspicious, person_boxes)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(out_path, composite)

    summary = {
        "frame_path": frame_path,
        "seat_map_path": seat_map_path,
        "roster_path": roster_path,
        "output_image": out_path,
        "room_no": room_no,
        "seat_plan_max_col": max_col,
        "roster_seats_parsed": len(roster),
        "persons": len(persons),
        "assigned": len(assigned),
        "suspicious_behaviours": len(suspicious),
        "suspicious": [
            {
                "label": r.label,
                "confidence": float(r.confidence),
                "bbox": list(map(int, r.bbox)),
            }
            for r in suspicious
        ],
        "assignments": {
            pk: {
                "seat_key": v.get("seat_key"),
                "score": v.get("score"),
                "roll_number": v.get("roll_number"),
                "full_name": v.get("full_name"),
            }
            for pk, v in assigned.items()
        },
    }
    json_path = str(Path(out_path).with_suffix(".json"))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    summary["json_output"] = json_path
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frame", required=True, help="BGR image path (jpg/png)")
    ap.add_argument("--seat-map", required=True, help="seat_map.json with seats.seat_c*r* polygons")
    ap.add_argument("--roster", required=True, help="Text file: lines like '1 24I-2023 Name C1R1'")
    ap.add_argument("--out", default="", help="Output image path (default: outputs/.../combined.jpg)")
    ap.add_argument("--room-no", default="", help="e.g. D-302 for column filtering")
    ap.add_argument(
        "--seat-plan-max-col",
        type=int,
        default=0,
        help="Max column C in plan (default: infer from roster)",
    )
    ap.add_argument("--person-model", default="", help="YOLO person weights (default env PERSON_MODEL_PATH)")
    args = ap.parse_args()

    out = args.out.strip()
    if not out:
        out_dir = _SRC.parent / "outputs" / "seat_mapping_lab"
        out = str(out_dir / "frame_cheating_seatmap.jpg")

    max_col: Optional[int] = args.seat_plan_max_col or None
    if args.seat_plan_max_col == 0:
        max_col = None

    summary = run(
        frame_path=args.frame,
        seat_map_path=args.seat_map,
        roster_path=args.roster,
        out_path=out,
        room_no=args.room_no.strip() or None,
        seat_plan_max_col=max_col,
        person_model_path=args.person_model.strip() or None,
        detect_cfg=None,
    )
    print(json.dumps({k: summary[k] for k in summary if k != "assignments"}, indent=2))
    print("Wrote:", summary["output_image"])
    print("JSON:", summary.get("json_output"))


if __name__ == "__main__":
    main()
