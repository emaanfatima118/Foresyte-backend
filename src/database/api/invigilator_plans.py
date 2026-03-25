"""
Invigilator plans: assign invigilators to exam rooms (parallel to seating plans for students).
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from uuid import UUID
from datetime import datetime, date as date_class
from typing import List, Optional
from pydantic import BaseModel

from database.db import get_db
from database.models import Room, Exam, Invigilator, ExamInvigilatorAssignment
from database.auth import get_current_user

router = APIRouter(prefix="/invigilator-plans", tags=["invigilator-plans"])


class InvigilatorAssignmentRead(BaseModel):
    assignment_id: str
    invigilator_id: str
    name: str
    email: Optional[str] = None
    is_primary: bool


class RoomInvigilatorInfo(BaseModel):
    room_id: str
    room_name: str
    assignments: List[InvigilatorAssignmentRead]


class InvigilatorPlanRead(BaseModel):
    id: str
    course: str
    exam_date: Optional[datetime] = None
    start_time: Optional[str] = None
    uploaded_at: datetime
    status: str
    rooms: List[RoomInvigilatorInfo]


class InvigilatorPlanListResponse(BaseModel):
    plans: List[InvigilatorPlanRead]
    total: int
    page: int
    limit: int


class AssignInvigilatorBody(BaseModel):
    room_id: UUID
    invigilator_id: UUID


class RoomAssignmentStatus(BaseModel):
    assigned: bool
    invigilator_name: Optional[str] = None
    message: Optional[str] = None


def _room_display_name(room: Room) -> str:
    if room.block:
        return f"{room.block}-{room.room_number}"
    return str(room.room_number)


@router.get("/", response_model=InvigilatorPlanListResponse)
def list_invigilator_plans(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    plan_status: Optional[str] = Query(None, alias="status", regex="^(completed|processing)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Exams that have at least one room (same basis as seating plans)."""
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Access denied")

    room_exam_ids = db.query(Room.exam_id).distinct().filter(Room.exam_id.isnot(None)).all()
    exam_ids = [e[0] for e in room_exam_ids if e[0] is not None]
    if not exam_ids:
        return InvigilatorPlanListResponse(plans=[], total=0, page=page, limit=limit)

    q = db.query(Exam).filter(Exam.exam_id.in_(exam_ids))
    today = date_class.today()
    if plan_status == "completed":
        q = q.filter(Exam.exam_date < today)
    elif plan_status == "processing":
        q = q.filter(Exam.exam_date >= today)
    exams = q.order_by(Exam.created_at.desc()).all()

    all_rooms = db.query(Room).filter(Room.exam_id.in_([e.exam_id for e in exams])).all()
    room_ids = [r.room_id for r in all_rooms]
    all_assignments = []
    if room_ids:
        all_assignments = (
            db.query(ExamInvigilatorAssignment)
            .filter(ExamInvigilatorAssignment.room_id.in_(room_ids))
            .all()
        )

    inv_ids = list({a.invigilator_id for a in all_assignments})
    inv_by_id = {}
    if inv_ids:
        for inv in db.query(Invigilator).filter(Invigilator.invigilator_id.in_(inv_ids)).all():
            inv_by_id[inv.invigilator_id] = inv

    rooms_by_exam = {}
    for r in all_rooms:
        rooms_by_exam.setdefault(r.exam_id, []).append(r)

    assign_by_room = {}
    for a in all_assignments:
        assign_by_room.setdefault(a.room_id, []).append(a)

    plans: List[InvigilatorPlanRead] = []
    for exam in exams:
        rooms_out: List[RoomInvigilatorInfo] = []
        for room in rooms_by_exam.get(exam.exam_id, []):
            assigns = assign_by_room.get(room.room_id, [])
            reads: List[InvigilatorAssignmentRead] = []
            for asn in assigns:
                inv = inv_by_id.get(asn.invigilator_id)
                reads.append(
                    InvigilatorAssignmentRead(
                        assignment_id=str(asn.assignment_id),
                        invigilator_id=str(asn.invigilator_id),
                        name=inv.name if inv else "Unknown",
                        email=inv.email if inv else None,
                        is_primary=bool(asn.is_primary),
                    )
                )
            rooms_out.append(
                RoomInvigilatorInfo(
                    room_id=str(room.room_id),
                    room_name=_room_display_name(room),
                    assignments=reads,
                )
            )
        st = "completed" if exam.exam_date and exam.exam_date < today else "processing"
        plans.append(
            InvigilatorPlanRead(
                id=str(exam.exam_id),
                course=exam.course,
                exam_date=datetime.combine(exam.exam_date, datetime.min.time()) if exam.exam_date else None,
                start_time=exam.start_time.isoformat() if exam.start_time else None,
                uploaded_at=exam.created_at or datetime.utcnow(),
                status=st,
                rooms=rooms_out,
            )
        )

    plans.sort(key=lambda x: x.uploaded_at or datetime(1970, 1, 1), reverse=True)
    total = len(plans)
    offset = (page - 1) * limit
    return InvigilatorPlanListResponse(
        plans=plans[offset : offset + limit],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/assignment-check", response_model=RoomAssignmentStatus)
def check_room_invigilator_assignment(
    exam_id: UUID = Query(..., description="Exam UUID"),
    room_id: UUID = Query(..., description="Room UUID"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Whether this exam room has an invigilator assigned (required before upload/recording)."""
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Access denied")
    room = db.query(Room).filter(Room.room_id == room_id).first()
    if not room:
        return RoomAssignmentStatus(assigned=False, message="Room not found")
    if room.exam_id != exam_id:
        return RoomAssignmentStatus(
            assigned=False,
            message="This room does not belong to the selected exam",
        )
    asn = (
        db.query(ExamInvigilatorAssignment)
        .filter(ExamInvigilatorAssignment.room_id == room_id)
        .first()
    )
    if not asn:
        return RoomAssignmentStatus(
            assigned=False,
            message="Assign an invigilator to this room in Invigilator Plans before uploading or recording exam footage.",
        )
    inv = db.query(Invigilator).filter(Invigilator.invigilator_id == asn.invigilator_id).first()
    return RoomAssignmentStatus(
        assigned=True,
        invigilator_name=inv.name if inv else None,
        message=None,
    )


@router.get("/{exam_id}", response_model=InvigilatorPlanRead)
def get_invigilator_plan(
    exam_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Access denied")
    exam = db.query(Exam).filter(Exam.exam_id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    rooms = db.query(Room).filter(Room.exam_id == exam_id).all()
    room_ids = [r.room_id for r in rooms]
    assigns = []
    if room_ids:
        assigns = (
            db.query(ExamInvigilatorAssignment)
            .filter(ExamInvigilatorAssignment.room_id.in_(room_ids))
            .all()
        )
    inv_ids = list({a.invigilator_id for a in assigns})
    inv_by_id = {}
    if inv_ids:
        for inv in db.query(Invigilator).filter(Invigilator.invigilator_id.in_(inv_ids)).all():
            inv_by_id[inv.invigilator_id] = inv
    assign_by_room = {}
    for a in assigns:
        assign_by_room.setdefault(a.room_id, []).append(a)
    today = date_class.today()
    st = "completed" if exam.exam_date and exam.exam_date < today else "processing"
    rooms_out = []
    for room in rooms:
        reads = []
        for asn in assign_by_room.get(room.room_id, []):
            inv = inv_by_id.get(asn.invigilator_id)
            reads.append(
                InvigilatorAssignmentRead(
                    assignment_id=str(asn.assignment_id),
                    invigilator_id=str(asn.invigilator_id),
                    name=inv.name if inv else "Unknown",
                    email=inv.email if inv else None,
                    is_primary=bool(asn.is_primary),
                )
            )
        rooms_out.append(
            RoomInvigilatorInfo(
                room_id=str(room.room_id),
                room_name=_room_display_name(room),
                assignments=reads,
            )
        )
    return InvigilatorPlanRead(
        id=str(exam.exam_id),
        course=exam.course,
        exam_date=datetime.combine(exam.exam_date, datetime.min.time()) if exam.exam_date else None,
        start_time=exam.start_time.isoformat() if exam.start_time else None,
        uploaded_at=exam.created_at or datetime.utcnow(),
        status=st,
        rooms=rooms_out,
    )


@router.post("/{exam_id}/assign", response_model=InvigilatorPlanRead)
def assign_invigilator_to_room(
    exam_id: UUID,
    body: AssignInvigilatorBody,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can assign invigilators")
    exam = db.query(Exam).filter(Exam.exam_id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    room = db.query(Room).filter(Room.room_id == body.room_id, Room.exam_id == exam_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found for this exam")
    inv = db.query(Invigilator).filter(Invigilator.invigilator_id == body.invigilator_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invigilator not found")
    # Exactly one invigilator per room: replace any existing assignment
    db.query(ExamInvigilatorAssignment).filter(
        ExamInvigilatorAssignment.room_id == body.room_id
    ).delete(synchronize_session=False)
    row = ExamInvigilatorAssignment(
        exam_id=exam_id,
        room_id=body.room_id,
        invigilator_id=body.invigilator_id,
        is_primary=True,
    )
    db.add(row)
    db.commit()
    return get_invigilator_plan(exam_id, db, current_user)


@router.delete("/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_assignment(
    assignment_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can remove assignments")
    row = db.query(ExamInvigilatorAssignment).filter(
        ExamInvigilatorAssignment.assignment_id == assignment_id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    db.delete(row)
    db.commit()
    return None
