#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = BACKEND_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv

load_dotenv(BACKEND_DIR / ".env")
if not (BACKEND_DIR / ".env").exists():
    load_dotenv(BACKEND_DIR.parent / ".env")

from sandbox.seat_mapping_lab.first_frame_mapper import run_first_frame_mapping


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Standalone first-frame seat mapping test harness."
    )
    p.add_argument(
        "--frame",
        default=str(
            BACKEND_DIR
            / "src"
            / "app"
            / "ai_engine"
            / "D314_classroom_person_mapping_frame.png"
        ),
        help="Path to first frame image (default: D314 classroom reference for person→seat sandbox).",
    )
    p.add_argument(
        "--seat-map",
        default=str(BACKEND_DIR / "src" / "app" / "seating_plan" / "CSFYP" / "D314-25112025" / "seat_map.json"),
        help="Path to seat_map.json.",
    )
    p.add_argument(
        "--output-dir",
        default=str(BACKEND_DIR / "outputs" / "seat_mapping_lab"),
        help="Directory to save debug image + JSON output.",
    )
    p.add_argument("--room-id", default="", help="Room UUID (optional, for DB seat/student enrichment).")
    p.add_argument("--room-no", default="D-314", help="Room number for seat key mapping (e.g., D-314).")
    p.add_argument(
        "--seat-plan-max-col",
        type=int,
        default=6,
        help="Maximum seating-plan input columns (used to exclude unmapped seat_map columns).",
    )
    p.add_argument("--person-model", default="", help="YOLO person model path (default from PERSON_MODEL_PATH).")
    return p


def main() -> None:
    import os

    # Match previous lab harness: print per-person scores unless overridden.
    os.environ.setdefault("FFMAP_DEBUG_PRINT", "1")
    args = build_parser().parse_args()
    result = run_first_frame_mapping(
        frame_path=args.frame,
        seat_map_path=args.seat_map,
        output_dir=args.output_dir,
        room_id=args.room_id or None,
        room_no=args.room_no or None,
        seat_plan_max_col=args.seat_plan_max_col or None,
        person_model_path=args.person_model or None,
    )
    print("First-frame seat mapping completed")
    print(f"persons_detected={result['persons_detected']}")
    print(f"assigned_count={result['assigned_count']}")
    print(f"unmapped_count={result['unmapped_count']}")
    print(f"allowed_seat_map_cols={result['allowed_seat_map_cols']}")
    print(f"seat_polygons_considered={result['seat_polygons_considered']}/{result['seat_polygons_total']}")
    print(f"debug_image={result['debug_image']}")
    print(f"json_output={result['json_output']}")


if __name__ == "__main__":
    main()
