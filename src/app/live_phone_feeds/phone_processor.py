"""
Phone Feed Processor
Processes live video feeds from phones using the existing VideoProcessor
"""

import asyncio
import logging
import os
from typing import Dict, Any, Optional
from datetime import datetime
from pathlib import Path
import sys
import cv2
import numpy as np

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.live_phone_feeds.phone_stream_receiver import PhoneStreamReceiver
from app.video_processing.processor import VideoProcessor, LiveFootageAccumState
from app.video_processing.stream_handler import ensure_session_frame_dirs_under

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PhoneFeedProcessor:
    """
    Processes live video feeds from phones.
    Integrates with existing VideoProcessor for analysis.
    """
    
    def __init__(self, db_session=None, enable_ai: Optional[bool] = None, save_frames=True, frame_dir="uploads/frames"):
        """
        Initialize phone feed processor.

        Args:
            db_session: Database session (optional)
            enable_ai: Run student + invigilator pipeline on sampled frames (default: env
                PHONE_MONITORING_ENABLE_AI, or True).
            save_frames: Save frames to disk (default: True)
            frame_dir: Directory to save frames (default: "uploads/frames")
        """
        self.receiver = PhoneStreamReceiver()
        if enable_ai is None:
            enable_ai = os.getenv("PHONE_MONITORING_ENABLE_AI", "true").strip().lower() not in (
                "0",
                "false",
                "no",
                "off",
            )
        self.enable_ai = bool(enable_ai)
        self.processor = VideoProcessor(db_session=db_session, enable_ai=self.enable_ai)
        self.current_stream_id = None
        self.is_processing = False
        self.save_frames = save_frames
        self.frame_dir = Path(frame_dir).resolve()
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        # Real-time tracking
        self.live_frame_count = {}  # stream_id -> frame_count
        self.live_stream_url = {}  # stream_id -> stream_url
        
    async def start_phone_feed_processing(
        self,
        stream_url: str,
        stream_id: str,
        exam_id: str,
        room_id: str,
        seat_mapping: Dict = None,
        duration_seconds: int = 3600,
        process_every_n_frames: int = 30
    ) -> Dict[str, Any]:
        """
        Start processing live feed from phone.
        
        Args:
            stream_url: Phone stream URL
            stream_id: Unique stream identifier
            exam_id: Exam identifier
            room_id: Room identifier
            seat_mapping: Seat position mapping (optional)
            duration_seconds: How long to process (default: 1 hour)
            process_every_n_frames: Process 1 frame every N frames
            
        Returns:
            Processing results
        """
        logger.info(f"Starting phone feed processing: {stream_id}")
        logger.info(f"Stream URL: {stream_url}")
        
        # Test connection first
        connection_test = self.receiver.connect_to_phone_stream(stream_url)
        if not connection_test.get("success"):
            return {
                "success": False,
                "error": connection_test.get("error", "Connection failed"),
                "suggestions": connection_test.get("suggestions", [])
            }
        
        # Use the working URL from connection test (might be different from original)
        working_url = connection_test.get("stream_url", stream_url)
        if working_url != stream_url:
            logger.info(f"Using working URL: {working_url} (original: {stream_url})")
        
        self.current_stream_id = stream_id
        self.is_processing = True

        live_state = LiveFootageAccumState()
        self.processor._current_exam_id = exam_id
        self.processor._current_room_id = room_id
        self.processor._ensure_invigilator_adapter(stream_id)
        seat_payload: Dict[str, Any] = {"map": seat_mapping}

        saved_frames = []
        frame_count = 0

        async def frame_callback(frame, frame_num, timestamp):
            """Save sampled frame, then run the same live pipeline as CCTV (AI, seats, invig, R2)."""
            nonlocal frame_count

            frame_count += 1
            self.live_frame_count[stream_id] = frame_count

            frame_path = None
            if self.save_frames:
                try:
                    if not isinstance(frame, np.ndarray):
                        if hasattr(frame, "__array__"):
                            frame = np.array(frame)
                        else:
                            logger.warning(
                                "Frame %s is not in expected format (type: %s)",
                                frame_num,
                                type(frame),
                            )
                            return

                    frame_filename = (
                        f"phone_{stream_id}_{frame_num}_"
                        f"{timestamp.strftime('%Y%m%d_%H%M%S')}.jpg"
                    )
                    simple_dir, _ = ensure_session_frame_dirs_under(
                        self.frame_dir, stream_id
                    )
                    frame_path = simple_dir / frame_filename

                    success = cv2.imwrite(str(frame_path), frame)
                    if success:
                        saved_frames.append(
                            {
                                "frame_number": frame_num,
                                "timestamp": timestamp.isoformat(),
                                "frame_path": str(frame_path),
                            }
                        )
                        if frame_count % 10 == 0:
                            logger.info("Saved frame %s to %s", frame_num, frame_path)
                    else:
                        logger.warning(
                            "Failed to save frame %s — cv2.imwrite returned False",
                            frame_num,
                        )
                except Exception as e:
                    logger.error("Failed to save frame %s: %s", frame_num, e, exc_info=True)

            if frame_count % 10 == 0:
                logger.info(
                    "Captured %s frames from phone stream (%s saved)",
                    frame_count,
                    len(saved_frames) if self.save_frames else 0,
                )

            if self.enable_ai and isinstance(frame, np.ndarray):
                if seat_payload["map"] is None and self.processor.db_session:
                    try:
                        from uuid import UUID

                        UUID(str(room_id))
                        h, w = frame.shape[:2]
                        seat_payload["map"] = (
                            self.processor.stream_handler.get_seat_map_for_room(
                                str(room_id), w, h, self.processor.db_session
                            )
                        )
                    except (ValueError, TypeError, AttributeError):
                        pass
                stub = (
                    str(frame_path)
                    if frame_path is not None
                    else f"uploads/live/phone_{stream_id}_{frame_num}.jpg"
                )
                await self.processor._run_live_frame_pipeline(
                    frame,
                    frame_num,
                    timestamp,
                    seat_payload["map"],
                    room_id,
                    live_state,
                    live_frame_stub=stub,
                )

        # Store stream URL for real-time access
        self.live_stream_url[stream_id] = working_url
        self.live_frame_count[stream_id] = 0
        
        # Process the stream using the working URL
        try:
            stream_result = await self.receiver.process_phone_stream(
                stream_url=working_url,  # Use working URL from connection test
                duration_seconds=duration_seconds,
                frame_callback=frame_callback,
                process_every_n_frames=process_every_n_frames
            )

            if self.enable_ai:
                await self.processor._finalize_live_student_runs(
                    exam_id, room_id, live_state
                )
            activities = live_state.activities
            violations = live_state.violations

            # Compile results
            results = {
                "success": stream_result.get("success", False),
                "stream_id": stream_id,
                "exam_id": exam_id,
                "room_id": room_id,
                "stream_type": "live_phone",
                "stream_url": working_url,  # Use working URL
                "original_url": stream_url,  # Keep original for reference
                "started_at": datetime.utcnow().isoformat(),
                "completed_at": datetime.utcnow().isoformat(),
                "frames_captured": stream_result.get("frames_captured", 0),
                "frames_processed": stream_result.get("frames_processed", 0),
                "frames_saved": len(saved_frames) if self.save_frames else 0,
                "duration_seconds": stream_result.get("duration_seconds", 0),
                "activities_logged": activities,
                "violations_detected": violations,
                "saved_frames": saved_frames if self.save_frames else [],
                "frame_directory": str(self.frame_dir) if self.save_frames else None,
                "connection_info": connection_test
            }
            
            # Store results in processor
            self.processor.processing_results[stream_id] = results
            
            logger.info(f"Phone feed processing completed: {stream_id}")
            logger.info(f"Frames captured: {results['frames_captured']}")
            logger.info(f"Frames processed: {results['frames_processed']}")
            if self.save_frames:
                logger.info(f"Frames saved: {results['frames_saved']} to {self.frame_dir}")
            
            return results
            
        except Exception as e:
            logger.error(f"Error processing phone feed: {e}")
            return {
                "success": False,
                "error": str(e),
                "stream_id": stream_id
            }
        finally:
            self.is_processing = False
    
    def stop_processing(self):
        """Stop current phone feed processing"""
        if self.is_processing:
            self.receiver.stop_stream()
            self.is_processing = False
            logger.info("Phone feed processing stopped")
    
    def get_processing_results(self, stream_id: str) -> Optional[Dict[str, Any]]:
        """Get processing results for a stream"""
        return self.processor.get_processing_results(stream_id)
    
    def generate_report(self, stream_id: str, report_format: str = 'json') -> Dict[str, Any]:
        """Generate report for phone feed processing"""
        return self.processor.generate_report(stream_id, report_format)

