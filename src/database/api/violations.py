from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import desc
from sqlalchemy.orm import Session
from uuid import UUID
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel

from database.db import get_db
from database.models import Violation, Student, StudentActivity
from database.auth import get_current_user
from database.activity_enrichment import enrich_violations
from database.severity_logic import severity_to_int as activity_severity_to_int

router = APIRouter(prefix="/violations", tags=["Violations"])

# -------------------------
# Pydantic Schemas
# -------------------------
class ViolationCreate(BaseModel):
    """Create a violation from a reviewed student activity. Omit type/severity to copy from the activity."""

    activity_id: UUID
    violation_type: Optional[str] = None
    severity: Optional[int] = None
    status: Optional[str] = "pending"
    evidence_url: Optional[str] = None


class ViolationRead(BaseModel):
    violation_id: UUID
    activity_id: UUID
    violation_type: str
    timestamp: datetime
    severity: int
    status: str
    evidence_url: Optional[str]
    student_id: Optional[UUID] = None
    student_name: Optional[str] = None
    exam_id: Optional[UUID] = None
    exam_name: Optional[str] = None
    seat_number: Optional[str] = None
    room: Optional[str] = None

    model_config = {
        "from_attributes": True
    }


class ViolationUpdate(BaseModel):
    violation_type: Optional[str] = None
    severity: Optional[int] = None
    status: Optional[str] = None
    evidence_url: Optional[str] = None


# -------------------------
# CRUD Routes
# -------------------------

# CREATE (Admin + Investigator): promote a reviewed student activity to a formal violation record
@router.post("/", response_model=ViolationRead)
def create_violation(
    violation: ViolationCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Admins and investigators create violations after reviewing student activities.
    At most one violation per activity; if one already exists, returns it with 200.
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(
            status_code=403,
            detail="Only admins and investigators can create violations from student activities",
        )

    activity = db.query(StudentActivity).filter(StudentActivity.activity_id == violation.activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Student activity not found")

    existing = db.query(Violation).filter(Violation.activity_id == violation.activity_id).first()
    if existing:
        response.status_code = status.HTTP_200_OK
        enriched = enrich_violations(db, [existing])
        return ViolationRead(**enriched[0])

    vtype = violation.violation_type or (activity.activity_type or "Unknown")
    sev = violation.severity
    if sev is None:
        sev = activity_severity_to_int(str(activity.severity or "low"))
    evid = violation.evidence_url or activity.evidence_url
    ts = activity.timestamp or datetime.utcnow()

    new_violation = Violation(
        activity_id=violation.activity_id,
        violation_type=vtype,
        severity=sev,
        status=violation.status or "pending",
        evidence_url=evid,
        timestamp=ts,
    )
    db.add(new_violation)
    db.commit()
    db.refresh(new_violation)
    response.status_code = status.HTTP_201_CREATED
    enriched = enrich_violations(db, [new_violation])
    return ViolationRead(**enriched[0])


# READ All (Admin + Investigator)
@router.get("/", response_model=List[ViolationRead])
def get_all_violations(
    exam_id: Optional[UUID] = Query(
        None,
        description="If set, only violations tied to a student activity in this exam.",
    ),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Admins and Investigators can view all violations.
    Optional exam_id filters to violations from that exam only.
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    if exam_id is None:
        rows = (
            db.query(Violation)
            .order_by(desc(Violation.severity), Violation.timestamp.asc())
            .all()
        )
    else:
        rows = (
            db.query(Violation)
            .join(StudentActivity, Violation.activity_id == StudentActivity.activity_id)
            .filter(StudentActivity.exam_id == exam_id)
            .order_by(desc(Violation.severity), Violation.timestamp.asc())
            .all()
        )
    return [ViolationRead(**d) for d in enrich_violations(db, rows)]


# READ by Student ID — students see only outcomes after investigator review (not pending, not raw activities)
@router.get("/student/{student_id}", response_model=List[ViolationRead])
def get_violations_by_student_id(
    student_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Violations linked to this student's activities.

    - **Students**: only violations with status `confirmed` or `dismissed` (investigator has decided).
    - **Admin / investigator**: all violation rows for this student (any status).
    """
    user_type = current_user.get("user_type")
    user_id = current_user.get("id")

    if user_type == "invigilator":
        raise HTTPException(status_code=403, detail="Invigilators are not allowed to access this resource")

    if user_type == "student" and str(user_id) != str(student_id):
        raise HTTPException(status_code=403, detail="Students can only view their own violations")

    if not db.query(Student).filter(Student.student_id == student_id).first():
        raise HTTPException(status_code=404, detail="Student not found")

    q = (
        db.query(Violation)
        .join(StudentActivity, Violation.activity_id == StudentActivity.activity_id)
        .filter(StudentActivity.student_id == student_id)
    )
    if user_type == "student":
        q = q.filter(Violation.status.in_(["confirmed", "dismissed"]))
    rows = q.order_by(desc(Violation.timestamp)).all()
    return [ViolationRead(**d) for d in enrich_violations(db, rows)]


# READ by ID (Admin + Investigator)
@router.get("/{violation_id}", response_model=ViolationRead)
def get_violation(
    violation_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Admins and Investigators can view a specific violation.
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    violation = db.query(Violation).filter(Violation.violation_id == violation_id).first()
    if not violation:
        raise HTTPException(status_code=404, detail="Violation not found")

    enriched = enrich_violations(db, [violation])
    return ViolationRead(**enriched[0])


# UPDATE: admins may change any field; investigators may only update status and evidence_url (review workflow)
@router.put("/{violation_id}", response_model=ViolationRead)
def update_violation(
    violation_id: UUID,
    updated: ViolationUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_type = current_user.get("user_type")
    if user_type not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    violation = db.query(Violation).filter(Violation.violation_id == violation_id).first()
    if not violation:
        raise HTTPException(status_code=404, detail="Violation not found")

    updates = updated.dict(exclude_unset=True)
    if user_type == "investigator":
        allowed = {"status", "evidence_url"}
        bad = set(updates.keys()) - allowed
        if bad:
            raise HTTPException(
                status_code=403,
                detail="Investigators may only update status and evidence_url on violations",
            )
    for key, value in updates.items():
        setattr(violation, key, value)

    db.commit()
    db.refresh(violation)
    enriched = enrich_violations(db, [violation])
    return ViolationRead(**enriched[0])


# DELETE (Admin Only)
@router.delete("/{violation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_violation(
    violation_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can delete violations.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can delete violations")

    violation = db.query(Violation).filter(Violation.violation_id == violation_id).first()
    if not violation:
        raise HTTPException(status_code=404, detail="Violation not found")

    db.delete(violation)
    db.commit()
    return None


# READ by Activity ID (Admin, Investigator, or the Student themselves)
@router.get("/activity/{activity_id}", response_model=List[ViolationRead])
def get_violations_by_activity_id(
    activity_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    user_type = current_user.get("user_type")
    user_id = current_user.get("id")

    if user_type == "invigilator":
        raise HTTPException(status_code=403, detail="Invigilators are not allowed to access this resource")


    activity = db.query(StudentActivity).filter(StudentActivity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Student activity not found")

    if user_type == "student" and str(activity.student_id) != str(user_id):
        raise HTTPException(status_code=403, detail="Students can only view violations for their own activities")

    violations = db.query(Violation).filter(Violation.activity_id == activity_id).all()
    if not violations:
        return []
    return [ViolationRead(**d) for d in enrich_violations(db, violations)]
