from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from uuid import UUID
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel

from database.db import get_db
from database.models import StudentActivity, Student, Exam
from database.auth import get_current_user
from database.severity_logic import compute_severity
from database.activity_enrichment import enrich_activities

router = APIRouter(prefix="/student-activities", tags=["Student Activities"])

# -------------------------
# Pydantic Schemas
# -------------------------
class StudentActivityCreate(BaseModel):
    student_id: UUID
    exam_id: UUID
    activity_type: str
    severity: Optional[str] = None
    confidence: Optional[float] = None
    evidence_url: Optional[str] = None


class StudentActivityRead(BaseModel):
    activity_id: UUID
    student_id: UUID
    exam_id: UUID
    timestamp: datetime
    activity_type: str
    severity: Optional[str]
    confidence: Optional[float]
    evidence_url: Optional[str]
    run_frame_count: Optional[int] = None
    severity_rule: Optional[str] = None
    student_name: Optional[str] = None
    exam_name: Optional[str] = None
    seat_number: Optional[str] = None
    room: Optional[str] = None

    model_config = {
        "from_attributes": True
    }


class StudentActivityUpdate(BaseModel):
    activity_type: Optional[str] = None
    severity: Optional[str] = None
    confidence: Optional[float] = None
    evidence_url: Optional[str] = None
    run_frame_count: Optional[int] = None
    severity_rule: Optional[str] = None


# -------------------------
# CRUD Routes
# -------------------------

# CREATE (Admin Only)
@router.post("/", response_model=StudentActivityRead, status_code=status.HTTP_201_CREATED)
def create_student_activity(
    activity: StudentActivityCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can create student activity records.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create student activities")

    # Validate student
    student = db.query(Student).filter(Student.student_id == activity.student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Validate exam
    exam = db.query(Exam).filter(Exam.exam_id == activity.exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    # Frequency-based severity: if not provided, compute from how often this student
    # has done this action in this exam
    severity = activity.severity
    if not severity or (isinstance(severity, str) and not severity.strip()):
        severity = compute_severity(
            activity.student_id,
            activity.exam_id,
            activity.activity_type,
            db,
        )
    payload = activity.dict()
    payload["severity"] = severity
    new_activity = StudentActivity(**payload)
    db.add(new_activity)
    db.commit()
    db.refresh(new_activity)
    enriched = enrich_activities(db, [new_activity])
    return StudentActivityRead(**enriched[0])


# READ All (Admin + Investigator)
@router.get("/", response_model=List[StudentActivityRead])
def get_all_student_activities(
    exam_id: Optional[UUID] = Query(None, description="Filter by exam"),
    student_id: Optional[UUID] = Query(None, description="Filter by student"),
    severity: Optional[str] = Query(None, description="Filter by severity label"),
    activity_type: Optional[str] = Query(None, description="Filter by activity type"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Admins and Investigators can view all student activities.
    Optional query parameters filter the result set (aligned with client query string).
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    q = db.query(StudentActivity)
    if exam_id is not None:
        q = q.filter(StudentActivity.exam_id == exam_id)
    if student_id is not None:
        q = q.filter(StudentActivity.student_id == student_id)
    if severity is not None and severity.strip():
        q = q.filter(StudentActivity.severity == severity)
    if activity_type is not None and activity_type.strip():
        q = q.filter(StudentActivity.activity_type == activity_type)

    rows = q.all()
    return [StudentActivityRead(**d) for d in enrich_activities(db, rows)]


# READ by ID (Admin + Investigator)
@router.get("/{activity_id}", response_model=StudentActivityRead)
def get_student_activity(
    activity_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Admins and Investigators can view a specific student activity.
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    activity = db.query(StudentActivity).filter(StudentActivity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Student activity not found")

    enriched = enrich_activities(db, [activity])
    return StudentActivityRead(**enriched[0])


# UPDATE (Admin Only)
@router.put("/{activity_id}", response_model=StudentActivityRead)
def update_student_activity(
    activity_id: UUID,
    updated: StudentActivityUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can update student activity records.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can update student activities")

    activity = db.query(StudentActivity).filter(StudentActivity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Student activity not found")

    for key, value in updated.dict(exclude_unset=True).items():
        setattr(activity, key, value)

    db.commit()
    db.refresh(activity)
    enriched = enrich_activities(db, [activity])
    return StudentActivityRead(**enriched[0])


# DELETE (Admin Only)
@router.delete("/{activity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_student_activity(
    activity_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can delete student activity records.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can delete student activities")

    activity = db.query(StudentActivity).filter(StudentActivity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Student activity not found")

    db.delete(activity)
    db.commit()
    return None


# READ by Student ID (Accessible by Admin, Investigator, and the Student themselves)
@router.get("/student/{student_id}", response_model=List[StudentActivityRead])
def get_activities_by_student_id(
    student_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all activities for a specific student.
    Returns empty list if no activities found (instead of 404).
    """
    user_type = current_user.get("user_type")
    user_id = current_user.get("id")

    if user_type == "invigilator":
        raise HTTPException(status_code=403, detail="Invigilators are not allowed to access this resource")

    if user_type == "student" and str(user_id) != str(student_id):
        raise HTTPException(status_code=403, detail="Students can only view their own activities")

    # Verify student exists
    student = db.query(Student).filter(Student.student_id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    activities = db.query(StudentActivity).filter(StudentActivity.student_id == student_id).all()

    return [StudentActivityRead(**d) for d in enrich_activities(db, activities)]
