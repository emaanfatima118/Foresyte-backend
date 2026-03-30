"""
Video Streaming API - UC-07 Endpoints
Production-ready implementation for frontend integration
Handles video upload, processing, and results retrieval
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field
from uuid import UUID, uuid4
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load .env before reading any environment variables
load_dotenv()

from database.db import get_db
from database.models import VideoStream, ProcessingJob, FrameLog, Exam, Room, StudentActivity, Violation, InvigilatorActivity, Invigilator
from database.auth import get_current_user
from app.video_processing.processor import VideoProcessor

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/video-streams",
    tags=["video-streams"]
)

# Configuration — evaluated after load_dotenv() above
def _use_database() -> bool:
    return os.getenv("USE_DATABASE", "false").lower() == "true"

# Module-level constant (safe now that load_dotenv ran first)
USE_DATABASE = _use_database()
MAX_FILE_SIZE = 20 * 1024 * 1024 * 1024  # 20 GB
ALLOWED_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.webm'}

# -------------------------
# Pydantic Schemas (Frontend-friendly)
# -------------------------

class VideoStreamCreate(BaseModel):
    room_id: UUID
    exam_id: UUID
    stream_type: str = Field(..., pattern="^(live|recorded)$")
    source_url: Optional[str] = None


class VideoStreamResponse(BaseModel):
    stream_id: UUID
    room_id: UUID
    exam_id: UUID
    stream_type: str
    source_url: Optional[str]
    status: str
    created_at: str  # ISO format string for frontend
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    
    class Config:
        from_attributes = True


class ProcessingJobResponse(BaseModel):
    job_id: str
    stream_id: str
    status: str
    progress: float
    total_frames: int
    processed_frames: int
    detected_activities: int
    detected_violations: int
    created_at: str
    error_message: Optional[str] = None


class ProcessingResultsResponse(BaseModel):
    stream_id: str
    status: str
    exam_id: str
    room_id: str
    processing_summary: dict
    activities_summary: dict
    violations_summary: dict
    activities: List[dict]
    violations: List[dict]
    frame_analysis: List[dict]


class ErrorResponse(BaseModel):
    error: str
    detail: str
    status_code: int


class SuccessResponse(BaseModel):
    success: bool
    message: str
    data: Optional[dict] = None


# -------------------------
# Helper Functions
# -------------------------

def validate_video_file(filename: str, file_size: int) -> tuple[bool, str]:
    """Validate uploaded video file"""
    ext = os.path.splitext(filename)[1].lower()
    
    if ext not in ALLOWED_EXTENSIONS:
        return False, f"Invalid file format. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
    
    if file_size > MAX_FILE_SIZE:
        file_size_gb = file_size / (1024 * 1024 * 1024)
        max_size_gb = MAX_FILE_SIZE / (1024 * 1024 * 1024)
        return False, f"File too large ({file_size_gb:.2f} GB). Maximum size: {max_size_gb:.0f} GB"
    
    return True, "Valid"


def serialize_datetime(dt):
    """Convert datetime to ISO string for JSON with timezone"""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        # Ensure UTC timezone is included in ISO format
        if dt.tzinfo is None:
            # If no timezone, assume UTC (from datetime.utcnow())
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def convert_path_to_url(file_path: str, base_url: str = "http://localhost:8000") -> str:
    """
    Convert absolute file path to frontend-accessible URL
    
    Example:
        C:/Users/.../src/uploads/videos/exam.mp4 
        -> http://localhost:8000/uploads/videos/exam.mp4
    """
    if not file_path:
        return None
    
    # Normalize path separators
    file_path = file_path.replace('\\', '/')
    
    # Find 'uploads' in the path
    if 'uploads' in file_path:
        # Extract everything from 'uploads' onwards
        uploads_index = file_path.find('uploads')
        relative_path = file_path[uploads_index:]
        return f"{base_url}/{relative_path}"
    
    return file_path


def get_db_safe():
    """Get database session with error handling"""
    if not USE_DATABASE:
        return None
    try:
        db = next(get_db())
        return db
    except Exception as e:
        logger.warning(f"Database not available: {e}")
        return None


# -------------------------
# API Endpoints
# -------------------------

@router.post("/upload")
async def upload_exam_footage(
    background_tasks: BackgroundTasks,
    video_file: UploadFile = File(...),
    exam_id: str = Form(...),
    room_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Upload exam recording for processing (Frontend Integration Ready)
    
    Frontend Usage:
    ```javascript
    const formData = new FormData();
    formData.append('video_file', videoFile);
    formData.append('exam_id', selectedExamId);
    formData.append('room_id', selectedRoomId);
    
    const response = await fetch('/api/video-streams/upload', {
        method: 'POST',
        body: formData
    });
    ```
    """
    try:
        # Validate UUIDs first (cheap check before doing any I/O)
        try:
            exam_uuid = UUID(exam_id)
            room_uuid = UUID(room_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid exam_id or room_id format")

        # Validate file extension before streaming
        ext = os.path.splitext(video_file.filename or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file format. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
            )

        # Stream the file to disk in 8 MB chunks — avoids loading the whole video into RAM
        CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
        stream_id = uuid4()

        upload_dir = Path("uploads/videos").resolve() / str(exam_uuid) / str(room_uuid)
        upload_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        new_filename = f"exam_footage_{timestamp}{ext}"
        file_path = upload_dir / new_filename

        file_size = 0
        with open(file_path, "wb") as out:
            while True:
                chunk = await video_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                file_size += len(chunk)

        # Size guard (applied after streaming so we don't need to buffer everything)
        if file_size > MAX_FILE_SIZE:
            file_path.unlink(missing_ok=True)
            file_size_gb = file_size / (1024 ** 3)
            raise HTTPException(
                status_code=400,
                detail=f"File too large ({file_size_gb:.2f} GB). Maximum: {MAX_FILE_SIZE // (1024**3)} GB"
            )

        video_path = str(file_path.resolve())
        logger.info(f"Video saved ({file_size / (1024**2):.1f} MB): {video_path}")

        # Create database record
        if USE_DATABASE and db:
            logger.info("Attempting to save video stream to database...")
            try:
                # Validate exam and room exist
                exam = db.query(Exam).filter(Exam.exam_id == exam_uuid).first()
                room = db.query(Room).filter(Room.room_id == room_uuid).first()
                
                if not exam:
                    raise HTTPException(status_code=404, detail=f"Exam not found")
                if not room:
                    raise HTTPException(status_code=404, detail=f"Room not found")
                
                # Create video stream record
                video_stream = VideoStream(
                    stream_id=stream_id,
                    room_id=room_uuid,
                    exam_id=exam_uuid,
                    stream_type="recorded",
                    source_url=video_path,
                    status="pending",
                    created_at=datetime.utcnow()
                )
                db.add(video_stream)
                db.commit()
                db.refresh(video_stream)
                
                logger.info(f"Database record created for stream: {stream_id}")
            except SQLAlchemyError as e:
                logger.error(f"Database error saving stream record: {e}")
                db.rollback()
        else:
            logger.warning("USE_DATABASE=false — stream record will not persist after restart")
        
        # Start processing in background
        background_tasks.add_task(
            process_video_background,
            str(stream_id),
            video_path,
            str(exam_uuid),
            str(room_uuid),
            "recorded",
            USE_DATABASE
        )
        
        # Convert absolute path to frontend-accessible URL
        video_url = convert_path_to_url(video_path)
        
        # Return response (frontend-friendly JSON)
        return {
            "success": True,
            "message": "Video uploaded successfully",
            "data": {
                "stream_id": str(stream_id),
                "room_id": str(room_uuid),
                "exam_id": str(exam_uuid),
                "stream_type": "recorded",
                "source_url": video_url,  # Frontend-accessible URL
                "source_path": video_path,  # Backend internal path
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
                "file_size": file_size,
                "filename": video_file.filename
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.get("/{stream_id}/status")
def get_processing_status(stream_id: str, db: Session = Depends(get_db)):
    """
    Get processing status for a video stream (Frontend Polling)
    
    Frontend Usage:
    ```javascript
    // Poll every 2 seconds
    const interval = setInterval(async () => {
        const response = await fetch(`/api/video-streams/${streamId}/status`);
        const status = await response.json();
        
        if (status.status === 'completed') {
            clearInterval(interval);
            // Navigate to results page
        }
    }, 2000);
    ```
    """
    try:
        stream_uuid = UUID(stream_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stream_id format")
    
    # Try database first
    if USE_DATABASE and db:
        try:
            job = db.query(ProcessingJob).filter(
                ProcessingJob.stream_id == stream_uuid
            ).order_by(ProcessingJob.created_at.desc()).first()
            
            if job:
                return {
                    "success": True,
                    "data": {
                        "job_id": str(job.job_id),
                        "stream_id": str(job.stream_id),
                        "status": job.status,
                        "progress": float(job.progress or 0.0),
                        "total_frames": job.total_frames or 0,
                        "processed_frames": job.processed_frames or 0,
                        "detected_activities": job.detected_activities or 0,
                        "detected_violations": job.detected_violations or 0,
                        "created_at": serialize_datetime(job.created_at),
                        "error_message": job.error_message
                    }
                }
        except SQLAlchemyError as e:
            logger.warning(f"Database query failed: {e}")
    
    # Fallback to processor cache
    processor = VideoProcessor(None, enable_ai=False)
    results = processor.get_processing_results(stream_id)
    
    if not results:
        # Still processing or not found
        return {
            "success": True,
            "data": {
                "job_id": stream_id,
                "stream_id": stream_id,
                "status": "processing",
                "progress": 0.0,
                "total_frames": 0,
                "processed_frames": 0,
                "detected_activities": 0,
                "detected_violations": 0,
                "created_at": datetime.utcnow().isoformat(),
                "error_message": None
            }
        }
    
    # Return from cache
    return {
        "success": True,
        "data": {
            "job_id": stream_id,
            "stream_id": stream_id,
            "status": "completed" if results.get('success') else "failed",
            "progress": 100.0 if results.get('success') else 0.0,
            "total_frames": results.get('total_frames_processed', 0),
            "processed_frames": results.get('total_frames_processed', 0),
            "detected_activities": len(results.get('activities_logged', [])),
            "detected_violations": len(results.get('violations_detected', [])),
            "created_at": results.get('started_at', datetime.utcnow().isoformat()),
            "error_message": results.get('error')
        }
    }


@router.get("/{stream_id}/results")
async def get_processing_results(stream_id: str, db: Session = Depends(get_db)):
    """
    Get complete processing results (Frontend Results Page).
    When the in-memory processor cache is empty (e.g. after a server restart) the
    endpoint reconstructs results from the database so the page is never broken.
    """
    try:
        stream_uuid = UUID(stream_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stream_id format")

    # --- DB-backed results (primary path when USE_DATABASE=true) ---
    if USE_DATABASE and db:
        try:
            stream = db.query(VideoStream).filter(VideoStream.stream_id == stream_uuid).first()
            if not stream:
                raise HTTPException(status_code=404, detail="Video stream not found")

            if stream.status not in ("completed", "failed"):
                raise HTTPException(
                    status_code=202,
                    detail=f"Video is still {stream.status}. Poll /status and retry when completed."
                )

            # Load student activities and violations for this stream's exam
            activities_raw = (
                db.query(StudentActivity)
                .filter(StudentActivity.exam_id == stream.exam_id)
                .order_by(StudentActivity.timestamp)
                .all()
            )
            violations_raw = (
                db.query(Violation)
                .filter(
                    Violation.activity_id.in_([a.activity_id for a in activities_raw])
                )
                .all()
            )

            activities = [
                {
                    "timestamp": serialize_datetime(a.timestamp),
                    "behavior_type": a.activity_type,
                    "severity": a.severity,
                    "confidence": float(a.confidence) if a.confidence is not None else None,
                    "evidence_url": a.evidence_url,
                    "actor_type": "student",
                    "student_id": str(a.student_id) if a.student_id else None,
                }
                for a in activities_raw
            ]

            violations = [
                {
                    "violation_type": v.violation_type,
                    "severity_level": v.severity,
                    "status": v.status,
                    "timestamp": serialize_datetime(v.timestamp),
                    "evidence_url": v.evidence_url,
                }
                for v in violations_raw
            ]

            # Load invigilator activities for this stream's room within the processing window
            invig_q = db.query(InvigilatorActivity).filter(
                InvigilatorActivity.room_id == stream.room_id
            )
            if stream.started_at:
                invig_q = invig_q.filter(InvigilatorActivity.timestamp >= stream.started_at)
            if stream.completed_at:
                invig_q = invig_q.filter(InvigilatorActivity.timestamp <= stream.completed_at)
            invig_activities_raw = invig_q.order_by(InvigilatorActivity.timestamp).all()

            # Batch-load invigilator names
            invig_ids = {r.invigilator_id for r in invig_activities_raw if r.invigilator_id}
            invigs_map = {
                i.invigilator_id: i
                for i in db.query(Invigilator).filter(
                    Invigilator.invigilator_id.in_(invig_ids)
                ).all()
            } if invig_ids else {}

            invigilator_activities = [
                {
                    "activity_id": str(ia.activity_id),
                    "timestamp": serialize_datetime(ia.timestamp),
                    "behavior_type": ia.activity_type,
                    "severity": ia.severity,
                    "confidence": float(ia.confidence) if ia.confidence is not None else None,
                    "evidence_url": ia.evidence_url,
                    "frame_number": ia.frame_number,
                    "notes": ia.notes,
                    "actor_type": "invigilator",
                    "invigilator_id": str(ia.invigilator_id) if ia.invigilator_id else None,
                    "invigilator_name": invigs_map[ia.invigilator_id].name
                        if ia.invigilator_id and ia.invigilator_id in invigs_map else None,
                }
                for ia in invig_activities_raw
            ]

            job = (
                db.query(ProcessingJob)
                .filter(ProcessingJob.stream_id == stream_uuid)
                .order_by(ProcessingJob.created_at.desc())
                .first()
            )

            return {
                "success": True,
                "data": {
                    "stream_id": stream_id,
                    "status": stream.status,
                    "exam_id": str(stream.exam_id),
                    "room_id": str(stream.room_id),
                    "processing_summary": {
                        "started_at": serialize_datetime(stream.started_at),
                        "completed_at": serialize_datetime(stream.completed_at),
                        "total_frames": job.total_frames if job else 0,
                        "stream_type": stream.stream_type,
                    },
                    "activities_summary": {
                        "total_activities": len(activities) + len(invigilator_activities),
                        "student_activities": len(activities),
                        "invigilator_issues": len(invigilator_activities),
                    },
                    "violations_summary": {
                        "total_violations": len(violations),
                        "high_severity": len([v for v in violations if (v.get("severity_level") or 0) >= 3]),
                        "pending_review": len([v for v in violations if v.get("status") == "pending"]),
                    },
                    "activities": activities,
                    "violations": violations,
                    "invigilator_activities": invigilator_activities,
                    "frame_analysis": [],
                },
            }
        except HTTPException:
            raise
        except SQLAlchemyError as e:
            logger.error(f"DB error fetching results: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch results from database")

    # --- In-memory fallback (only useful if USE_DATABASE=false) ---
    processor = VideoProcessor(None, enable_ai=False)
    results = processor.get_processing_results(stream_id)

    if not results:
        raise HTTPException(
            status_code=404,
            detail="Processing results not available. Video may still be processing or failed."
        )

    return {
        "success": True,
        "data": {
            "stream_id": stream_id,
            "status": "completed" if results.get("success") else "failed",
            "exam_id": str(results.get("exam_id", "")),
            "room_id": str(results.get("room_id", "")),
            "processing_summary": {
                "started_at": results.get("started_at", ""),
                "completed_at": results.get("completed_at", ""),
                "total_frames": results.get("total_frames_processed", 0),
                "stream_type": results.get("stream_type", "recorded"),
            },
            "activities_summary": {
                "total_activities": len(results.get("activities_logged", [])),
                "student_activities": len(
                    [a for a in results.get("activities_logged", []) if a.get("actor_type") == "student"]
                ),
                "invigilator_issues": len(
                    [a for a in results.get("activities_logged", []) if a.get("actor_type") == "invigilator"]
                ),
            },
            "violations_summary": {
                "total_violations": len(results.get("violations_detected", [])),
                "high_severity": len(
                    [v for v in results.get("violations_detected", []) if v.get("severity_level", 0) >= 3]
                ),
                "pending_review": len(
                    [v for v in results.get("violations_detected", []) if v.get("status") == "pending"]
                ),
            },
            "activities": [
                a for a in results.get("activities_logged", []) if a.get("actor_type") == "student"
            ],
            "invigilator_activities": [
                a for a in results.get("activities_logged", []) if a.get("actor_type") == "invigilator"
            ],
            "violations": results.get("violations_detected", []),
            "frame_analysis": [
                {
                    **frame,
                    "frame_url": convert_path_to_url(frame.get("frame_path", "")) if frame.get("frame_path") else None,
                }
                for frame in results.get("frame_analysis", [])
            ],
        },
    }


@router.get("/exam/{exam_id}/streams")
def get_exam_streams(exam_id: str, db: Session = Depends(get_db)):
    """
    Get all video streams for an exam (Frontend Dashboard)
    
    Frontend Usage:
    ```javascript
    const response = await fetch(`/api/video-streams/exam/${examId}/streams`);
    const streams = await response.json();
    // Display list of uploaded videos for this exam
    ```
    """
    try:
        exam_uuid = UUID(exam_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid exam_id format")
    
    if USE_DATABASE and db:
        try:
            streams = db.query(VideoStream).filter(
                VideoStream.exam_id == exam_uuid
            ).order_by(VideoStream.created_at.desc()).all()
            
            return {
                "success": True,
                "data": {
                    "exam_id": exam_id,
                    "count": len(streams),
                    "streams": [
                        {
                            "stream_id": str(s.stream_id),
                            "room_id": str(s.room_id),
                            "stream_type": s.stream_type,
                            "status": s.status,
                            "created_at": serialize_datetime(s.created_at),
                            "completed_at": serialize_datetime(s.completed_at)
                        }
                        for s in streams
                    ]
                }
            }
        except SQLAlchemyError as e:
            logger.error(f"Database error: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch streams")
    
    return {
        "success": True,
        "data": {
            "exam_id": exam_id,
            "count": 0,
            "streams": [],
            "message": "Database not available"
        }
    }


@router.get("/room/{room_id}/streams")
def get_room_streams(room_id: str, db: Session = Depends(get_db)):
    """
    Get all video streams for a room
    """
    try:
        room_uuid = UUID(room_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid room_id format")
    
    if USE_DATABASE and db:
        try:
            streams = db.query(VideoStream).filter(
                VideoStream.room_id == room_uuid
            ).order_by(VideoStream.created_at.desc()).all()
            
            return {
                "success": True,
                "data": {
                    "room_id": room_id,
                    "count": len(streams),
                    "streams": [
                        {
                            "stream_id": str(s.stream_id),
                            "exam_id": str(s.exam_id),
                            "stream_type": s.stream_type,
                            "status": s.status,
                            "created_at": serialize_datetime(s.created_at),
                            "completed_at": serialize_datetime(s.completed_at)
                        }
                        for s in streams
                    ]
                }
            }
        except SQLAlchemyError as e:
            logger.error(f"Database error: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch streams")
    
    return {
        "success": True,
        "data": {
            "room_id": room_id,
            "count": 0,
            "streams": [],
            "message": "Database not available"
        }
    }


@router.get("/all")
def get_all_streams(
    limit: Optional[int] = Query(1000, description="Maximum number of streams to return"),
    db: Session = Depends(get_db)
):
    """
    Get all video streams (Frontend Admin Dashboard)
    
    Frontend Usage:
    ```javascript
    const response = await fetch('/api/video-streams/all?limit=1000');
    const data = await response.json();
    // Display all uploaded videos with filters
    ```
    
    Note: Default limit is 1000 to show all videos. Use pagination for very large datasets.
    """
    if USE_DATABASE and db:
        try:
            # Increase limit to 1000 by default (was 100, causing videos to disappear)
            # This ensures all videos are visible unless there are more than 1000
            max_limit = min(limit or 1000, 10000)  # Cap at 10000 for safety
            streams = db.query(VideoStream).order_by(
                VideoStream.created_at.desc()
            ).limit(max_limit).all()
            
            total_count = db.query(VideoStream).count()
            
            return {
                "success": True,
                "data": {
                    "count": len(streams),
                    "total_count": total_count,
                    "limit": max_limit,
                    "streams": [
                        {
                            "stream_id": str(s.stream_id),
                            "exam_id": str(s.exam_id),
                            "room_id": str(s.room_id),
                            "stream_type": s.stream_type,
                            "status": s.status,
                            "created_at": serialize_datetime(s.created_at),
                            "completed_at": serialize_datetime(s.completed_at),
                            "source_url": convert_path_to_url(s.source_url) if s.source_url else None
                        }
                        for s in streams
                    ]
                }
            }
        except SQLAlchemyError as e:
            logger.error(f"Database error: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch streams")
    
    # If database not available, try to get from processor cache
    # This is a fallback for when database is disabled
    try:
        processor = VideoProcessor(None, enable_ai=False)
        # Get all processing results (these contain stream info)
        # Note: This only works for recently processed videos
        # For proper persistence, USE_DATABASE should be true
        logger.warning("Database not available. Videos will not persist after server restart.")
        return {
            "success": True,
            "data": {
                "count": 0,
                "streams": [],
                "message": "Database not available. Enable USE_DATABASE=true for persistent storage.",
                "warning": "Videos uploaded without database will be lost on server restart."
            }
        }
    except Exception as e:
        logger.error(f"Error in fallback: {e}")
        return {
            "success": True,
            "data": {
                "count": 0,
                "streams": [],
                "message": "Database not available"
            }
        }


@router.delete("/{stream_id}")
def delete_video_stream(stream_id: str, db: Session = Depends(get_db)):
    """
    Delete a video stream and associated files
    """
    try:
        stream_uuid = UUID(stream_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stream_id format")
    
    if USE_DATABASE and db:
        try:
            stream = db.query(VideoStream).filter(
                VideoStream.stream_id == stream_uuid
            ).first()
            
            if not stream:
                raise HTTPException(status_code=404, detail="Video stream not found")
            
            # Delete associated records
            db.query(FrameLog).filter(
                FrameLog.job_id.in_(
                    db.query(ProcessingJob.job_id).filter(
                        ProcessingJob.stream_id == stream_uuid
                    )
                )
            ).delete(synchronize_session=False)
            
            db.query(ProcessingJob).filter(
                ProcessingJob.stream_id == stream_uuid
            ).delete()
            
            # Delete video file
            if stream.source_url and os.path.exists(stream.source_url):
                try:
                    os.remove(stream.source_url)
                    logger.info(f"Deleted video file: {stream.source_url}")
                except Exception as e:
                    logger.error(f"Failed to delete video file: {e}")
            
            # Delete stream record
            db.delete(stream)
            db.commit()
            
            return {
                "success": True,
                "message": "Video stream deleted successfully"
            }
        except SQLAlchemyError as e:
            logger.error(f"Database error: {e}")
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to delete stream")
    
    return {
        "success": False,
        "message": "Database not available. Cannot delete."
    }


# -------------------------
# Background Processing
# -------------------------

async def process_video_background(
    stream_id: str, 
    source: str, 
    exam_id: str,
    room_id: str, 
    stream_type: str,
    use_database: bool = False
):
    """
    Background task for video processing
    Handles both database and non-database modes
    """
    from database.db import SessionLocal
    
    db = None
    if use_database:
        try:
            db = SessionLocal()
        except Exception as e:
            logger.warning(f"Database not available for background task: {e}")
            db = None
    
    try:
        logger.info(f"[Background Task] Starting video processing for stream {stream_id}")
        logger.info(f"[Background Task] Source path: {source}")
        logger.info(f"[Background Task] Source exists: {os.path.exists(source)}")
        logger.info(f"[Background Task] Source absolute: {os.path.abspath(source)}")
        logger.info(f"[Background Task] Current working directory: {os.getcwd()}")
        logger.info(f"[Background Task] Stream type: {stream_type}")
        
        # Update status to processing (if database available)
        if db and use_database:
            try:
                stream = db.query(VideoStream).filter(
                    VideoStream.stream_id == UUID(stream_id)
                ).first()
                if stream:
                    stream.status = "processing"
                    stream.started_at = datetime.utcnow()
                    db.commit()
                
                # Create processing job
                job = ProcessingJob(
                    stream_id=UUID(stream_id),
                    status="processing",
                    started_at=datetime.utcnow()
                )
                db.add(job)
                db.commit()
            except Exception as e:
                logger.error(f"Failed to update database: {e}")
                db.rollback()
        
        # Process video with AI cheating detection enabled
        processor = VideoProcessor(db, enable_ai=True)
        seat_mapping = {}  # TODO: Load from seating plan
        
        # Create progress callback to update ProcessingJob during extraction
        def update_progress_callback(processed: int, total: int):
            """Update ProcessingJob with frame extraction progress"""
            if db and use_database:
                try:
                    job = db.query(ProcessingJob).filter(
                        ProcessingJob.stream_id == UUID(stream_id)
                    ).first()
                    if job:
                        job.total_frames = total
                        job.processed_frames = processed
                        job.progress = (processed / total * 100) if total > 0 else 0.0
                        db.commit()
                        logger.info(f"Updated progress: {processed}/{total} frames ({job.progress:.1f}%)")
                except Exception as e:
                    logger.warning(f"Failed to update progress: {e}")
                    db.rollback()
        
        # Pass progress callback to processor
        processor.set_progress_callback(update_progress_callback)
        
        results = await processor.process_video_stream(
            stream_id, source, stream_type, exam_id, room_id, seat_mapping
        )
        
        # Update status to completed (if database available)
        if db and use_database:
            try:
                if results.get('success'):
                    # Update job
                    job = db.query(ProcessingJob).filter(
                        ProcessingJob.stream_id == UUID(stream_id)
                    ).first()
                    if job:
                        job.status = "completed"
                        job.progress = 100.0
                        # Use the actual extracted frames count
                        extracted_frames = results.get('total_frames_processed', 0)
                        # Get expected extracted frames from extraction result
                        extraction_result = results.get('extraction_result', {})
                        expected_extracted = extraction_result.get('extracted_frames', extracted_frames)
                        total_video_frames = extraction_result.get('total_frames', extracted_frames)
                        
                        # Store expected extracted frames as total_frames (for display)
                        if job.total_frames is None or job.total_frames == 0:
                            job.total_frames = expected_extracted if expected_extracted > 0 else extracted_frames
                        job.processed_frames = extracted_frames
                        job.detected_activities = len(results.get('activities_logged', []))
                        job.detected_violations = len(results.get('violations_detected', []))
                        job.completed_at = datetime.utcnow()
                    
                    # Update stream
                    stream = db.query(VideoStream).filter(
                        VideoStream.stream_id == UUID(stream_id)
                    ).first()
                    if stream:
                        stream.status = "completed"
                        stream.completed_at = datetime.utcnow()
                    
                    db.commit()
                    logger.info(f"Processing completed successfully for stream {stream_id}")
                else:
                    # Update as failed
                    job = db.query(ProcessingJob).filter(
                        ProcessingJob.stream_id == UUID(stream_id)
                    ).first()
                    if job:
                        job.status = "failed"
                        job.error_message = results.get('error', 'Unknown error')
                    
                    stream = db.query(VideoStream).filter(
                        VideoStream.stream_id == UUID(stream_id)
                    ).first()
                    if stream:
                        stream.status = "failed"
                    
                    db.commit()
                    logger.error(f"Processing failed for stream {stream_id}: {results.get('error')}")
            except Exception as e:
                logger.error(f"Failed to update completion status: {e}")
                db.rollback()
        else:
            logger.info(f"Processing completed (no database). Frames: {results.get('total_frames_processed', 0)}")
        
    except Exception as e:
        logger.error(f"Error processing video: {e}")
        import traceback
        traceback.print_exc()
        
        # Update as failed if database available
        if db and use_database:
            try:
                job = db.query(ProcessingJob).filter(
                    ProcessingJob.stream_id == UUID(stream_id)
                ).first()
                if job:
                    job.status = "failed"
                    job.error_message = str(e)
                
                stream = db.query(VideoStream).filter(
                    VideoStream.stream_id == UUID(stream_id)
                ).first()
                if stream:
                    stream.status = "failed"
                
                db.commit()
            except:
                pass
    
    finally:
        if db:
            db.close()
