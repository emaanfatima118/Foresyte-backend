"""
Maps detection bounding boxes to students using seating plan from DB and seat_map.json.
Flow: DB seating plan (Room, Seats, Students) -> seat_map.json by block (A,B,C,D) -> bbox point-in-polygon.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Optional
from uuid import UUID

from app.seating_plan.seat_mapping import (
    get_max_column_from_seats,
    seat_number_to_seat_map_key,
)

logger = logging.getLogger(__name__)


def _point_in_polygon(px: float, py: float, polygon: list) -> bool:
    """Ray-casting: point inside polygon iff ray crosses boundary odd times."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _polygon_centroid(polygon: list) -> tuple[float, float]:
    if not polygon:
        return (0.0, 0.0)
    xs = [float(p[0]) for p in polygon]
    ys = [float(p[1]) for p in polygon]
    return (sum(xs) / max(1, len(xs)), sum(ys) / max(1, len(ys)))


class SeatMapper:
    """
    Maps bbox (from AI detection) to student_id.
    1. Load seating plan from DB (Room, Seats, Students for exam/room)
    2. Map each Seat.seat_number to seat_map.json key using block-specific column mapping
    3. For each detection bbox: point-in-polygon -> seat_map_key -> student_id
    """

    def __init__(
        self,
        seat_map: dict,
        room_id: str,
        exam_id: str,
        db_session,
        room_no: str = "",
    ):
        """
        Args:
            seat_map: dict of seat_map_key (e.g. seat_c1r1) -> polygon [[x,y], ...]
            room_id: Room UUID
            exam_id: Exam UUID
            db_session: SQLAlchemy session
            room_no: Room number e.g. "D-314" (for block mapping A,B,C,D)
        """
        self.seat_map = seat_map or {}
        self.room_id = room_id
        self.exam_id = exam_id
        self.db_session = db_session
        self.room_no = room_no or ""
        self._seat_key_to_student: dict[str, tuple[str, str]] = {}
        self._seat_key_to_polygon: dict[str, list] = {}
        self._seat_plan_max_col: Optional[int] = None
        self._first_frame_anchors: dict[str, dict[str, Any]] = {}
        self._build_lookup_from_db()

    def _build_lookup_from_db(self) -> None:
        """
        Build seat_map_key -> (seat_id, student_id) from DB seating plan.
        Uses the same mapping logic as seating plan upload (C1R1 -> seat_c1r1 via column mapping).
        """
        if not self.db_session or not self.seat_map:
            return
        try:
            from database.models import Seat

            seats = (
                self.db_session.query(Seat)
                .filter(Seat.room_id == UUID(self.room_id))
                .all()
            )

            seat_numbers = [s.seat_number for s in seats if s.seat_number]
            max_col = get_max_column_from_seats(seat_numbers)
            self._seat_plan_max_col = max_col

            for seat in seats:
                if not seat.student_id:
                    continue
                seat_map_key = seat_number_to_seat_map_key(
                    seat.seat_number, self.room_no, max_col
                )
                if seat_map_key and seat_map_key in self.seat_map:
                    self._seat_key_to_student[seat_map_key] = (
                        str(seat.seat_id),
                        str(seat.student_id),
                    )
                    self._seat_key_to_polygon[seat_map_key] = self.seat_map[seat_map_key]

            logger.info(
                "SeatMapper: mapped %d/%d seats to students for room %s (from DB seating plan)",
                len(self._seat_key_to_student),
                len(seats),
                self.room_id,
            )
        except Exception as e:
            logger.warning("Failed to build seat lookup from DB: %s", e)

    def install_first_frame_anchors(self, frame_bgr: Any) -> None:
        """
        Run YOLO person + greedy seat assignment on the first video frame and cache
        per-seat reference bboxes for later frames (IoU / foot-distance match).
        Controlled by SEAT_MAPPER_FIRST_FRAME (default on).
        """
        self._first_frame_anchors = {}
        if os.getenv("SEAT_MAPPER_FIRST_FRAME", "1").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return
        if frame_bgr is None or not getattr(frame_bgr, "shape", None):
            return
        try:
            from app.video_processing import first_frame_seat_mapping as ff

            self._first_frame_anchors = ff.build_seat_anchors_for_frame(
                frame_bgr,
                self.seat_map,
                room_no=self.room_no,
                seat_plan_max_col=self._seat_plan_max_col,
                seat_key_to_student=self._seat_key_to_student,
            )
            logger.info(
                "SeatMapper: first-frame anchors installed for %d occupied seats",
                len(self._first_frame_anchors),
            )
        except Exception as e:
            logger.warning("SeatMapper: first-frame anchors failed: %s", e, exc_info=True)
            self._first_frame_anchors = {}

    def _match_first_frame_anchor(
        self, bbox: tuple[int, int, int, int]
    ) -> Optional[tuple[str, str]]:
        if not self._first_frame_anchors:
            return None
        from app.video_processing.first_frame_seat_mapping import bbox_iou as _ff_iou

        x1, y1, x2, y2 = bbox
        qb = (float(x1), float(y1), float(x2), float(y2))
        foot_qx = (x1 + x2) / 2.0
        foot_qy = float(y2)
        min_iou = float(os.getenv("SEAT_MAPPER_FF_MIN_IOU", "0.12"))
        max_foot_dist = float(os.getenv("SEAT_MAPPER_FF_MAX_FOOT_DIST", "140"))

        best_rec: Optional[dict[str, Any]] = None
        best_iou = -1.0
        for _sk, rec in self._first_frame_anchors.items():
            pb = rec.get("bbox_flt")
            if not pb:
                continue
            iou = _ff_iou(qb, pb)
            if iou > best_iou:
                best_iou = iou
                best_rec = rec

        if best_rec is not None and best_iou >= min_iou:
            return (str(best_rec["seat_id"]), str(best_rec["student_id"]))

        best_d = float("inf")
        best_rec2: Optional[dict[str, Any]] = None
        for _sk, rec in self._first_frame_anchors.items():
            fx, fy = rec.get("foot", (0.0, 0.0))
            d = math.hypot(foot_qx - fx, foot_qy - fy)
            if d < best_d:
                best_d = d
                best_rec2 = rec
        if best_rec2 is not None and best_d <= max_foot_dist:
            return (str(best_rec2["seat_id"]), str(best_rec2["student_id"]))
        return None

    def get_student_for_bbox(self, bbox: tuple[int, int, int, int]) -> Optional[tuple[str, str]]:
        """
        Map bbox (x1,y1,x2,y2) to (seat_id, student_id) via point-in-polygon.
        Returns (seat_id, student_id) or None.
        """
        if not self.seat_map or not self._seat_key_to_student:
            return None
        ff = self._match_first_frame_anchor(bbox)
        if ff:
            return ff

        x1, y1, x2, y2 = bbox
        probe_points = [
            ((x1 + x2) / 2.0, (y1 + y2) / 2.0),   # center
            ((x1 + x2) / 2.0, y2),               # bottom center
            ((x1 + x2) / 2.0, y1 + 0.75 * (y2 - y1)),
            (x1 + 0.33 * (x2 - x1), y2),
            (x1 + 0.66 * (x2 - x1), y2),
        ]

        for px, py in probe_points:
            for seat_map_key, polygon in self.seat_map.items():
                if _point_in_polygon(px, py, polygon):
                    result = self._seat_key_to_student.get(seat_map_key)
                    if result:
                        return result

        # Optional nearest-seat fallback. Enabled by default to improve mapping
        # coverage; unmatched detections still remain Unidentified when distance
        # is too large.
        use_nearest_fallback = os.getenv("SEAT_MAPPER_NEAREST_FALLBACK", "1").strip().lower() in (
            "1", "true", "yes", "on"
        )
        if use_nearest_fallback:
            bottom_cx = (x1 + x2) / 2.0
            bottom_cy = y2
            best_key = None
            best_dist = float("inf")
            best_dx = float("inf")
            for seat_map_key, polygon in self._seat_key_to_polygon.items():
                pcx, pcy = _polygon_centroid(polygon)
                dist = math.hypot(bottom_cx - pcx, bottom_cy - pcy)
                dx = abs(bottom_cx - pcx)
                if dist < best_dist:
                    best_dist = dist
                    best_dx = dx
                    best_key = seat_map_key

            nearest_max_dist = float(
                os.getenv("SEAT_MAPPER_NEAREST_MAX_DIST", "145")
            )
            nearest_max_dx = float(
                os.getenv("SEAT_MAPPER_NEAREST_MAX_DX", "110")
            )
            adaptive_dist_cap = max(nearest_max_dist, (y2 - y1) * 1.05)
            if (
                best_key is not None
                and best_dist <= adaptive_dist_cap
                and best_dx <= nearest_max_dx
            ):
                result = self._seat_key_to_student.get(best_key)
                if result:
                    return result
        return None
