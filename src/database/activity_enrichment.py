"""Batch-load student / exam / seat context for activity and violation API responses."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from database.models import Exam, Room, Seat, Student, StudentActivity, Violation


def room_label(room: Room | None) -> str | None:
    if not room:
        return None
    if getattr(room, "block", None):
        return f"{room.block}-{room.room_number}"
    return str(room.room_number) if room.room_number else None


def batch_activity_context(
    db: Session, activities: list[StudentActivity]
) -> tuple[dict, dict, dict, dict]:
    """Returns (students_by_id, exams_by_id, seats_by_student_id, rooms_by_id)."""
    sids = {a.student_id for a in activities if a.student_id}
    eids = {a.exam_id for a in activities if a.exam_id}
    students: dict[UUID, Student] = {}
    if sids:
        students = {
            s.student_id: s
            for s in db.query(Student).filter(Student.student_id.in_(sids)).all()
        }
    exams: dict[UUID, Exam] = {}
    if eids:
        exams = {
            e.exam_id: e
            for e in db.query(Exam).filter(Exam.exam_id.in_(eids)).all()
        }
    seats: dict[UUID, Seat] = {}
    if sids:
        for seat in db.query(Seat).filter(Seat.student_id.in_(sids)).all():
            seats[seat.student_id] = seat
    rids = {s.room_id for s in seats.values() if s.room_id}
    rooms: dict[UUID, Room] = {}
    if rids:
        rooms = {
            r.room_id: r
            for r in db.query(Room).filter(Room.room_id.in_(rids)).all()
        }
    return students, exams, seats, rooms


def student_activity_to_dict(
    a: StudentActivity,
    students: dict,
    exams: dict,
    seats: dict,
    rooms: dict,
) -> dict[str, Any]:
    st = students.get(a.student_id)
    ex = exams.get(a.exam_id)
    seat = seats.get(a.student_id)
    rm = rooms.get(seat.room_id) if seat and seat.room_id else None
    return {
        "activity_id": a.activity_id,
        "student_id": a.student_id,
        "exam_id": a.exam_id,
        "timestamp": a.timestamp,
        "activity_type": a.activity_type,
        "severity": a.severity,
        "confidence": a.confidence,
        "evidence_url": a.evidence_url,
        "run_frame_count": getattr(a, "run_frame_count", None),
        "severity_rule": getattr(a, "severity_rule", None),
        "student_name": st.name if st else None,
        "exam_name": ex.course if ex else None,
        "seat_number": seat.seat_number if seat else None,
        "room": room_label(rm),
    }


def enrich_activities(db: Session, activities: list[StudentActivity]) -> list[dict[str, Any]]:
    if not activities:
        return []
    ctx = batch_activity_context(db, activities)
    return [student_activity_to_dict(a, *ctx) for a in activities]


def violation_to_dict(
    v: Violation,
    activity: StudentActivity | None,
    students: dict,
    exams: dict,
    seats: dict,
    rooms: dict,
) -> dict[str, Any]:
    base = {
        "violation_id": v.violation_id,
        "activity_id": v.activity_id,
        "violation_type": v.violation_type,
        "timestamp": v.timestamp,
        "severity": v.severity,
        "status": v.status,
        "evidence_url": v.evidence_url,
        "student_id": None,
        "student_name": None,
        "exam_id": None,
        "exam_name": None,
        "seat_number": None,
        "room": None,
    }
    if not activity:
        return base
    st = students.get(activity.student_id)
    ex = exams.get(activity.exam_id)
    seat = seats.get(activity.student_id)
    rm = rooms.get(seat.room_id) if seat and seat.room_id else None
    base["student_id"] = activity.student_id
    base["exam_id"] = activity.exam_id
    base["student_name"] = st.name if st else None
    base["exam_name"] = ex.course if ex else None
    base["seat_number"] = seat.seat_number if seat else None
    base["room"] = room_label(rm)
    base["run_frame_count"] = getattr(activity, "run_frame_count", None)
    base["severity_rule"] = getattr(activity, "severity_rule", None)
    return base


def enrich_violations(db: Session, violations: list[Violation]) -> list[dict[str, Any]]:
    if not violations:
        return []
    act_ids = {v.activity_id for v in violations if v.activity_id}
    activities = (
        {
            a.activity_id: a
            for a in db.query(StudentActivity)
            .filter(StudentActivity.activity_id.in_(act_ids))
            .all()
        }
        if act_ids
        else {}
    )
    act_list = list(activities.values())
    students, exams, seats, rooms = batch_activity_context(db, act_list)
    out = []
    for v in violations:
        act = activities.get(v.activity_id)
        out.append(violation_to_dict(v, act, students, exams, seats, rooms))
    return out
