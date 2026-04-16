"""
Video Processing Orchestrator - Complete UC-07 Implementation
Coordinates video processing, AI detection, and database logging
"""

from datetime import datetime
from typing import Dict, Any, Optional, List
from collections import defaultdict
import json
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from functools import partial

import cv2

from .stream_handler import (
    FRAME_SUBDIR_PIPELINE,
    FRAME_SUBDIR_SIMPLE,
    VideoStreamHandler,
)
from database.cheating_labels import (
    merge_behavior_labels,
    merged_base_severity,
    merge_frame_behavior_details,
)
from database.severity_logic import (
    get_runs_from_detections,
    filter_qualifying_runs,
    severity_to_int,
)
from database.violation_severity_matrix import resolve_activity_severity

logging.basicConfig(level=logging.INFO)


def _evidence_path_to_url(file_path: Optional[str]) -> Optional[str]:
    """Convert filesystem path to frontend-accessible URL (/uploads/...)."""
    if not file_path:
        return None
    path = str(file_path).replace("\\", "/")
    if "uploads" in path:
        idx = path.find("uploads")
        return "/" + path[idx:]
    return path


def _pipeline_annotated_path(raw_frame_path: str) -> str:
    """
    Raw frame: .../frames/<job>/simple/name.jpg
    Annotated: .../frames/<job>/pipeline/name_detection.jpg
    Falls back to next to raw file if layout is not session/simple.
    """
    p = Path(raw_frame_path)
    if p.parent.name == FRAME_SUBDIR_SIMPLE:
        out_dir = p.parent.parent / FRAME_SUBDIR_PIPELINE
        out_dir.mkdir(parents=True, exist_ok=True)
        return str(out_dir / f"{p.stem}_detection{p.suffix}")
    return f"{p.with_suffix('')}_detection.jpg"


def _report_bbox_frame_path(raw_frame_path: str, actor_type: str, suffix: str) -> str:
    """
    Save report appendix frames separately from the general evidence frame.
    Layout:
      .../uploads/report_evidence/<actor_type>/<stem>_<suffix>.jpg
    """
    p = Path(raw_frame_path)
    uploads_root = None
    parts = list(p.parts)
    if "uploads" in parts:
        idx = parts.index("uploads")
        uploads_root = Path(*parts[: idx + 1])
    if uploads_root is None:
        uploads_root = Path("uploads")
    out_dir = uploads_root / "report_evidence" / actor_type
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in suffix)
    return str(out_dir / f"{p.stem}_{safe_suffix}.jpg")


def _save_report_bbox_frame(
    frame,
    raw_frame_path: str,
    actor_type: str,
    suffix: str,
    bboxes: list,
    color: tuple[int, int, int],
) -> tuple[Optional[str], Optional[str]]:
    """
    Save a dedicated report image with only the relevant bbox(es) drawn.
    Returns (local_path, public_or_remote_url).
    """
    if frame is None or not bboxes:
        return (None, None)
    draw = frame.copy()
    h, w = draw.shape[:2]
    for bbox in bboxes:
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = [int(round(float(v))) for v in bbox[:4]]
        x1 = max(0, min(w - 1, x1))
        x2 = max(0, min(w - 1, x2))
        y1 = max(0, min(h - 1, y1))
        y2 = max(0, min(h - 1, y2))
        if x2 <= x1 or y2 <= y1:
            continue
        cv2.rectangle(draw, (x1, y1), (x2, y2), color, 3)

    out_path = _report_bbox_frame_path(raw_frame_path, actor_type, suffix)
    cv2.imwrite(out_path, draw)
    report_url = _evidence_path_to_url(out_path)
    try:
        from app.storage.blob_storage import upload_evidence_frame

        blob_url = upload_evidence_frame(out_path)
        if blob_url:
            report_url = blob_url
    except Exception as exc:
        logger.debug("Report bbox frame upload skipped or failed: %s", exc)
    return (out_path, report_url)


def _save_invigilator_full_frame_evidence(
    frame,
    raw_frame_path: str,
    frame_label: str,
) -> tuple[Optional[str], Optional[str]]:
    """Full frame when no bbox; saved under report_evidence and uploaded to blob (R2)."""
    if frame is None or frame.size == 0:
        return (None, None)
    p = Path(raw_frame_path)
    uploads_root = None
    parts = list(p.parts)
    if "uploads" in parts:
        idx = parts.index("uploads")
        uploads_root = Path(*parts[: idx + 1])
    if uploads_root is None:
        uploads_root = Path("uploads")
    safe = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in frame_label)[:120]
    out_dir = uploads_root / "report_evidence" / "invigilator"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / f"{p.stem}_full_{safe}.jpg")
    cv2.imwrite(out_path, frame)
    report_url = _evidence_path_to_url(out_path)
    try:
        from app.storage.blob_storage import upload_evidence_frame

        blob_url = upload_evidence_frame(out_path)
        if blob_url:
            report_url = blob_url
    except Exception as exc:
        logger.debug("Full invigilator frame blob upload skipped: %s", exc)
    return (out_path, report_url)


def _save_student_identification_frame(
    frame,
    raw_frame_path: str,
    students: list[dict[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    """
    Save a shared report image for exam reports with student name + roll under each bbox
    (sandbox YOLO person→seat boxes when rows include full_name).
    """
    if frame is None or not students:
        return (None, None)
    draw = frame.copy()
    h, w = draw.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    for item in students:
        bbox = item.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = [int(round(float(v))) for v in bbox[:4]]
        x1 = max(0, min(w - 1, x1))
        x2 = max(0, min(w - 1, x2))
        y1 = max(0, min(h - 1, y1))
        y2 = max(0, min(h - 1, y2))
        if x2 <= x1 or y2 <= y1:
            continue
        cv2.rectangle(draw, (x1, y1), (x2, y2), (0, 180, 0), 2)
        full_name = str(item.get("full_name") or "").strip()
        roll = str(item.get("roll_number") or "").strip() or "UNIDENTIFIED-AI"
        line_name = (full_name[:40] + "...") if len(full_name) > 40 else full_name
        line_roll = f"Roll: {roll}" if line_name else roll
        fs1, fs2 = 0.48, 0.44
        if line_name:
            (tw1, th1), _bl1 = cv2.getTextSize(line_name, font, fs1, 1)
        else:
            tw1, th1 = 0, 0
        (tw2, th2), _bl2 = cv2.getTextSize(line_roll, font, fs2, 1)
        tw = int(max(tw1, tw2, 100) + 12)
        th = 52 if line_name else 28
        text_y0 = max(0, min(h - th - 2, y2 + 4))
        x1b = max(0, min(x1, w - tw - 2))
        x2b = min(w - 1, x1b + tw)
        y2b = min(h - 1, text_y0 + th)
        cv2.rectangle(draw, (x1b, text_y0), (x2b, y2b), (0, 140, 0), -1)
        base_y = text_y0 + 18
        if line_name:
            cv2.putText(draw, line_name, (x1b + 4, base_y), font, fs1, (255, 255, 255), 1, cv2.LINE_AA)
            base_y += 22
        cv2.putText(draw, line_roll, (x1b + 4, base_y), font, fs2, (255, 255, 255), 1, cv2.LINE_AA)

    out_path = _report_bbox_frame_path(raw_frame_path, "student_identification", "rolls")
    cv2.imwrite(out_path, draw)
    report_url = _evidence_path_to_url(out_path)
    try:
        from app.storage.blob_storage import upload_evidence_frame

        blob_url = upload_evidence_frame(out_path)
        if blob_url:
            report_url = blob_url
    except Exception as exc:
        logger.debug("Student identification frame upload skipped or failed: %s", exc)
    return (out_path, report_url)


def _anonymous_student_bucket(behavior: Dict[str, Any], seat_id: Any = None) -> str:
    """
    Build a stable-ish bucket for detections that could not be resolved to a student.
    This avoids collapsing all unmapped students into one global "unidentified" run.
    """
    if seat_id:
        return f"seat:{seat_id}"
    bbox = behavior.get("bbox")
    if bbox and len(bbox) >= 4:
        x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
        cx = int(round(((x1 + x2) / 2.0) / 80.0))
        cy = int(round(((y1 + y2) / 2.0) / 80.0))
        w = int(round(max(1.0, x2 - x1) / 80.0))
        h = int(round(max(1.0, y2 - y1) / 80.0))
        return f"bbox:{cx}:{cy}:{w}:{h}"
    return "unidentified"


def _bbox_iou(box_a: Any, box_b: Any) -> float:
    if not box_a or not box_b or len(box_a) < 4 or len(box_b) < 4:
        return 0.0
    ax1, ay1, ax2, ay2 = [float(v) for v in box_a[:4]]
    bx1, by1, bx2, by2 = [float(v) for v in box_b[:4]]
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter = inter_w * inter_h
    if inter <= 0:
        return 0.0
    area_a = max(1.0, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1.0, (bx2 - bx1) * (by2 - by1))
    return inter / max(1.0, area_a + area_b - inter)


def _merge_unmapped_frame_groups(frame_groups: Dict[tuple, List[Dict[str, Any]]]) -> Dict[tuple, List[Dict[str, Any]]]:
    """
    Merge same-frame anonymous groups when their boxes strongly overlap.
    This helps combine multiple labels for one unmapped student into one merged activity.
    """
    merged: list[tuple[tuple, List[Dict[str, Any]]]] = []
    for key, blist in frame_groups.items():
        exemplar = next((b for b in blist if b.get("bbox")), blist[0] if blist else None)
        if not exemplar:
            merged.append((key, blist))
            continue
        if exemplar.get("student_id"):
            merged.append((key, blist))
            continue
        matched_idx = None
        for idx, (_mkey, existing) in enumerate(merged):
            existing_exemplar = next((b for b in existing if b.get("bbox")), existing[0] if existing else None)
            if not existing_exemplar or existing_exemplar.get("student_id"):
                continue
            if _bbox_iou(exemplar.get("bbox"), existing_exemplar.get("bbox")) >= 0.35:
                matched_idx = idx
                break
        if matched_idx is None:
            merged.append((key, list(blist)))
        else:
            merged[matched_idx][1].extend(blist)
    return {key: blist for key, blist in merged}


logger = logging.getLogger(__name__)


class VideoProcessor:
    """
    Main orchestrator for UC-07: Process Exam Footage (Live/Recorded)
    Coordinates all steps from video input to final report generation
    """
    
    def __init__(self, db_session=None, enable_ai=False):
        """
        Initialize video processor with all components.
        
        Args:
            db_session: Database session for logging activities
            enable_ai: Enable AI detection (default: False for testing input)
        """
        self.stream_handler = VideoStreamHandler()
        self.enable_ai = enable_ai
        if enable_ai:
            try:
                from app.ai_engine.detection_adapter import process_frame, map_detection_to_seat
                self.process_frame = process_frame
                self.map_detection_to_seat = map_detection_to_seat
                self.behavior_detector = True  # Flag that student cheating AI is available
            except ImportError as e:
                logger.warning(
                    "Student cheating AI (detection_adapter) not available — student detection disabled. %s",
                    e,
                )
                # Do not set enable_ai=False: invigilator detection is independent and must still run.
                self.process_frame = None
                self.map_detection_to_seat = None
                self.behavior_detector = None
        else:
            self.process_frame = None
            self.map_detection_to_seat = None
            self.behavior_detector = None
        self.db_session = db_session
        self.processing_results = {}
        self.progress_callback = None  # Callback to update progress during processing

        # Invigilator detection adapter (one instance per stream; initialized lazily)
        self.invig_adapter = None

        # Set during process_video_stream so DB helpers can access them
        self._current_exam_id: Optional[str] = None
        self._current_room_id: Optional[str] = None
        
    def set_progress_callback(self, callback):
        """Set callback function to update progress during frame extraction"""
        self.progress_callback = callback
        
    async def process_video_stream(self, stream_id: str, source: str, 
                                   stream_type: str, exam_id: str,
                                   room_id: str, seat_mapping: Dict = None) -> Dict[str, Any]:
        """
        Complete UC-07 Main Success Scenario (Steps 1-10)
        
        Args:
            stream_id: Video stream identifier
            source: Video source (file path or stream URL)
            stream_type: 'live' or 'recorded'
            exam_id: Exam identifier
            room_id: Room identifier  
            seat_mapping: Seat position mapping
            
        Returns:
            Complete processing results
        """
        logger.info(f"Starting video processing for stream {stream_id}")
        start_time = datetime.utcnow()

        # Store context for DB helpers
        self._current_exam_id = exam_id
        self._current_room_id = room_id

        # Invigilator pose/activity detection: on by default when unset so upload pipeline
        # persists InvigilatorActivity rows. Set ENABLE_INVIGILATOR_DETECTION=false to skip
        # (saves load time if models are not deployed).
        self.invig_adapter = None
        _inv_env = (os.getenv("ENABLE_INVIGILATOR_DETECTION") or "true").strip().lower()
        _enable_inv = _inv_env not in ("0", "false", "no", "off", "")
        if _enable_inv:
            try:
                from app.invigilator.invig_adapter import InvigFrameAdapter
                self.invig_adapter = InvigFrameAdapter()
                logger.info("Invigilator detection adapter initialized for stream %s", stream_id)
            except FileNotFoundError as e:
                logger.warning(
                    "Invigilator model not found — invigilator activities will not be detected: %s",
                    e,
                )
            except Exception as e:
                logger.warning(
                    "Invigilator detection adapter failed to load — invigilator activities disabled: %s",
                    e,
                    exc_info=True,
                )
        else:
            logger.info(
                "Invigilator detection disabled (ENABLE_INVIGILATOR_DETECTION=false); "
                "no invigilator activities will be logged"
            )

        try:
            # Step 1: Connect to video source
            # Step 2: Validate video input
            validation = self.stream_handler.validate_video_input(source, stream_type)
            
            if not validation['valid']:
                return {
                    "success": False,
                    "error": validation.get('error', 'Invalid video source'),
                    "stream_id": stream_id
                }
            
            logger.info(f"Video validated: {validation}")
            
            # Initialize results
            results = {
                "stream_id": stream_id,
                "exam_id": exam_id,
                "room_id": room_id,
                "stream_type": stream_type,
                "started_at": start_time.isoformat(),
                "validation": validation,
                "activities_logged": [],
                "violations_detected": [],
                "frame_analysis": []
            }
            
            # Step 3-6: Process video based on type
            if stream_type == 'live':
                processing_result = await self._process_live_footage(
                    stream_id, source, exam_id, room_id, seat_mapping
                )
            else:  # recorded
                processing_result = await self._process_recorded_footage(
                    stream_id, source, exam_id, room_id, seat_mapping
                )
            
            results.update(processing_result)
            
            # Step 7-10: Results accessible through reports API
            results['completed_at'] = datetime.utcnow().isoformat()
            results['success'] = True
            
            # Store results
            self.processing_results[stream_id] = results
            
            logger.info(f"Processing completed for stream {stream_id}")
            logger.info(f"Total activities: {len(results['activities_logged'])}")
            logger.info(f"Total violations: {len(results['violations_detected'])}")
            
            return results
            
        except Exception as e:
            logger.error(f"Error processing stream {stream_id}: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "stream_id": stream_id,
                "completed_at": datetime.utcnow().isoformat()
            }
    
    async def _process_live_footage(self, stream_id: str, stream_url: str,
                                   exam_id: str, room_id: str,
                                   seat_mapping: Dict = None) -> Dict[str, Any]:
        """
        Process live CCTV footage (real-time processing).
        Steps 3-6 of UC-07 for live streams.
        
        Args:
            stream_id: Stream identifier
            stream_url: CCTV stream URL
            exam_id: Exam identifier
            room_id: Room identifier
            seat_mapping: Seat position mapping
            
        Returns:
            Processing results
        """
        logger.info(f"Processing live stream: {stream_url}")

        activities = []
        violations = []
        frame_count = 0
        detections_by_student_live: Dict[str, List[Dict]] = defaultdict(list)
        student_roll_cache: Dict[str, str] = {}
        live_identification_path: Optional[str] = None
        live_identification_url: Optional[str] = None

        def _roll_for_student(student_id: Optional[str]) -> str:
            if not student_id:
                return "UNIDENTIFIED-AI"
            if student_id in student_roll_cache:
                return student_roll_cache[student_id]
            roll = "UNIDENTIFIED-AI"
            if self.db_session:
                try:
                    from uuid import UUID
                    from database.models import Student

                    student = self.db_session.query(Student).filter(
                        Student.student_id == UUID(str(student_id))
                    ).first()
                    if student and student.roll_number:
                        roll = str(student.roll_number)
                except Exception:
                    pass
            student_roll_cache[student_id] = roll
            return roll

        async def frame_callback(frame, frame_num, timestamp):
            """Process each frame: collect detections per student for run-based logic."""
            nonlocal frame_count, live_identification_path, live_identification_url
            live_frame_stub = f"uploads/live/frame_{frame_num}.jpg"

            if self.process_frame:
                analysis = self.process_frame(
                    frame, frame_num, timestamp, seat_mapping
                )
                student_behaviors = analysis.get('student_behaviors', [])
            else:
                student_behaviors = []

            # Invigilator runs whenever the adapter is loaded (independent of student AI)
            if self.invig_adapter is not None:
                try:
                    invigilator_behaviors = self.invig_adapter.process_frame(
                        frame, frame_num, timestamp
                    )
                except Exception as exc:
                    logger.warning(
                        "Invigilator detection failed on live frame %d: %s",
                        frame_num,
                        exc,
                    )
                    invigilator_behaviors = []
            else:
                invigilator_behaviors = []

            frame_student_groups: Dict[tuple, List[Dict]] = defaultdict(list)
            _unmapped_slot = [0]

            def _frame_group_key(student_id, frame_no: int, behavior: Dict) -> tuple:
                if student_id:
                    return ("id", str(student_id), frame_no)
                student_index = behavior.get("student_index")
                if student_index is not None:
                    return ("idx", int(student_index), frame_no)
                bbox = behavior.get("bbox")
                if bbox and len(bbox) >= 4:
                    bbox_key = tuple(int(round(float(x))) for x in bbox[:4])
                else:
                    _unmapped_slot[0] += 1
                    bbox_key = ("slot", _unmapped_slot[0])
                return ("bbox", bbox_key, frame_no)

            for behavior in student_behaviors:
                seat_id = (
                    self.map_detection_to_seat(behavior, seat_mapping)
                    if (self.process_frame and self.map_detection_to_seat)
                    else None
                )
                student_id = behavior.get("student_id") or (
                    seat_id if isinstance(seat_id, str) else None
                )
                gkey = _frame_group_key(student_id, frame_num, behavior)
                frame_student_groups[gkey].append(
                    {
                        **behavior,
                        "seat_id": seat_id,
                        "student_id": str(student_id) if student_id else None,
                    }
                )
            frame_student_groups = _merge_unmapped_frame_groups(frame_student_groups)
            shared_student_report_evidence_path = None
            shared_student_report_evidence_url = None
            identification_evidence_path = None
            identification_evidence_url = None
            shared_student_report_bboxes = [
                b.get("bbox")
                for blist in frame_student_groups.values()
                for b in blist
                if b.get("bbox")
            ]
            if shared_student_report_bboxes:
                shared_student_report_evidence_path = live_frame_stub
                shared_student_report_evidence_url = _evidence_path_to_url(live_frame_stub)
                if live_identification_url is None:
                    identification_students = []
                    for blist in frame_student_groups.values():
                        exemplar = next((b for b in blist if b.get("bbox")), blist[0] if blist else None)
                        if not exemplar or not exemplar.get("bbox"):
                            continue
                        identification_students.append(
                            {
                                "bbox": exemplar.get("bbox"),
                                "roll_number": _roll_for_student(exemplar.get("student_id")),
                            }
                        )
                    identification_evidence_path, identification_evidence_url = (
                        _save_student_identification_frame(
                            frame,
                            live_frame_stub,
                            identification_students,
                        )
                    )
                    live_identification_path = identification_evidence_path
                    live_identification_url = identification_evidence_url
                else:
                    identification_evidence_path = live_identification_path
                    identification_evidence_url = live_identification_url
            for _gkey, blist in frame_student_groups.items():
                labels = [b["behavior_type"] for b in blist]
                merged = merge_behavior_labels(labels)
                if not merged:
                    continue
                base_sev = merged_base_severity(labels)
                details = merge_frame_behavior_details(blist)
                max_conf = max(float(b.get("confidence") or 0) for b in blist)
                detection = {
                    "timestamp": timestamp.isoformat(),
                    "frame_number": frame_num,
                    "behavior_type": merged,
                    "severity": base_sev,
                    "confidence": max_conf,
                    "seat_id": blist[0].get("seat_id"),
                    "student_id": blist[0].get("student_id"),
                    "details": details or blist[0].get("details", ""),
                    "report_evidence_path": shared_student_report_evidence_path,
                    "report_evidence_url": shared_student_report_evidence_url,
                    "identification_evidence_path": identification_evidence_path,
                    "identification_evidence_url": identification_evidence_url,
                    "actor_type": "student",
                }
                sk = (
                    str(blist[0].get("student_id"))
                    if blist[0].get("student_id")
                    else _anonymous_student_bucket(
                        blist[0],
                        seat_id=blist[0].get("seat_id"),
                    )
                )
                detections_by_student_live[sk].append(detection)

            for behavior in invigilator_behaviors:
                report_evidence_path = None
                report_evidence_url = None
                if behavior.get("bbox"):
                    report_evidence_path, report_evidence_url = _save_report_bbox_frame(
                        frame,
                        live_frame_stub,
                        "invigilator",
                        f"frame_{frame_num}_{behavior.get('tracker_id', 'unknown')}_{behavior['behavior_type'][:32]}",
                        [behavior.get("bbox")],
                        (255, 0, 0),
                    )
                else:
                    report_evidence_path, report_evidence_url = _save_invigilator_full_frame_evidence(
                        frame,
                        live_frame_stub,
                        f"f{frame_num}_{behavior.get('tracker_id', 'u')}_{str(behavior.get('behavior_type', ''))[:40]}",
                    )
                activity = {
                    "timestamp": timestamp.isoformat(),
                    "frame_number": frame_num,
                    "behavior_type": behavior['behavior_type'],
                    "severity": behavior['severity'],
                    "confidence": behavior['confidence'],
                    "details": behavior.get('details', ''),
                    "tracker_id": behavior.get('tracker_id'),
                    "bbox": behavior.get('bbox'),
                    "evidence_path": report_evidence_path,
                    "evidence_url": report_evidence_url,
                    "report_evidence_path": report_evidence_path,
                    "report_evidence_url": report_evidence_url,
                    "actor_type": "invigilator",
                }
                activities.append(activity)
                if self.db_session:
                    await self._log_invigilator_activity_to_db(activity, room_id)

            frame_count += 1
            if frame_count % 100 == 0:
                logger.info(f"Processed {frame_count} frames")

        stream_result = await self.stream_handler.process_live_stream(
            stream_url, duration_seconds=3600, callback=frame_callback
        )

        # Run-based logic: one activity + one violation per qualifying run per student
        for student_key, det_list in detections_by_student_live.items():
            runs = get_runs_from_detections(det_list)
            qualifying = filter_qualifying_runs(runs)
            if not qualifying and det_list:
                logger.info(
                    "No qualifying student runs in live mode for key=%s; using fallback detections=%d",
                    student_key,
                    len(det_list),
                )
                for fd in det_list:
                    severity_str, severity_rule = resolve_activity_severity(
                        str(fd.get("behavior_type") or "unknown"),
                        1,
                    )
                    activity = {
                        "timestamp": fd.get("timestamp"),
                        "frame_number": fd.get("frame_number"),
                        "behavior_type": fd.get("behavior_type"),
                        "severity": severity_str,
                        "run_frame_count": 1,
                        "severity_rule": f"{severity_rule}_fallback",
                        "confidence": fd.get("confidence"),
                        "seat_id": fd.get("seat_id"),
                        "student_id": fd.get("student_id") if not str(student_key).startswith(("bbox:", "seat:", "unidentified")) else None,
                        "details": (fd.get("details", "") or "") + " [fallback: non-qualifying short run]",
                        "report_evidence_path": fd.get("report_evidence_path"),
                        "report_evidence_url": fd.get("report_evidence_url"),
                        "identification_evidence_path": fd.get("identification_evidence_path")
                        or live_identification_path,
                        "identification_evidence_url": fd.get("identification_evidence_url")
                        or live_identification_url,
                        "actor_type": "student",
                    }
                    activities.append(activity)
                    violations.append({
                        "activity": activity,
                        "violation_type": fd.get("behavior_type"),
                        "severity_level": severity_to_int(severity_str),
                        "status": "pending",
                        "timestamp": fd.get("timestamp"),
                    })
                    if self.db_session:
                        await self._log_activity_and_violation(
                            activity, exam_id, room_id,
                            create_violation=True,
                        )
            for run in qualifying:
                fd = run.first_detection
                severity_str, severity_rule = resolve_activity_severity(
                    run.label_raw, run.frame_count
                )
                activity = {
                    "timestamp": fd.get("timestamp"),
                    "frame_number": fd.get("frame_number"),
                    "behavior_type": run.label_raw,
                    "severity": severity_str,
                    "run_frame_count": run.frame_count,
                    "severity_rule": severity_rule,
                    "confidence": fd.get("confidence"),
                    "seat_id": fd.get("seat_id"),
                    "student_id": fd.get("student_id") if not str(student_key).startswith(("bbox:", "seat:", "unidentified")) else None,
                    "details": fd.get("details", "") or f"({run.frame_count} consecutive frames)",
                    "report_evidence_path": fd.get("report_evidence_path"),
                    "report_evidence_url": fd.get("report_evidence_url"),
                    "identification_evidence_path": live_identification_path
                    or fd.get("identification_evidence_path"),
                    "identification_evidence_url": live_identification_url
                    or fd.get("identification_evidence_url"),
                    "actor_type": "student",
                }
                activities.append(activity)
                violations.append({
                    "activity": activity,
                    "violation_type": run.label_raw,
                    "severity_level": severity_to_int(severity_str),
                    "status": "pending",
                    "timestamp": fd.get("timestamp"),
                })
                if self.db_session:
                    await self._log_activity_and_violation(
                        activity, exam_id, room_id,
                        create_violation=True,
                    )

        violations.sort(
            key=lambda v: (-int(v.get("severity_level", 0)), str(v.get("timestamp") or ""))
        )

        return {
            "stream_result": stream_result,
            "activities_logged": activities,
            "violations_detected": violations,
            "total_frames_processed": frame_count,
            "total_frames_in_video": frame_count
        }
    
    async def _process_recorded_footage(self, stream_id: str, video_path: str,
                                       exam_id: str, room_id: str,
                                       seat_mapping: Dict = None) -> Dict[str, Any]:
        """
        Process recorded exam footage (batch processing).
        Steps 3-6 of UC-07 for recorded videos.
        
        Args:
            stream_id: Stream identifier
            video_path: Path to recorded video file
            exam_id: Exam identifier
            room_id: Room identifier
            seat_mapping: Seat position mapping
            
        Returns:
            Processing results
        """
        logger.info(f"Processing recorded video: {video_path}")

        # Collect detections per student (frame sequence) for run-based violation logic
        detections_by_student: Dict[str, List[Dict]] = defaultdict(list)
        activities = []
        violations = []
        frame_analyses = []
        student_roll_cache: Dict[str, str] = {}

        def _roll_for_student(student_id: Optional[str]) -> str:
            if not student_id:
                return "UNIDENTIFIED-AI"
            if student_id in student_roll_cache:
                return student_roll_cache[student_id]
            roll = "UNIDENTIFIED-AI"
            if self.db_session:
                try:
                    from uuid import UUID
                    from database.models import Student

                    student = self.db_session.query(Student).filter(
                        Student.student_id == UUID(str(student_id))
                    ).first()
                    if student and student.roll_number:
                        roll = str(student.roll_number)
                except Exception:
                    pass
            student_roll_cache[student_id] = roll
            return roll
        
        # Extract and process frames
        def progress_callback(processed, total):
            progress = (processed / total * 100) if total > 0 else 0
            logger.info(f"Progress: {progress:.1f}% ({processed}/{total} frames)")
            # Call external progress callback if set (for database updates)
            if self.progress_callback:
                try:
                    self.progress_callback(processed, total)
                except Exception as e:
                    logger.warning(f"Progress callback error: {e}")
        
        # Step 3: Process video frames in batch mode.
        # process_recorded_video is a blocking sync function (OpenCV + disk I/O).
        # Run it in a thread-pool executor so it doesn't block the asyncio event loop.
        loop = asyncio.get_event_loop()
        extraction_result = await loop.run_in_executor(
            None,
            partial(
                self.stream_handler.process_recorded_video,
                video_path,
                stream_id,
                progress_callback,
                room_id=room_id,
                db_session=self.db_session,
            ),
        )
        
        if not extraction_result['success']:
            return {
                "success": False,
                "error": extraction_result.get('error', 'Failed to extract frames')
            }
        
        frames_info = extraction_result['frames_info']
        total_frames_in_video = extraction_result.get('total_frames', len(frames_info))
        logger.info(f"Extracted {len(frames_info)} frames for analysis (out of {total_frames_in_video} total frames in video)")
        
        # Build seat mapper for bbox -> student resolution (seating plan coordinates)
        seat_mapper = None
        if extraction_result.get('seat_map') and self.db_session:
            from uuid import UUID
            from app.video_processing.seat_mapper import SeatMapper
            from database.models import Room

            room = self.db_session.query(Room).filter(Room.room_id == UUID(room_id)).first()
            room_no = f"{room.block}-{room.room_number}" if room and room.block else (room.room_number if room else "")
            seat_mapper = SeatMapper(
                extraction_result['seat_map'],
                room_id,
                exam_id,
                self.db_session,
                room_no=room_no,
            )
            logger.info("Seat mapper initialized for student identification")
            if frames_info:
                def _read_first_frame_for_seat_anchors():
                    import cv2 as _cv2

                    return _cv2.imread(frames_info[0]["frame_path"])

                first_frame_img = await loop.run_in_executor(
                    None,
                    _read_first_frame_for_seat_anchors,
                )
                if first_frame_img is not None:
                    await loop.run_in_executor(
                        None,
                        seat_mapper.install_first_frame_anchors,
                        first_frame_img,
                    )
                else:
                    logger.warning(
                        "Seat mapper: could not read first frame for YOLO anchors: %s",
                        frames_info[0].get("frame_path"),
                    )

        # Single seat-mapping identification frame (first frame + DB names), reused for all report rows.
        seat_mapping_identification_path: Optional[str] = None
        seat_mapping_identification_url: Optional[str] = None
        if seat_mapper and self.db_session and frames_info:
            try:
                from app.video_processing.first_frame_seat_mapping import (
                    build_identification_rows_from_anchors,
                )

                anchors = getattr(seat_mapper, "_first_frame_anchors", None) or {}
                if anchors:
                    import cv2 as _cv2

                    ff_path = frames_info[0]["frame_path"]
                    ff_img = _cv2.imread(ff_path)
                    if ff_img is not None:
                        id_rows = build_identification_rows_from_anchors(
                            self.db_session, anchors
                        )
                        if id_rows:
                            (
                                seat_mapping_identification_path,
                                seat_mapping_identification_url,
                            ) = _save_student_identification_frame(
                                ff_img, ff_path, id_rows
                            )
            except Exception as _one_map_exc:
                logger.warning(
                    "One-time seat-mapping identification image failed: %s",
                    _one_map_exc,
                )

        # Step 4: AI engine processes each frame.
        # cv2.imread + model inference are blocking CPU/disk operations; run each in the
        # thread-pool so we don't freeze the event loop between frames.

        def _analyse_frame_sync(frame_info):
            """Blocking helper: load frame, run student AI (optional), invigilator AI, save evidence."""
            fp = frame_info["frame_path"]
            fn = frame_info["frame_number"]
            ts = frame_info["timestamp"]

            import cv2 as _cv2
            frame = _cv2.imread(fp)
            if frame is None:
                return None, fp, fn, ts

            if self.process_frame:
                analysis = self.process_frame(frame, fn, ts, return_annotated=True)
            else:
                analysis = {
                    "student_behaviors": [],
                    "invigilator_behaviors": [],
                }

            # Invigilator runs whenever the adapter is loaded (independent of student AI)
            if self.invig_adapter is not None:
                try:
                    invig_behaviors = self.invig_adapter.process_frame(frame, fn, ts)
                    analysis["invigilator_behaviors"] = invig_behaviors
                except Exception as _invig_exc:
                    logger.warning(
                        "Invigilator detection failed on frame %d: %s",
                        fn,
                        _invig_exc,
                        exc_info=True,
                    )

            evidence_url = _evidence_path_to_url(fp)
            if analysis.get("annotated_frame") is not None:
                ann_path = _pipeline_annotated_path(fp)
                _cv2.imwrite(ann_path, analysis["annotated_frame"])
                fp = ann_path
                try:
                    from app.storage.blob_storage import upload_evidence_frame
                    blob_url = upload_evidence_frame(ann_path)
                    evidence_url = blob_url if blob_url else _evidence_path_to_url(ann_path)
                except Exception as exc:
                    logger.debug("Blob upload skipped or failed: %s", exc)
                    evidence_url = _evidence_path_to_url(ann_path)

            analysis["_evidence_url"] = evidence_url
            analysis["_frame_path"] = fp

            # Seat polygons: align to COCO person box when possible (behaviour bbox unchanged)
            if seat_mapper and analysis.get("student_behaviors"):
                from app.video_processing.person_seat_anchor import (
                    attach_person_anchors_for_seats,
                )
                attach_person_anchors_for_seats(frame, analysis["student_behaviors"])

            return analysis, fp, fn, ts

        for idx, frame_info in enumerate(frames_info):
            raw_frame_source = frame_info['frame_path']
            frame_path = raw_frame_source
            frame_number = frame_info['frame_number']
            timestamp = frame_info['timestamp']

            if self.process_frame or self.invig_adapter:
                analysis, frame_path, frame_number, timestamp = await loop.run_in_executor(
                    None, _analyse_frame_sync, frame_info
                )
                if analysis is None:
                    logger.warning(f"Failed to load frame: {frame_info['frame_path']}")
                    continue

                evidence_url_preferred = analysis.pop("_evidence_url", _evidence_path_to_url(frame_path))
                analysis.pop("_frame_path", None)
                report_frame = None

                def _get_report_frame():
                    nonlocal report_frame
                    if report_frame is None:
                        report_frame = cv2.imread(raw_frame_source)
                    return report_frame

                frame_analyses.append({
                    "frame_number": frame_number,
                    "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
                    "detections": len(analysis.get('student_behaviors', [])) + len(analysis.get('invigilator_behaviors', []))
                })

                # Step 5 & 6: Process detections, map to seats, and log
                student_behaviors = analysis.get('student_behaviors', [])
                invigilator_behaviors = analysis.get('invigilator_behaviors', [])
            else:
                # No student AI and no invigilator adapter — only log frame extraction
                frame_analyses.append({
                    "frame_number": frame_number,
                    "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
                    "frame_path": frame_path,
                    "detections": 0
                })
                student_behaviors = []
                invigilator_behaviors = []
                logger.info(f"Frame {frame_number} extracted (no AI: student and invigilator disabled)")
            
            # Process student behaviors: merge same-frame same-student into one detection (severity-ordered type)
            frame_student_groups: Dict[tuple, List[Dict]] = defaultdict(list)
            _unmapped_slot_rec = [0]

            def _frame_group_key_rec(student_id, frame_no: int, behavior: Dict) -> tuple:
                if student_id:
                    return ("id", str(student_id), frame_no)
                student_index = behavior.get("student_index")
                if student_index is not None:
                    return ("idx", int(student_index), frame_no)
                bbox = behavior.get("bbox")
                if bbox and len(bbox) >= 4:
                    bbox_key = tuple(int(round(float(x))) for x in bbox[:4])
                else:
                    _unmapped_slot_rec[0] += 1
                    bbox_key = ("slot", _unmapped_slot_rec[0])
                return ("bbox", bbox_key, frame_no)

            for behavior in student_behaviors:
                seat_id = None
                student_id = None
                if seat_mapper and behavior.get("bbox"):
                    seat_bbox = behavior.get("_seat_bbox") or behavior["bbox"]
                    result = seat_mapper.get_student_for_bbox(seat_bbox)
                    if result:
                        seat_id, student_id = result

                gkey = _frame_group_key_rec(student_id, frame_number, behavior)
                frame_student_groups[gkey].append(
                    {
                        **behavior,
                        "seat_id": seat_id,
                        "student_id": str(student_id) if student_id else None,
                    }
                )
            frame_student_groups = _merge_unmapped_frame_groups(frame_student_groups)
            shared_student_report_evidence_path = None
            shared_student_report_evidence_url = None
            identification_evidence_path = None
            identification_evidence_url = None
            shared_student_report_bboxes = [
                b.get("bbox")
                for blist in frame_student_groups.values()
                for b in blist
                if b.get("bbox")
            ]
            if shared_student_report_bboxes:
                shared_student_report_evidence_path = frame_path
                shared_student_report_evidence_url = _evidence_path_to_url(frame_path)
                try:
                    from app.storage.blob_storage import upload_evidence_frame

                    ru = upload_evidence_frame(frame_path)
                    if ru:
                        shared_student_report_evidence_url = ru
                except Exception as _rep_up_exc:
                    logger.debug("Report frame blob upload skipped: %s", _rep_up_exc)
                identification_evidence_path = seat_mapping_identification_path
                identification_evidence_url = seat_mapping_identification_url
            for _gkey, blist in frame_student_groups.items():
                labels = [b["behavior_type"] for b in blist]
                merged = merge_behavior_labels(labels)
                if not merged:
                    continue
                base_sev = merged_base_severity(labels)
                details = merge_frame_behavior_details(blist)
                max_conf = max(float(b.get("confidence") or 0) for b in blist)
                detection = {
                    "timestamp": timestamp.isoformat(),
                    "frame_number": frame_number,
                    "behavior_type": merged,
                    "severity": base_sev,
                    "confidence": max_conf,
                    "seat_id": blist[0].get("seat_id"),
                    "student_id": blist[0].get("student_id"),
                    "details": details or blist[0].get("details", ""),
                    "evidence_path": frame_path,
                    "evidence_url": evidence_url_preferred,
                    "report_evidence_path": shared_student_report_evidence_path,
                    "report_evidence_url": shared_student_report_evidence_url,
                    "identification_evidence_path": identification_evidence_path,
                    "identification_evidence_url": identification_evidence_url,
                    "actor_type": "student",
                }
                sk = (
                    str(blist[0].get("student_id"))
                    if blist[0].get("student_id")
                    else _anonymous_student_bucket(
                        blist[0],
                        seat_id=blist[0].get("seat_id"),
                    )
                )
                detections_by_student[sk].append(detection)
            
            # Process invigilator behaviors — persist detection frame to blob (R2) for this row
            for behavior in invigilator_behaviors:
                report_evidence_path = None
                report_evidence_url = None
                rf = _get_report_frame()
                if behavior.get("bbox"):
                    report_evidence_path, report_evidence_url = _save_report_bbox_frame(
                        rf,
                        raw_frame_source,
                        "invigilator",
                        f"frame_{frame_number}_{behavior.get('tracker_id', 'unknown')}_{behavior['behavior_type'][:32]}",
                        [behavior.get("bbox")],
                        (255, 0, 0),
                    )
                elif rf is not None:
                    report_evidence_path, report_evidence_url = _save_invigilator_full_frame_evidence(
                        rf,
                        raw_frame_source,
                        f"f{frame_number}_{behavior.get('tracker_id', 'u')}_{str(behavior.get('behavior_type', ''))[:40]}",
                    )
                detection_url = report_evidence_url
                detection_path = report_evidence_path
                activity = {
                    "timestamp": timestamp.isoformat(),
                    "frame_number": frame_number,
                    "behavior_type": behavior['behavior_type'],
                    "severity": behavior['severity'],
                    "confidence": behavior['confidence'],
                    "details": behavior.get('details', ''),
                    "tracker_id": behavior.get('tracker_id'),
                    "bbox": behavior.get('bbox'),
                    "evidence_path": detection_path or frame_path,
                    "evidence_url": detection_url
                    or (
                        evidence_url_preferred
                        if (self.process_frame or self.invig_adapter)
                        else _evidence_path_to_url(frame_path)
                    ),
                    "report_evidence_path": report_evidence_path,
                    "report_evidence_url": report_evidence_url,
                    "actor_type": "invigilator",
                }
                activities.append(activity)
                
                if self.db_session:
                    await self._log_invigilator_activity_to_db(activity, room_id)
            
            # Progress logging
            if (idx + 1) % 10 == 0:
                logger.info(f"Analyzed {idx + 1}/{len(frames_info)} frames")

        invigilator_phase_count = sum(
            1 for a in activities if a.get("actor_type") == "invigilator"
        )
        logger.info(
            "Invigilator detection summary: adapter=%s, invigilator behaviours in batch=%d "
            "(each should produce 'Logged invigilator activity … to DB' when USE_DATABASE is on)",
            "active" if self.invig_adapter else "none",
            invigilator_phase_count,
        )

        # Run-based logic: one activity + one violation per qualifying run per student (no redundant per-frame)
        for student_key, det_list in detections_by_student.items():
            runs = get_runs_from_detections(det_list)
            qualifying = filter_qualifying_runs(runs)
            if not qualifying and det_list:
                logger.info(
                    "No qualifying student runs in recorded mode for key=%s; using fallback detections=%d",
                    student_key,
                    len(det_list),
                )
                for fd in det_list:
                    severity_str, severity_rule = resolve_activity_severity(
                        str(fd.get("behavior_type") or "unknown"),
                        1,
                    )
                    activity = {
                        "timestamp": fd.get("timestamp"),
                        "frame_number": fd.get("frame_number"),
                        "behavior_type": fd.get("behavior_type"),
                        "severity": severity_str,
                        "run_frame_count": 1,
                        "severity_rule": f"{severity_rule}_fallback",
                        "confidence": fd.get("confidence"),
                        "seat_id": fd.get("seat_id"),
                        "student_id": fd.get("student_id") if not str(student_key).startswith(("bbox:", "seat:", "unidentified")) else None,
                        "details": (fd.get("details", "") or "") + " [fallback: non-qualifying short run]",
                        "evidence_path": fd.get("evidence_path"),
                        "evidence_url": fd.get("evidence_url"),
                        "report_evidence_path": fd.get("report_evidence_path"),
                        "report_evidence_url": fd.get("report_evidence_url"),
                        "identification_evidence_path": fd.get("identification_evidence_path")
                        or seat_mapping_identification_path,
                        "identification_evidence_url": fd.get("identification_evidence_url")
                        or seat_mapping_identification_url,
                        "actor_type": "student",
                    }
                    activities.append(activity)
                    violations.append({
                        "activity": activity,
                        "violation_type": fd.get("behavior_type"),
                        "severity_level": severity_to_int(severity_str),
                        "status": "pending",
                        "evidence_url": fd.get("evidence_url") or fd.get("evidence_path"),
                        "timestamp": fd.get("timestamp"),
                    })
                    if self.db_session:
                        await self._log_activity_and_violation(
                            activity, exam_id, room_id,
                            create_violation=True,
                        )
            for run in qualifying:
                fd = run.first_detection
                severity_str, severity_rule = resolve_activity_severity(
                    run.label_raw, run.frame_count
                )
                activity = {
                    "timestamp": fd.get("timestamp"),
                    "frame_number": fd.get("frame_number"),
                    "behavior_type": run.label_raw,
                    "severity": severity_str,
                    "run_frame_count": run.frame_count,
                    "severity_rule": severity_rule,
                    "confidence": fd.get("confidence"),
                    "seat_id": fd.get("seat_id"),
                    "student_id": fd.get("student_id") if not str(student_key).startswith(("bbox:", "seat:", "unidentified")) else None,
                    "details": fd.get("details", "") or f"({run.frame_count} consecutive frames)",
                    "evidence_path": fd.get("evidence_path"),
                    "evidence_url": fd.get("evidence_url"),
                    "report_evidence_path": fd.get("report_evidence_path"),
                    "report_evidence_url": fd.get("report_evidence_url"),
                    "identification_evidence_path": seat_mapping_identification_path
                    or fd.get("identification_evidence_path"),
                    "identification_evidence_url": seat_mapping_identification_url
                    or fd.get("identification_evidence_url"),
                    "actor_type": "student",
                }
                activities.append(activity)
                violations.append({
                    "activity": activity,
                    "violation_type": run.label_raw,
                    "severity_level": severity_to_int(severity_str),
                    "status": "pending",
                    "evidence_url": fd.get("evidence_url") or fd.get("evidence_path"),
                    "timestamp": fd.get("timestamp"),
                })
                if self.db_session:
                    await self._log_activity_and_violation(
                        activity, exam_id, room_id,
                        create_violation=True,
                    )

        violations.sort(
            key=lambda v: (-int(v.get("severity_level", 0)), str(v.get("timestamp") or ""))
        )

        return {
            "success": True,
            "activities_logged": activities,
            "violations_detected": violations,
            "frame_analysis": frame_analyses,
            "total_frames_analyzed": len(frames_info),
            "total_frames_processed": len(frames_info),
            "total_frames_in_video": extraction_result.get('total_frames', len(frames_info)),
            "extraction_result": extraction_result
        }
    
    def _get_or_create_unidentified_student(self):
        """Get or create a placeholder student for unmapped detections."""
        from database.models import Student

        UNIDENTIFIED_EMAIL = "unidentified-ai-detection@foresyte.system"
        student = self.db_session.query(Student).filter(
            Student.email == UNIDENTIFIED_EMAIL
        ).first()
        if not student:
            student = Student(
                name="Unidentified (AI Detection)",
                email=UNIDENTIFIED_EMAIL,
                roll_number="UNIDENTIFIED-AI",
            )
            self.db_session.add(student)
            self.db_session.commit()
            self.db_session.refresh(student)
            logger.info("Created Unidentified placeholder student for unmapped detections")
        return str(student.student_id)

    async def _log_activity_and_violation(
        self, activity: Dict, exam_id: str, room_id: str, create_violation: bool = False
    ):
        """
        Step 6 of UC-07: Store StudentActivity and optionally Violation in database.
        Uses student_id from seat mapping when available; otherwise uses Unidentified placeholder.
        """
        if not self.db_session:
            return
        student_id = activity.get("student_id")
        if not student_id:
            student_id = self._get_or_create_unidentified_student()
            logger.debug(
                "No seat mapping for %s - saving as Unidentified",
                activity.get("behavior_type")
            )
        try:
            from uuid import UUID
            from database.models import StudentActivity, Violation

            ts = activity.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")) if "T" in ts else datetime.fromisoformat(ts)
            else:
                ts = ts or datetime.utcnow()

            raw = activity.get("evidence_url") or activity.get("evidence_path")
            if raw and (str(raw).startswith("http://") or str(raw).startswith("https://")):
                evidence_url = raw  # Already a full URL (e.g. R2 / B2)
            else:
                evidence_url = _evidence_path_to_url(raw)
            report_raw = activity.get("report_evidence_url") or activity.get("report_evidence_path")
            if report_raw and (str(report_raw).startswith("http://") or str(report_raw).startswith("https://")):
                report_evidence_url = report_raw
            else:
                report_evidence_url = _evidence_path_to_url(report_raw)
            identification_raw = activity.get("identification_evidence_url") or activity.get("identification_evidence_path")
            if identification_raw and (str(identification_raw).startswith("http://") or str(identification_raw).startswith("https://")):
                identification_evidence_url = identification_raw
            else:
                identification_evidence_url = _evidence_path_to_url(identification_raw)
            student_activity = StudentActivity(
                student_id=UUID(student_id),
                exam_id=UUID(exam_id),
                activity_type=activity.get("behavior_type"),
                severity=activity.get("severity"),
                confidence=activity.get("confidence"),
                evidence_url=evidence_url,
                report_evidence_url=report_evidence_url,
                identification_evidence_url=identification_evidence_url,
                timestamp=ts,
                run_frame_count=activity.get("run_frame_count"),
                severity_rule=activity.get("severity_rule"),
            )
            self.db_session.add(student_activity)
            self.db_session.commit()
            self.db_session.refresh(student_activity)
            logger.info(
                "Logged activity to DB: %s for student %s",
                activity["behavior_type"],
                student_id
            )

            if create_violation:
                sev = activity.get("severity")
                violation_severity = (
                    severity_to_int(sev)
                    if isinstance(sev, str)
                    else int(sev)
                    if sev is not None
                    else 1
                )
                violation = Violation(
                    activity_id=student_activity.activity_id,
                    violation_type=activity.get("behavior_type"),
                    timestamp=ts,
                    severity=violation_severity,
                    status="pending",
                    evidence_url=evidence_url,
                )
                self.db_session.add(violation)
                self.db_session.commit()
                logger.info("Created violation for %s (student %s)", activity["behavior_type"], student_id)
        except Exception as e:
            logger.warning("Failed to log activity/violation to DB: %s", e)
            if self.db_session:
                self.db_session.rollback()
    
    async def _log_invigilator_activity_to_db(self, activity: Dict, room_id: str):
        """
        Step 6 of UC-07 (invigilator path): Store InvigilatorActivity in the database.

        Resolves the invigilator identity via ExamRoomAssignment for the current
        exam + room.  If no assignment exists the record is still saved with
        invigilator_id=None so the alert is not silently lost.
        """
        if not self.db_session:
            return
        try:
            from uuid import UUID
            from database.models import InvigilatorActivity, ExamRoomAssignment

            room_uuid = UUID(room_id)

            # Resolve which invigilator is assigned to this room for the exam
            invigilator_id = None
            if self._current_exam_id:
                try:
                    assignment = (
                        self.db_session.query(ExamRoomAssignment)
                        .filter(
                            ExamRoomAssignment.room_id == room_uuid,
                            ExamRoomAssignment.exam_id == UUID(self._current_exam_id),
                        )
                        .first()
                    )
                    if assignment:
                        invigilator_id = assignment.invigilator_id
                        logger.debug(
                            "Resolved invigilator %s for room %s / exam %s",
                            invigilator_id, room_id, self._current_exam_id,
                        )
                    else:
                        logger.debug(
                            "No ExamRoomAssignment found for room %s / exam %s — "
                            "saving invigilator activity without invigilator_id",
                            room_id, self._current_exam_id,
                        )
                except Exception as lookup_err:
                    logger.warning("Invigilator lookup failed: %s", lookup_err)

            ts = activity.get("timestamp")
            if isinstance(ts, str):
                ts = (
                    datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if "T" in ts
                    else datetime.fromisoformat(ts)
                )
            else:
                ts = ts or datetime.utcnow()

            # Primary URL: bbox/full detection frame (blob/R2 after upload), not generic pipeline frame
            raw_url = (
                activity.get("report_evidence_url")
                or activity.get("evidence_url")
                or activity.get("report_evidence_path")
                or activity.get("evidence_path")
            )
            if raw_url and (
                str(raw_url).startswith("http://") or str(raw_url).startswith("https://")
            ):
                evidence_url = raw_url
            else:
                evidence_url = _evidence_path_to_url(raw_url)
            report_raw_url = activity.get("report_evidence_url") or activity.get("report_evidence_path")
            if report_raw_url and (
                str(report_raw_url).startswith("http://") or str(report_raw_url).startswith("https://")
            ):
                report_evidence_url = report_raw_url
            else:
                report_evidence_url = _evidence_path_to_url(report_raw_url)

            notes_parts = [activity.get("details", "")]
            tracker_id = activity.get("tracker_id")
            if tracker_id is not None:
                notes_parts.append(f"tracker_id={tracker_id}")
            bbox = activity.get("bbox")
            if bbox:
                notes_parts.append(f"bbox={bbox}")
            notes = " | ".join(p for p in notes_parts if p)

            inv_activity = InvigilatorActivity(
                invigilator_id=invigilator_id,
                room_id=room_uuid,
                timestamp=ts,
                activity_type=activity.get("behavior_type"),
                severity=activity.get("severity"),
                confidence=activity.get("confidence"),
                frame_number=activity.get("frame_number"),
                evidence_url=evidence_url,
                report_evidence_url=report_evidence_url,
                notes=notes or None,
            )
            self.db_session.add(inv_activity)
            self.db_session.commit()
            logger.info(
                "Logged invigilator activity [%s] to DB (invigilator=%s, room=%s)",
                activity.get("behavior_type"),
                invigilator_id or "unknown",
                room_id,
            )
        except Exception as e:
            logger.warning("Failed to log invigilator activity to DB: %s", e)
            if self.db_session:
                self.db_session.rollback()
    
    def get_processing_results(self, stream_id: str) -> Optional[Dict[str, Any]]:
        """
        Steps 7-8 of UC-07: Retrieve processed footage results for investigator.
        
        Args:
            stream_id: Stream identifier
            
        Returns:
            Processing results or None
        """
        return self.processing_results.get(stream_id)
    
    def generate_report(self, stream_id: str, report_format: str = 'json') -> Dict[str, Any]:
        """
        Steps 9-10 of UC-07: Generate final report for investigator.
        
        Args:
            stream_id: Stream identifier
            report_format: Output format (json, pdf, csv)
            
        Returns:
            Report data
        """
        results = self.get_processing_results(stream_id)
        
        if not results:
            return {
                "success": False,
                "error": f"No results found for stream {stream_id}"
            }
        
        # Compile comprehensive report
        report = {
            "report_id": f"report_{stream_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
            "stream_id": stream_id,
            "exam_id": results.get('exam_id'),
            "room_id": results.get('room_id'),
            "generated_at": datetime.utcnow().isoformat(),
            "processing_summary": {
                "started_at": results.get('started_at'),
                "completed_at": results.get('completed_at'),
                "stream_type": results.get('stream_type'),
                "total_frames": results.get('total_frames_processed', 0)
            },
            "activities_summary": {
                "total_activities": len(results.get('activities_logged', [])),
                "student_activities": len([a for a in results.get('activities_logged', []) 
                                          if a.get('actor_type') == 'student']),
                "invigilator_issues": len([a for a in results.get('activities_logged', []) 
                                          if a.get('actor_type') == 'invigilator'])
            },
            "violations_summary": {
                "total_violations": len(results.get('violations_detected', [])),
                "high_severity": len([v for v in results.get('violations_detected', []) 
                                     if v.get('severity_level', 0) >= 3]),
                "pending_review": len([v for v in results.get('violations_detected', []) 
                                      if v.get('status') == 'pending'])
            },
            "detailed_activities": results.get('activities_logged', []),
            "violations": results.get('violations_detected', []),
            "format": report_format
        }
        
        # Save report based on format
        if report_format == 'json':
            report_path = self._save_json_report(report)
            report['report_path'] = report_path
        
        return report
    
    def _save_json_report(self, report: Dict) -> str:
        """
        Save report as JSON file.
        
        Args:
            report: Report data
            
        Returns:
            Path to saved report
        """
        reports_dir = Path("uploads/reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        report_filename = f"{report['report_id']}.json"
        report_path = reports_dir / report_filename
        
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Report saved to: {report_path}")
        return str(report_path)

