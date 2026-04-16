from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from uuid import UUID
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel

from database.db import get_db
from database.models import InvigilatorActivity, Invigilator, Room
from database.auth import get_current_user

router = APIRouter(prefix="/invigilator-activities", tags=["Invigilator Activities"])

# -------------------------
# Pydantic Schemas
# -------------------------
class InvigilatorActivityCreate(BaseModel):
    invigilator_id: UUID
    room_id: UUID
    activity_type: str
    notes: Optional[str] = None


class InvigilatorActivityRead(BaseModel):
    activity_id: UUID
    invigilator_id: UUID
    room_id: UUID
    timestamp: datetime
    activity_type: str
    notes: Optional[str]
    invigilator_name: Optional[str] = None
    room_number: Optional[str] = None
    block: Optional[str] = None

    model_config = {
        "from_attributes": True
    }

class InvigilatorActivityDetailedRead(BaseModel):
    activity_id: UUID
    invigilator_id: UUID
    room_id: UUID
    timestamp: datetime
    activity_type: str
    notes: Optional[str]
    room_number: Optional[str] = None
    block: Optional[str] = None

    model_config = {
        "from_attributes": True
    }


class InvigilatorActivityUpdate(BaseModel):
    activity_type: Optional[str] = None
    notes: Optional[str] = None


def _enrich_invigilator_activities(
    db: Session, activities: list[InvigilatorActivity]
) -> list[dict]:
    if not activities:
        return []
    iids = {a.invigilator_id for a in activities if a.invigilator_id}
    rids = {a.room_id for a in activities if a.room_id}
    invs = (
        {i.invigilator_id: i for i in db.query(Invigilator).filter(Invigilator.invigilator_id.in_(iids)).all()}
        if iids
        else {}
    )
    rooms = (
        {r.room_id: r for r in db.query(Room).filter(Room.room_id.in_(rids)).all()}
        if rids
        else {}
    )
    out = []
    for a in activities:
        inv = invs.get(a.invigilator_id)
        rm = rooms.get(a.room_id)
        out.append({
            "activity_id": a.activity_id,
            "invigilator_id": a.invigilator_id,
            "room_id": a.room_id,
            "timestamp": a.timestamp,
            "activity_type": a.activity_type,
            "notes": a.notes,
            "invigilator_name": inv.name if inv else None,
            "room_number": rm.room_number if rm else None,
            "block": rm.block if rm else None,
        })
    return out


# -------------------------
# CRUD Routes
# -------------------------

# CREATE (Admin Only)
@router.post("/", response_model=InvigilatorActivityRead, status_code=status.HTTP_201_CREATED)
def create_invigilator_activity(
    activity: InvigilatorActivityCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can create invigilator activity records.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create invigilator activities")

    # Validate invigilator
    invigilator = db.query(Invigilator).filter(Invigilator.invigilator_id == activity.invigilator_id).first()
    if not invigilator:
        raise HTTPException(status_code=404, detail="Invigilator not found")

    # Validate room
    room = db.query(Room).filter(Room.room_id == activity.room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    new_activity = InvigilatorActivity(**activity.dict())
    db.add(new_activity)
    db.commit()
    db.refresh(new_activity)
    row = _enrich_invigilator_activities(db, [new_activity])
    return InvigilatorActivityRead(**row[0])


# READ All (Admin + Investigator)
@router.get("/", response_model=List[InvigilatorActivityRead])
def get_all_invigilator_activities(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Admins and Investigators can view all invigilator activities.
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    rows = db.query(InvigilatorActivity).order_by(InvigilatorActivity.timestamp.desc()).all()
    enriched = _enrich_invigilator_activities(db, rows)
    return [InvigilatorActivityRead(**d) for d in enriched]


@router.get("/me", response_model=List[InvigilatorActivityDetailedRead])
def get_my_invigilator_activities(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Invigilators can view only their own activities.
    Admins and investigators can also call this endpoint but it will
    return an empty list unless the caller is an invigilator.
    """
    if current_user.get("user_type") == "invigilator":
        invigilator_id = UUID(current_user.get("id"))
        activities = (
            db.query(InvigilatorActivity, Room)
            .join(Room, InvigilatorActivity.room_id == Room.room_id, isouter=True)
            .filter(InvigilatorActivity.invigilator_id == invigilator_id)
            .order_by(InvigilatorActivity.timestamp.desc())
            .all()
        )

        return [
            InvigilatorActivityDetailedRead(
                activity_id=activity.activity_id,
                invigilator_id=activity.invigilator_id,
                room_id=activity.room_id,
                timestamp=activity.timestamp,
                activity_type=activity.activity_type,
                notes=activity.notes,
                room_number=room.room_number if room else None,
                block=room.block if room else None,
            )
            for activity, room in activities
        ]

    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return []


# READ by ID (Admin + Investigator)
@router.get("/{activity_id}", response_model=InvigilatorActivityRead)
def get_invigilator_activity(
    activity_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Admins and Investigators can view a specific invigilator activity.
    """
    if current_user.get("user_type") not in ["admin", "investigator"]:
        raise HTTPException(status_code=403, detail="Access denied")

    activity = db.query(InvigilatorActivity).filter(InvigilatorActivity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Invigilator activity not found")

    row = _enrich_invigilator_activities(db, [activity])
    return InvigilatorActivityRead(**row[0])


# UPDATE (Admin Only)
@router.put("/{activity_id}", response_model=InvigilatorActivityRead)
def update_invigilator_activity(
    activity_id: UUID,
    updated: InvigilatorActivityUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can update invigilator activity records.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can update invigilator activities")

    activity = db.query(InvigilatorActivity).filter(InvigilatorActivity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Invigilator activity not found")

    for key, value in updated.dict(exclude_unset=True).items():
        setattr(activity, key, value)

    db.commit()
    db.refresh(activity)
    row = _enrich_invigilator_activities(db, [activity])
    return InvigilatorActivityRead(**row[0])


# DELETE (Admin Only)
@router.delete("/{activity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_invigilator_activity(
    activity_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Only admins can delete invigilator activity records.
    """
    if current_user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can delete invigilator activities")

    activity = db.query(InvigilatorActivity).filter(InvigilatorActivity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Invigilator activity not found")

    db.delete(activity)
    db.commit()
    return None


