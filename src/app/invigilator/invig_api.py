"""
Invigilator Monitoring API
==========================
FastAPI router exposing invigilator-detection results from the video
processing pipeline.

Endpoints
---------
GET  /api/invigilator-monitoring/activities
     All activities (admin / investigator only, paginated).

GET  /api/invigilator-monitoring/invigilator/{invigilator_id}/activities
     Activities for a specific invigilator.

GET  /api/invigilator-monitoring/room/{room_id}/activities
     Activities for a specific room (optionally filtered by date range).

GET  /api/invigilator-monitoring/stream/{stream_id}/activities
     Activities recorded during a specific video-stream processing run.

GET  /api/invigilator-monitoring/stream/{stream_id}/summary
     Aggregated summary counts for a processed stream.

GET  /api/invigilator-monitoring/exam/{exam_id}/activities
     All invigilator activities across all rooms for an exam.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from database.db import get_db
from database.models import (
    InvigilatorActivity,
    Invigilator,
    Room,
    VideoStream,
    ExamRoomAssignment,
)
from database.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/invigilator-monitoring",
    tags=["Invigilator Monitoring"],
)


# ---------------------------------------------------------------------------
#  Pydantic schemas
# ---------------------------------------------------------------------------

class InvigilatorActivityOut(BaseModel):
    activity_id: UUID
    invigilator_id: Optional[UUID] = None
    invigilator_name: Optional[str] = None
    room_id: UUID
    room_number: Optional[str] = None
    block: Optional[str] = None
    timestamp: str
    activity_type: str
    severity: Optional[str] = None
    confidence: Optional[float] = None
    frame_number: Optional[int] = None
    evidence_url: Optional[str] = None
    notes: Optional[str] = None

    model_config = {"from_attributes": True}


class ActivitySummaryOut(BaseModel):
    stream_id: str
    room_id: Optional[str] = None
    exam_id: Optional[str] = None
    total_activities: int
    by_type: dict
    by_severity: dict
    invigilators_detected: List[str]
    time_range: dict


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _serialize_dt(dt) -> str:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        from datetime import timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def _build_activity_out(
    activity: InvigilatorActivity,
    invigilator: Invigilator | None,
    room: Room | None,
) -> dict:
    return {
        "activity_id": str(activity.activity_id),
        "invigilator_id": str(activity.invigilator_id) if activity.invigilator_id else None,
        "invigilator_name": invigilator.name if invigilator else None,
        "room_id": str(activity.room_id) if activity.room_id else None,
        "room_number": room.room_number if room else None,
        "block": room.block if room else None,
        "timestamp": _serialize_dt(activity.timestamp),
        "activity_type": activity.activity_type,
        "severity": activity.severity,
        "confidence": float(activity.confidence) if activity.confidence is not None else None,
        "frame_number": activity.frame_number,
        "evidence_url": activity.evidence_url,
        "notes": activity.notes,
    }


# ---------------------------------------------------------------------------
#  Endpoints
# ---------------------------------------------------------------------------

@router.get("/activities")
def list_all_activities(
    limit: int = Query(100, ge=1, le=1000, description="Max records to return"),
    offset: int = Query(0, ge=0, description="Records to skip"),
    activity_type: Optional[str] = Query(None, description="Filter by activity type"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    List all invigilator activities.  Accessible by **admin** and **investigator**.
    """
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        q = db.query(InvigilatorActivity)
        if activity_type:
            q = q.filter(InvigilatorActivity.activity_type == activity_type)
        if severity:
            q = q.filter(InvigilatorActivity.severity == severity)

        total = q.count()
        records = (
            q.order_by(InvigilatorActivity.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        # Batch-load related objects to avoid N+1
        invig_ids = {r.invigilator_id for r in records if r.invigilator_id}
        room_ids = {r.room_id for r in records if r.room_id}

        invigs = {
            i.invigilator_id: i
            for i in db.query(Invigilator).filter(Invigilator.invigilator_id.in_(invig_ids)).all()
        }
        rooms = {
            r.room_id: r
            for r in db.query(Room).filter(Room.room_id.in_(room_ids)).all()
        }

        return {
            "success": True,
            "data": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "activities": [
                    _build_activity_out(rec, invigs.get(rec.invigilator_id), rooms.get(rec.room_id))
                    for rec in records
                ],
            },
        }
    except SQLAlchemyError as e:
        log.error("DB error listing invigilator activities: %s", e)
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/invigilator/{invigilator_id}/activities")
def get_activities_by_invigilator(
    invigilator_id: UUID,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Activities for a specific invigilator.

    - **Invigilator** can only access their own activities.
    - **Admin** and **Investigator** can access any invigilator's activities.
    """
    user_type = current_user.get("user_type")
    if user_type == "invigilator":
        if str(invigilator_id) != current_user.get("id"):
            raise HTTPException(status_code=403, detail="Access denied")
    elif user_type not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Access denied")

    invigilator = db.query(Invigilator).filter(
        Invigilator.invigilator_id == invigilator_id
    ).first()
    if not invigilator:
        raise HTTPException(status_code=404, detail="Invigilator not found")

    try:
        q = (
            db.query(InvigilatorActivity)
            .filter(InvigilatorActivity.invigilator_id == invigilator_id)
        )
        total = q.count()
        records = (
            q.order_by(InvigilatorActivity.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        room_ids = {r.room_id for r in records if r.room_id}
        rooms = {
            r.room_id: r
            for r in db.query(Room).filter(Room.room_id.in_(room_ids)).all()
        }

        return {
            "success": True,
            "data": {
                "invigilator_id": str(invigilator_id),
                "invigilator_name": invigilator.name,
                "total": total,
                "limit": limit,
                "offset": offset,
                "activities": [
                    _build_activity_out(rec, invigilator, rooms.get(rec.room_id))
                    for rec in records
                ],
            },
        }
    except SQLAlchemyError as e:
        log.error("DB error: %s", e)
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/room/{room_id}/activities")
def get_activities_by_room(
    room_id: UUID,
    since: Optional[datetime] = Query(None, description="Filter activities after this datetime (ISO 8601)"),
    until: Optional[datetime] = Query(None, description="Filter activities before this datetime (ISO 8601)"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Activities detected in a specific room, with optional time-range filter.
    Admin and investigator only.
    """
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Access denied")

    room = db.query(Room).filter(Room.room_id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    try:
        q = db.query(InvigilatorActivity).filter(
            InvigilatorActivity.room_id == room_id
        )
        if since:
            q = q.filter(InvigilatorActivity.timestamp >= since)
        if until:
            q = q.filter(InvigilatorActivity.timestamp <= until)

        total = q.count()
        records = (
            q.order_by(InvigilatorActivity.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        invig_ids = {r.invigilator_id for r in records if r.invigilator_id}
        invigs = {
            i.invigilator_id: i
            for i in db.query(Invigilator).filter(Invigilator.invigilator_id.in_(invig_ids)).all()
        }

        return {
            "success": True,
            "data": {
                "room_id": str(room_id),
                "room_number": room.room_number,
                "block": room.block,
                "total": total,
                "limit": limit,
                "offset": offset,
                "activities": [
                    _build_activity_out(rec, invigs.get(rec.invigilator_id), room)
                    for rec in records
                ],
            },
        }
    except SQLAlchemyError as e:
        log.error("DB error: %s", e)
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/stream/{stream_id}/activities")
def get_activities_by_stream(
    stream_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Invigilator activities recorded during a specific video-stream processing run.

    The endpoint resolves the room and processing-time window from the
    VideoStream record and returns all InvigilatorActivity rows that fall
    within that window for the same room.
    """
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Access denied")

    stream = db.query(VideoStream).filter(VideoStream.stream_id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Video stream not found")

    try:
        q = db.query(InvigilatorActivity).filter(
            InvigilatorActivity.room_id == stream.room_id
        )
        # Narrow by processing time window when available
        if stream.started_at:
            q = q.filter(InvigilatorActivity.timestamp >= stream.started_at)
        if stream.completed_at:
            q = q.filter(InvigilatorActivity.timestamp <= stream.completed_at)

        records = q.order_by(InvigilatorActivity.timestamp.asc()).all()

        invig_ids = {r.invigilator_id for r in records if r.invigilator_id}
        invigs = {
            i.invigilator_id: i
            for i in db.query(Invigilator).filter(Invigilator.invigilator_id.in_(invig_ids)).all()
        }
        room = db.query(Room).filter(Room.room_id == stream.room_id).first()

        return {
            "success": True,
            "data": {
                "stream_id": str(stream_id),
                "room_id": str(stream.room_id) if stream.room_id else None,
                "exam_id": str(stream.exam_id) if stream.exam_id else None,
                "total": len(records),
                "activities": [
                    _build_activity_out(rec, invigs.get(rec.invigilator_id), room)
                    for rec in records
                ],
            },
        }
    except SQLAlchemyError as e:
        log.error("DB error: %s", e)
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/stream/{stream_id}/summary")
def get_stream_summary(
    stream_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Aggregated invigilator-activity summary for a processed video stream.
    Returns counts by activity type, severity, and a list of invigilators
    detected during the stream.
    """
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Access denied")

    stream = db.query(VideoStream).filter(VideoStream.stream_id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Video stream not found")

    try:
        q = db.query(InvigilatorActivity).filter(
            InvigilatorActivity.room_id == stream.room_id
        )
        if stream.started_at:
            q = q.filter(InvigilatorActivity.timestamp >= stream.started_at)
        if stream.completed_at:
            q = q.filter(InvigilatorActivity.timestamp <= stream.completed_at)

        records = q.all()

        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        invig_ids_seen: set[str] = set()
        earliest: datetime | None = None
        latest: datetime | None = None

        for rec in records:
            t = rec.activity_type or "Unknown"
            by_type[t] = by_type.get(t, 0) + 1

            s = rec.severity or "unknown"
            by_severity[s] = by_severity.get(s, 0) + 1

            if rec.invigilator_id:
                invig_ids_seen.add(str(rec.invigilator_id))

            if rec.timestamp:
                if earliest is None or rec.timestamp < earliest:
                    earliest = rec.timestamp
                if latest is None or rec.timestamp > latest:
                    latest = rec.timestamp

        # Resolve invigilator names
        invigs = (
            db.query(Invigilator)
            .filter(
                Invigilator.invigilator_id.in_(
                    [UUID(i) for i in invig_ids_seen]
                )
            )
            .all()
            if invig_ids_seen
            else []
        )
        invigilators_detected = [
            {"id": str(i.invigilator_id), "name": i.name} for i in invigs
        ]

        return {
            "success": True,
            "data": {
                "stream_id": str(stream_id),
                "room_id": str(stream.room_id) if stream.room_id else None,
                "exam_id": str(stream.exam_id) if stream.exam_id else None,
                "total_activities": len(records),
                "by_type": by_type,
                "by_severity": by_severity,
                "invigilators_detected": invigilators_detected,
                "time_range": {
                    "earliest": _serialize_dt(earliest),
                    "latest": _serialize_dt(latest),
                    "stream_started": _serialize_dt(stream.started_at),
                    "stream_completed": _serialize_dt(stream.completed_at),
                },
            },
        }
    except SQLAlchemyError as e:
        log.error("DB error: %s", e)
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/exam/{exam_id}/activities")
def get_activities_by_exam(
    exam_id: UUID,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    All invigilator activities across all rooms for a given exam.
    Admin and investigator only.
    """
    if current_user.get("user_type") not in ("admin", "investigator"):
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        # Get all rooms for the exam
        room_ids = [
            r.room_id
            for r in db.query(Room).filter(Room.exam_id == exam_id).all()
        ]
        if not room_ids:
            return {
                "success": True,
                "data": {
                    "exam_id": str(exam_id),
                    "total": 0,
                    "activities": [],
                },
            }

        q = db.query(InvigilatorActivity).filter(
            InvigilatorActivity.room_id.in_(room_ids)
        )
        total = q.count()
        records = (
            q.order_by(InvigilatorActivity.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        invig_ids = {r.invigilator_id for r in records if r.invigilator_id}
        invigs = {
            i.invigilator_id: i
            for i in db.query(Invigilator).filter(Invigilator.invigilator_id.in_(invig_ids)).all()
        }
        rooms = {
            r.room_id: r
            for r in db.query(Room).filter(Room.room_id.in_(set(room_ids))).all()
        }

        return {
            "success": True,
            "data": {
                "exam_id": str(exam_id),
                "total": total,
                "limit": limit,
                "offset": offset,
                "activities": [
                    _build_activity_out(rec, invigs.get(rec.invigilator_id), rooms.get(rec.room_id))
                    for rec in records
                ],
            },
        }
    except SQLAlchemyError as e:
        log.error("DB error: %s", e)
        raise HTTPException(status_code=500, detail="Database error")
