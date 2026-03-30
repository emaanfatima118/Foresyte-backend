"""
Invigilator Plans API
Allows admins and investigators to assign invigilators to exam rooms
and lets the video-upload page verify that an assignment exists before
accepting a video.

Endpoints
---------
GET  /invigilator-plans                          – paginated list of all exams as plans
GET  /invigilator-plans/assignment-check         – check if a room has an invigilator
GET  /invigilator-plans/{exam_id}                – detailed plan (rooms + assignments) for one exam
POST /invigilator-plans/{exam_id}/assign         – assign an invigilator to a room
DELETE /invigilator-plans/assignments/{id}        – remove an assignment
"""

from datetime import date, datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.auth import get_current_user
from database.db import get_db
from database.models import Exam, ExamRoomAssignment, Invigilator, Room

router = APIRouter(prefix="/invigilator-plans", tags=["Invigilator Plans"])


# ─────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────

class AssignmentOut(BaseModel):
    assignment_id: str
    invigilator_id: str
    name: str
    email: Optional[str] = None
    is_primary: bool = True


class RoomRowOut(BaseModel):
    room_id: str
    room_name: str
    assignments: List[AssignmentOut]


class PlanOut(BaseModel):
    id: str
    course: str
    exam_date: Optional[str] = None
    start_time: Optional[str] = None
    uploaded_at: str
    status: str
    rooms: Optional[List[RoomRowOut]] = None


class AssignBody(BaseModel):
    room_id: str
    invigilator_id: str


class AssignmentCheckOut(BaseModel):
    assigned: bool
    invigilator_name: Optional[str] = None
    message: Optional[str] = None


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _exam_status(exam: Exam) -> str:
    today = date.today()
    exam_date = exam.exam_date if isinstance(exam.exam_date, date) else None
    if exam_date and exam_date < today:
        return "completed"
    return "processing"


def _build_plan(exam: Exam, db: Session, include_rooms: bool = False) -> PlanOut:
    rooms_out: Optional[List[RoomRowOut]] = None
    if include_rooms:
        rooms = db.query(Room).filter(Room.exam_id == exam.exam_id).all()
        rooms_out = []
        for room in rooms:
            assignments = (
                db.query(ExamRoomAssignment)
                .filter(
                    ExamRoomAssignment.exam_id == exam.exam_id,
                    ExamRoomAssignment.room_id == room.room_id,
                )
                .all()
            )
            a_out = []
            for a in assignments:
                inv: Optional[Invigilator] = db.query(Invigilator).filter(
                    Invigilator.invigilator_id == a.invigilator_id
                ).first()
                if inv:
                    a_out.append(
                        AssignmentOut(
                            assignment_id=str(a.assignment_id),
                            invigilator_id=str(a.invigilator_id),
                            name=inv.name,
                            email=inv.email,
                            is_primary=True,
                        )
                    )
            room_label = (
                f"{room.block}-{room.room_number}" if room.block else room.room_number
            )
            rooms_out.append(
                RoomRowOut(
                    room_id=str(room.room_id),
                    room_name=room_label,
                    assignments=a_out,
                )
            )

    return PlanOut(
        id=str(exam.exam_id),
        course=exam.course,
        exam_date=str(exam.exam_date) if exam.exam_date else None,
        start_time=str(exam.start_time) if exam.start_time else None,
        uploaded_at=exam.created_at.isoformat() if exam.created_at else datetime.utcnow().isoformat(),
        status=_exam_status(exam),
        rooms=rooms_out,
    )


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

@router.get("/assignment-check", response_model=AssignmentCheckOut)
def assignment_check(
    exam_id: str = Query(...),
    room_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Returns whether an invigilator is assigned to a specific room for this exam.
    Used by the video upload page to gate the Upload button.
    """
    try:
        exam_uuid = UUID(exam_id)
        room_uuid = UUID(room_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid exam_id or room_id")

    assignment = (
        db.query(ExamRoomAssignment)
        .filter(
            ExamRoomAssignment.exam_id == exam_uuid,
            ExamRoomAssignment.room_id == room_uuid,
        )
        .first()
    )

    if not assignment:
        return AssignmentCheckOut(
            assigned=False,
            message="No invigilator assigned to this room. Assign one in Invigilator Plans.",
        )

    inv: Optional[Invigilator] = db.query(Invigilator).filter(
        Invigilator.invigilator_id == assignment.invigilator_id
    ).first()

    return AssignmentCheckOut(
        assigned=True,
        invigilator_name=inv.name if inv else None,
        message=None,
    )


@router.get("", response_model=dict)
def list_plans(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Return all exams as invigilator plans (with room-level assignment summary).
    """
    query = db.query(Exam)
    total = query.count()

    exams = query.order_by(Exam.exam_date.desc()).offset((page - 1) * limit).limit(limit).all()

    today = date.today()
    plans = []
    for exam in exams:
        p = _build_plan(exam, db, include_rooms=True)
        if status and p.status != status:
            continue
        plans.append(p.model_dump())

    return {
        "plans": plans,
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/{exam_id}", response_model=PlanOut)
def get_plan(
    exam_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Return detailed plan (rooms + assignments) for a single exam."""
    try:
        exam_uuid = UUID(exam_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid exam_id")

    exam = db.query(Exam).filter(Exam.exam_id == exam_uuid).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    return _build_plan(exam, db, include_rooms=True)


@router.post("/{exam_id}/assign", response_model=AssignmentOut)
def assign_invigilator(
    exam_id: str,
    body: AssignBody,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Assign an invigilator to a room for this exam (admin only)."""
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Only admins and investigators can assign invigilators")

    try:
        exam_uuid = UUID(exam_id)
        room_uuid = UUID(body.room_id)
        inv_uuid = UUID(body.invigilator_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID in request")

    exam = db.query(Exam).filter(Exam.exam_id == exam_uuid).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    room = db.query(Room).filter(Room.room_id == room_uuid, Room.exam_id == exam_uuid).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found for this exam")

    inv = db.query(Invigilator).filter(Invigilator.invigilator_id == inv_uuid).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invigilator not found")

    existing = (
        db.query(ExamRoomAssignment)
        .filter(
            ExamRoomAssignment.exam_id == exam_uuid,
            ExamRoomAssignment.room_id == room_uuid,
            ExamRoomAssignment.invigilator_id == inv_uuid,
        )
        .first()
    )
    if existing:
        return AssignmentOut(
            assignment_id=str(existing.assignment_id),
            invigilator_id=str(existing.invigilator_id),
            name=inv.name,
            email=inv.email,
            is_primary=True,
        )

    new_assignment = ExamRoomAssignment(
        exam_id=exam_uuid,
        room_id=room_uuid,
        invigilator_id=inv_uuid,
        created_at=datetime.utcnow(),
    )
    db.add(new_assignment)
    db.commit()
    db.refresh(new_assignment)

    return AssignmentOut(
        assignment_id=str(new_assignment.assignment_id),
        invigilator_id=str(new_assignment.invigilator_id),
        name=inv.name,
        email=inv.email,
        is_primary=True,
    )


@router.delete("/assignments/{assignment_id}", status_code=204)
def remove_assignment(
    assignment_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Remove a specific invigilator assignment (admin only)."""
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Only admins and investigators can remove assignments")

    try:
        asgn_uuid = UUID(assignment_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid assignment_id")

    assignment = db.query(ExamRoomAssignment).filter(
        ExamRoomAssignment.assignment_id == asgn_uuid
    ).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    db.delete(assignment)
    db.commit()
    return None
