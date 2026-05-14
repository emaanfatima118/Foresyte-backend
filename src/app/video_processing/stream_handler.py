"""
Video Stream Handler - UC-07: Process Exam Footage (Live/Recorded)
Handles both live CCTV feeds and uploaded exam recordings
"""

import cv2
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
import asyncio
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import logging
import json

from .frame_overlay_timestamp import parse_exam_timestamp_from_frame

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# JPEG save params: lower quality → smaller writes, faster disk I/O (still fine for ML).
def _frame_jpeg_write_params() -> list:
    q = max(60, min(100, int(os.getenv("FRAME_JPEG_QUALITY", "88"))))
    return [int(cv2.IMWRITE_JPEG_QUALITY), q]


def _exam_ts_ocr_stride() -> int:
    """Run overlay OCR every N extracted frames (1 = legacy; larger = much faster extraction)."""
    try:
        n = int(os.getenv("EXAM_TS_OCR_EVERY_N_EXTRACTED", "30"))
    except ValueError:
        n = 30
    return max(1, n)


def _use_fast_grab_between_samples() -> bool:
    """grab() skips full decode between sampled frames — disable if decode issues on odd codecs."""
    return os.getenv("VIDEO_EXTRACT_FAST_SKIP", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _recorded_sampling_stride(video_fps: float) -> tuple[int, float]:
    """
    How many decoded frames between saved extracts (~ samples/sec = fps / stride).

    ``RECORDED_TARGET_SAMPLE_HZ`` (default ``1``) = nominal extracts per video second.
    Set to ``0.5`` to extract ~every 2s (fewer AI passes, faster). Caps at native fps.

    Deprecated alias: ``RECORDED_SAMPLES_PER_SECOND`` (same meaning).
    """
    fv = float(video_fps if video_fps and video_fps > 1e-3 else 30.0)
    raw = (os.getenv("RECORDED_TARGET_SAMPLE_HZ") or os.getenv(
        "RECORDED_SAMPLES_PER_SECOND", "1"
    )).strip()
    try:
        hz = float(raw)
    except ValueError:
        hz = 1.0
    hz = max(1.0 / 12.0, min(hz, fv))
    stride = max(1, int(round(fv / hz)))
    approx_hz = fv / stride
    return stride, approx_hz


def _seconds_between_extracted_frames(video_fps: float, frame_interval: int) -> float:
    """Video-timeline seconds between two consecutive sampled frames (= frame_interval / fps)."""
    fv = video_fps if video_fps and video_fps > 1e-3 else 30.0
    return float(max(1, int(frame_interval))) / fv


# Per video upload / job: uploads/frames/<job_id>/simple/  (raw extracts)
#                         uploads/frames/<job_id>/pipeline/  (after AI pipeline)
FRAME_SUBDIR_SIMPLE = "simple"
FRAME_SUBDIR_PIPELINE = "pipeline"


def _sanitize_session_id(job_id: Optional[str]) -> str:
    if not job_id or not str(job_id).strip():
        return "default"
    s = str(job_id).strip()
    return "".join(c for c in s if c.isalnum() or c in "-_") or "default"


def ensure_session_frame_dirs_under(
    frame_root: Path, job_id: Optional[str]
) -> tuple[Path, Path]:
    """Create <frame_root>/<session>/simple and .../pipeline; return both Paths."""
    session = frame_root.resolve() / _sanitize_session_id(job_id)
    simple = session / FRAME_SUBDIR_SIMPLE
    pipeline = session / FRAME_SUBDIR_PIPELINE
    simple.mkdir(parents=True, exist_ok=True)
    pipeline.mkdir(parents=True, exist_ok=True)
    return simple, pipeline


def _video_extract_mp_workers() -> int:
    """Extract processes for CPU-bound decode/encode; 1 = legacy single-process path."""
    try:
        n = int(os.getenv("VIDEO_EXTRACT_MP_WORKERS", "1"))
    except ValueError:
        n = 1
    return max(1, n)


def _split_extract_index_ranges(extract_total: int, parts: int) -> List[Tuple[int, int]]:
    """Split [0, extract_total) into ``parts`` half-open ranges (start inclusive, end exclusive)."""
    extract_total = max(0, int(extract_total))
    parts = max(1, int(parts))
    if extract_total == 0:
        return [(0, 0)]
    parts = min(parts, extract_total)
    base = extract_total // parts
    rem = extract_total % parts
    out: List[Tuple[int, int]] = []
    cur = 0
    for i in range(parts):
        extra = 1 if i < rem else 0
        nxt = cur + base + extra
        out.append((cur, nxt))
        cur = nxt
    return out


def _parse_anchor_iso(iso_val: Optional[str]) -> Optional[datetime]:
    if not iso_val:
        return None
    s = iso_val.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _mp_extract_chunk(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Picklable worker: extract frames for extract-indices [e_start, e_end).
    Logical frame index ``e`` maps to CAP frame ``e * frame_rate`` when sampling every ``frame_rate`` frames from 0.
    """
    video_path = payload["video_path"]
    simple_dir = Path(payload["simple_dir"])
    job_id = payload["job_id"]
    frame_rate = max(1, int(payload["frame_rate"]))
    e_start = int(payload["e_start"])
    e_end = int(payload["e_end"])
    total_frames = int(payload["total_frames"])
    fps_raw = float(payload.get("fps_raw") or 30.0)
    ocr_stride = max(1, int(payload.get("ocr_stride") or 30))
    jpeg_params = list(payload["jpeg_params"])
    run_ocr = bool(payload.get("run_ocr"))
    passed_anchor_iso = payload.get("passed_anchor_iso")
    passed_anchor_idx = payload.get("passed_anchor_idx")
    if passed_anchor_idx is not None:
        passed_anchor_idx = int(passed_anchor_idx)

    passed_anchor_dt = _parse_anchor_iso(passed_anchor_iso)
    anchor_exam_dt: Optional[datetime] = passed_anchor_dt
    anchor_extract_idx: Optional[int] = passed_anchor_idx

    sec_per_extract = timedelta(
        seconds=_seconds_between_extracted_frames(fps_raw, frame_rate)
    )
    frames_info: list = []
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error("MP extract: cannot open %s", video_path)
        return {"frames": frames_info, "anchor_exam_dt": None, "anchor_extract_idx": None}

    try:
        for e in range(e_start, e_end):
            phy_fn = e * frame_rate
            if phy_fn >= total_frames:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, phy_fn)
            ret, frame = cap.read()
            if not ret or frame is None:
                logger.warning("MP extract: read failed at logical e=%s phy=%s", e, phy_fn)
                continue

            exam_ts = None
            if run_ocr and (e % ocr_stride == 0):
                try:
                    exam_ts = parse_exam_timestamp_from_frame(frame)
                except Exception as _ocr_exc:
                    logger.warning("MP extract OCR error at e=%s: %s", e, _ocr_exc)

            if exam_ts is not None:
                anchor_exam_dt = exam_ts
                anchor_extract_idx = e
                timestamp = exam_ts
            elif anchor_exam_dt is not None and anchor_extract_idx is not None:
                timestamp = anchor_exam_dt + sec_per_extract * (e - anchor_extract_idx)
            else:
                timestamp = datetime.utcnow()
                if run_ocr and (e % ocr_stride == 0):
                    logger.debug(
                        "MP extract: no anchor at e=%s — using UTC processing time",
                        e,
                    )

            frame_filename = (
                f"frame_{job_id}_{phy_fn}_"
                f"{timestamp.strftime('%Y%m%d_%H%M%S')}.jpg"
            )
            frame_path = simple_dir / frame_filename
            cv2.imwrite(str(frame_path), frame, jpeg_params)

            frames_info.append(
                {
                    "frame_number": phy_fn,
                    "timestamp": timestamp,
                    "frame_path": str(frame_path),
                    "extracted": True,
                    "annotated": False,
                }
            )
    finally:
        cap.release()

    anchor_out = None
    anchor_idx_out = None
    if anchor_exam_dt is not None and anchor_extract_idx is not None:
        anchor_out = anchor_exam_dt.isoformat()
        anchor_idx_out = anchor_extract_idx

    return {
        "frames": frames_info,
        "anchor_exam_dt": anchor_out,
        "anchor_extract_idx": anchor_idx_out,
    }


class VideoStreamHandler:
    """
    Handles video stream processing for both live and recorded footage.
    FR-31: Process both live CCTV feeds and uploaded recordings
    """
    
    def __init__(self, upload_dir: str = "uploads/videos", frame_dir: str = "uploads/frames"):
        # Use absolute paths to avoid OpenCV path resolution issues
        self.upload_dir = Path(upload_dir).resolve()
        self.frame_dir = Path(frame_dir).resolve()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        
        # Path to seating plan directory
        self.seating_plan_dir = Path(__file__).parent.parent / "seating_plan"
        self.csfyp_dir = self.seating_plan_dir / "CSFYP"

    def ensure_session_frame_dirs(self, job_id: Optional[str]) -> tuple[Path, Path]:
        """Create uploads/frames/<session>/simple and .../pipeline."""
        return ensure_session_frame_dirs_under(self.frame_dir, job_id)

        
    def validate_video_input(self, source: str, stream_type: str) -> Dict[str, Any]:
        """
        Step 2 of UC-07: Validates video input and prepares it for analysis
        
        Args: 
            source: Video file path or CCTV stream URL
            stream_type: 'live' or 'recorded'
            
        Returns:
            Dict with validation status and video properties
        """
        try:
            logger.info(f"[validate_video_input] Validating: {source}")
            logger.info(f"[validate_video_input] File exists: {os.path.exists(source)}")
            logger.info(f"[validate_video_input] Absolute path: {os.path.abspath(source)}")
            logger.info(f"[validate_video_input] Current working directory: {os.getcwd()}")
            
            cap = cv2.VideoCapture(source)
            
            if not cap.isOpened():
                error_msg = f"Unable to open video source: {source}"
                logger.error(f"[validate_video_input] {error_msg}")
                logger.error(f"[validate_video_input] Tried absolute: {os.path.abspath(source)}")
                return {
                    "valid": False,
                    "error": error_msg,
                    "source": source,
                    "absolute_path": os.path.abspath(source),
                    "file_exists": os.path.exists(source)
                }
            
            # Get video properties
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            
            logger.info(f"[validate_video_input] Success! FPS={fps}, Frames={frame_count}, Size={width}x{height}")
            
            cap.release()
            
            duration = frame_count / fps if fps > 0 and stream_type == 'recorded' else 0
            
            return {
                "valid": True,
                "fps": fps,
                "frame_count": frame_count if stream_type == 'recorded' else -1,
                "width": width,
                "height": height,
                "duration": duration,
                "stream_type": stream_type
            }
            
        except Exception as e:
            logger.error(f"[validate_video_input] Exception for {source}: {str(e)}")
            import traceback
            traceback.print_exc()
            return {
                "valid": False,
                "error": str(e),
                "source": source
            }
    
    def get_seat_map_for_room(self, room_id: Optional[str], frame_width: int, frame_height: int, db_session=None) -> Optional[Dict[str, list]]:
        """
        Load and scale seat map for a room. Used for bbox-to-student mapping.
        Returns dict of seat_map_key -> polygon points, or None.
        """
        if not room_id or not db_session:
            return None
        try:
            from database.models import Room
            from uuid import UUID
            
            room = db_session.query(Room).filter(Room.room_id == UUID(room_id)).first()
            if not room:
                return None
            room_no = f"{room.block}-{room.room_number}" if room.block else room.room_number
            seat_map_path = self._get_room_paths(room_no)
            if seat_map_path:
                return self._load_seat_map(seat_map_path, frame_width, frame_height)
        except Exception as e:
            logger.error(f"Error loading seat map for room: {e}")
        return None

    def _get_room_paths(self, room_no: str):
        """
        Get room-specific seat_map.json path based on room number.
        Helper function similar to upload_plan.py get_room_paths.
        
        Args:
            room_no: Room number like "A-104", "A104", "B-127", "C-301", "C-311", "D-314"
        
        Returns:
            seat_map_path or None if not found
        """
        # Normalize room number (handle both "A-104" and "A104" formats)
        room_no_upper = room_no.upper().replace('-', '').replace(' ', '')
        room_block = room_no_upper[0] if room_no_upper and room_no_upper[0].isalpha() else None
        room_num = room_no_upper[1:] if len(room_no_upper) > 1 else None
        
        if not room_block or not room_num:
            return None
        
        # Determine which CSFYP folder to use
        if room_block == 'A':
            room_folder = self.csfyp_dir / "A104-25112025"
        elif room_block == 'B':
            room_folder = self.csfyp_dir / "B127-25112025"
        elif room_block == 'C':
            if room_num == '311':
                room_folder = self.csfyp_dir / "C311-25112025"
            else:
                room_folder = self.csfyp_dir / "C301-25112025"
        elif room_block == 'D':
            room_folder = self.csfyp_dir / "D314-25112025"
        else:
            return None
        
        # Find seat_map.json
        seat_map_path = room_folder / "seat_map.json"
        if not seat_map_path.exists():
            logger.warning(f"Seat map not found at {seat_map_path}")
            return None
        
        return seat_map_path
    
    def _load_seat_map(self, seat_map_path: Path, frame_width: int, frame_height: int):
        """
        Load seat map JSON and scale coordinates to match frame dimensions.
        
        Args:
            seat_map_path: Path to seat_map.json file
            frame_width: Width of the video frame
            frame_height: Height of the video frame
        
        Returns:
            Dictionary of seat_id -> scaled polygon points, or None if error
        """
        try:
            with open(seat_map_path, 'r', encoding='utf-8') as f:
                seat_map_data = json.load(f)
            
            seats = seat_map_data.get('seats', {})
            meta = seat_map_data.get('_meta', {})
            base_w = meta.get('base_w', frame_width)
            base_h = meta.get('base_h', frame_height)
            
            # Calculate scaling factors
            scale_x = frame_width / base_w if base_w > 0 else 1.0
            scale_y = frame_height / base_h if base_h > 0 else 1.0
            
            # Scale all seat polygons
            scaled_seats = {}
            for seat_id, polygon in seats.items():
                if polygon and len(polygon) >= 3:
                    scaled_polygon = [
                        [int(point[0] * scale_x), int(point[1] * scale_y)]
                        for point in polygon if len(point) >= 2
                    ]
                    if len(scaled_polygon) >= 3:
                        scaled_seats[seat_id] = scaled_polygon
            
            logger.info(f"Loaded {len(scaled_seats)} seats from seat map, scaled from {base_w}x{base_h} to {frame_width}x{frame_height}")
            return scaled_seats
            
        except Exception as e:
            logger.error(f"Error loading seat map: {str(e)}")
            return None
    
    def extract_frames(self, video_source: str, frame_rate: int = 1, 
                      job_id: str = None, progress_callback=None, 
                      room_id: Optional[str] = None, db_session=None) -> list:
        """
        Extracts frames from video for analysis.
        Used in Step 3 of UC-07: Process video frames

        Performance (see env vars):
        ``EXAM_TS_OCR_EVERY_N_EXTRACTED`` (default 30) — OCR is expensive; timestamps between
        runs are interpolated from the overlay clock. Set to ``1`` for legacy per-frame OCR.
        ``VIDEO_EXTRACT_FAST_SKIP`` — when ``1`` (default), use ``grab()`` between samples.
        ``FRAME_JPEG_QUALITY`` (default 88) — JPEG quality for extracted files.

        Args:
            video_source: Path to video file or stream URL
            frame_rate: Extract 1 frame per N frames (default: 1 = every frame)
            job_id: Processing job identifier
            progress_callback: Callback function for progress updates
            room_id: Unused for drawing (kept for API compatibility; seat polygons are not drawn on frames)
            db_session: Unused for drawing (kept for API compatibility)
            
        Returns:
            List of extracted frame information (frame_path under .../<job_id>/simple/)
        """
        frames_info = []
        simple_dir, _pipeline_dir = self.ensure_session_frame_dirs(job_id)
        
        # Log video source for debugging
        logger.info(f"Attempting to open video: {video_source}")
        logger.info(f"Video source exists: {os.path.exists(video_source)}")
        logger.info(f"Current working directory: {os.getcwd()}")
        
        cap = cv2.VideoCapture(video_source)
        
        if not cap.isOpened():
            logger.error(f"Cannot open video source: {video_source}")
            logger.error(f"Tried absolute path: {os.path.abspath(video_source)}")
            return frames_info
        
        frame_number = 0
        extracted_count = 0
        ocr_stride = _exam_ts_ocr_stride()
        jpeg_params = _frame_jpeg_write_params()

        fps_raw = float(cap.get(cv2.CAP_PROP_FPS))
        sec_per_extract = timedelta(
            seconds=_seconds_between_extracted_frames(fps_raw, frame_rate)
        )

        anchor_exam_dt: Optional[datetime] = None
        anchor_extract_idx: Optional[int] = None

        fast_grab_skip = (
            frame_rate > 1
            and _use_fast_grab_between_samples()
        )

        # Get total frame count for progress tracking
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        # Calculate expected number of extracted frames
        expected_extracted_frames = (total_frames // frame_rate) + (1 if total_frames % frame_rate > 0 else 0)
        logger.info(
            "Video has %s total frames, extracting every %s frames (expected: ~%s); "
            "OCR every %s extracted frame(s); JPEG params %s",
            total_frames,
            frame_rate,
            expected_extracted_frames,
            ocr_stride,
            jpeg_params,
        )

        try:
            while True:
                if fast_grab_skip and frame_number % frame_rate != 0:
                    if not cap.grab():
                        break
                    frame_number += 1
                    continue

                if not fast_grab_skip and frame_number % frame_rate != 0:
                    ret, _ = cap.read()
                    if not ret:
                        break
                    frame_number += 1
                    continue

                ret, frame = cap.read()
                if not ret:
                    break

                exam_ts = None
                run_ocr = extracted_count % ocr_stride == 0
                if run_ocr:
                    try:
                        exam_ts = parse_exam_timestamp_from_frame(frame)
                    except Exception as _ocr_exc:
                        logger.warning("Exam overlay timestamp OCR error: %s", _ocr_exc)

                if exam_ts is not None:
                    anchor_exam_dt = exam_ts
                    anchor_extract_idx = extracted_count
                    timestamp = exam_ts
                elif anchor_exam_dt is not None and anchor_extract_idx is not None:
                    timestamp = anchor_exam_dt + sec_per_extract * (
                        extracted_count - anchor_extract_idx
                    )
                    logger.debug(
                        "Frame %s: timestamp interpolated from OCR anchor (extract #%s)",
                        frame_number,
                        extracted_count,
                    )
                else:
                    timestamp = datetime.utcnow()
                    if run_ocr:
                        logger.debug(
                            "Frame %s: no OCR anchor yet — UTC processing time",
                            frame_number,
                        )

                frame_filename = (
                    f"frame_{job_id}_{frame_number}_"
                    f"{timestamp.strftime('%Y%m%d_%H%M%S')}.jpg"
                )
                frame_path = simple_dir / frame_filename
                cv2.imwrite(str(frame_path), frame, jpeg_params)

                frames_info.append({
                    "frame_number": frame_number,
                    "timestamp": timestamp,
                    "frame_path": str(frame_path),
                    "extracted": True,
                    "annotated": False,
                })

                extracted_count += 1

                if progress_callback:
                    try:
                        progress_callback(extracted_count, expected_extracted_frames)
                    except Exception as e:
                        logger.warning(f"Progress callback error: {e}")

                if extracted_count % 100 == 0:
                    logger.info(
                        "Extracted %s frames from job %s (out of ~%s expected)",
                        extracted_count,
                        job_id,
                        total_frames // frame_rate,
                    )

                frame_number += 1
                
        except Exception as e:
            logger.error(f"Error extracting frames: {str(e)}")
        finally:
            cap.release()
            
        logger.info(f"Total frames extracted: {extracted_count} from {frame_number} total frames")
        return frames_info

    def extract_frames_parallel(
        self,
        video_source: str,
        frame_rate: int,
        job_id: str,
        progress_callback,
        total_frames: int,
        num_workers: int,
        room_id: Optional[str] = None,
        db_session=None,
    ) -> list:
        """
        CPU-bound extraction using multiple processes (OpenCV seek + JPEG encode per chunk).
        Chunk 0 runs OCR for exam timestamps; later chunks interpolate from that anchor.

        Set ``VIDEO_EXTRACT_MP_WORKERS`` > 1 to enable. Some codecs seek poorly; set to 1 if artifacts appear.
        """
        simple_dir, _pipeline_dir = self.ensure_session_frame_dirs(job_id)
        frame_rate = max(1, int(frame_rate))
        expected_extracted = (total_frames // frame_rate) + (
            1 if total_frames % frame_rate > 0 else 0
        )
        num_workers = max(1, min(int(num_workers), expected_extracted))
        ranges = _split_extract_index_ranges(expected_extracted, num_workers)

        cap_probe = cv2.VideoCapture(video_source)
        fps_raw = float(cap_probe.get(cv2.CAP_PROP_FPS)) if cap_probe.isOpened() else 30.0
        cap_probe.release()

        ocr_stride = _exam_ts_ocr_stride()
        jpeg_params = _frame_jpeg_write_params()
        base_payload: Dict[str, Any] = {
            "video_path": video_source,
            "simple_dir": str(simple_dir.resolve()),
            "job_id": job_id,
            "frame_rate": frame_rate,
            "total_frames": total_frames,
            "fps_raw": fps_raw,
            "ocr_stride": ocr_stride,
            "jpeg_params": jpeg_params,
        }

        # Phase 1 — establish OCR anchor (same semantics as single-process extract_frames)
        e0, e1 = ranges[0]
        first = _mp_extract_chunk(
            {
                **base_payload,
                "e_start": e0,
                "e_end": e1,
                "run_ocr": True,
                "passed_anchor_iso": None,
                "passed_anchor_idx": None,
            }
        )
        all_frames: list = list(first["frames"])
        anchor_iso = first.get("anchor_exam_dt")
        anchor_idx = first.get("anchor_extract_idx")
        done = len(all_frames)
        if progress_callback:
            try:
                progress_callback(min(done, expected_extracted), expected_extracted)
            except Exception as e:
                logger.warning("Progress callback error (parallel extract): %s", e)

        if len(ranges) == 1:
            return sorted(all_frames, key=lambda x: x["frame_number"])

        # Phase 2 — remaining extract-index ranges in parallel
        max_pool = max(1, min(len(ranges) - 1, (os.cpu_count() or 4)))
        with ProcessPoolExecutor(max_workers=max_pool) as pool:
            futs = []
            for e_start, e_end in ranges[1:]:
                payload = {
                    **base_payload,
                    "e_start": e_start,
                    "e_end": e_end,
                    "run_ocr": False,
                    "passed_anchor_iso": anchor_iso,
                    "passed_anchor_idx": anchor_idx,
                }
                futs.append(pool.submit(_mp_extract_chunk, payload))
            for fut in as_completed(futs):
                block = fut.result()
                all_frames.extend(block["frames"])
                done += len(block["frames"])
                if progress_callback:
                    try:
                        progress_callback(min(done, expected_extracted), expected_extracted)
                    except Exception as e:
                        logger.warning("Progress callback error (parallel extract): %s", e)

        all_frames.sort(key=lambda x: x["frame_number"])
        logger.info(
            "Parallel extract: %s frames across %s workers (expected ~%s)",
            len(all_frames),
            num_workers,
            expected_extracted,
        )
        return all_frames

    async def process_live_stream(self, stream_url: str, duration_seconds: int = 3600,
                                  callback=None) -> Dict[str, Any]:
        """
        Process live CCTV stream in real-time.
        Step 1 & 3 of UC-07: Connect to live CCTV and process in real-time
        
        Args:
            stream_url: CCTV camera stream URL (RTSP, HTTP, etc.)
            duration_seconds: How long to monitor (default: 1 hour)
            callback: Async function to call with each frame
            
        Returns:
            Processing statistics
        """
        cap = cv2.VideoCapture(stream_url)
        
        if not cap.isOpened():
            return {
                "success": False,
                "error": "Cannot connect to live stream",
                "stream_url": stream_url
            }
        
        start_time = datetime.utcnow()
        frame_count = 0
        processed_count = 0
        
        try:
            while (datetime.utcnow() - start_time).seconds < duration_seconds:
                ret, frame = cap.read()
                
                if not ret:
                    logger.warning("Failed to read frame from live stream")
                    await asyncio.sleep(0.1)
                    continue
                
                frame_count += 1
                
                # Process every Nth frame to optimize performance
                if frame_count % 30 == 0:  # Process 1 frame per second at 30fps
                    if callback:
                        try:
                            _ts = parse_exam_timestamp_from_frame(frame) or datetime.utcnow()
                        except Exception:
                            _ts = datetime.utcnow()
                        await callback(frame, frame_count, _ts)
                    processed_count += 1
                
                # Small delay to prevent overwhelming the system
                await asyncio.sleep(0.001)
                
        except Exception as e:
            logger.error(f"Error processing live stream: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "frames_captured": frame_count,
                "frames_processed": processed_count
            }
        finally:
            cap.release()
        
        return {
            "success": True,
            "frames_captured": frame_count,
            "frames_processed": processed_count,
            "duration": (datetime.utcnow() - start_time).seconds
        }
    
    def process_recorded_video(self, video_path: str, job_id: str,
                              progress_callback=None, room_id: Optional[str] = None,
                              db_session=None) -> Dict[str, Any]:
        """
        Process uploaded exam recording in batch mode.
        Step 1 & 3 of UC-07: Process uploaded recordings in batch
        
        Args:
            video_path: Path to uploaded video file
            job_id: Processing job identifier
            progress_callback: Function to update progress (called during extraction)
            room_id: Room UUID to get seating plan (optional)
            db_session: Database session to query room info (optional)
            
        Returns:
            Processing results
        """
        logger.info(f"[process_recorded_video] Starting validation for: {video_path}")
        logger.info(f"[process_recorded_video] File exists: {os.path.exists(video_path)}")
        logger.info(f"[process_recorded_video] Absolute path: {os.path.abspath(video_path)}")
        
        validation = self.validate_video_input(video_path, 'recorded')
        
        logger.info(f"[process_recorded_video] Validation result: {validation}")
        
        if not validation['valid']:
            logger.error(f"[process_recorded_video] Validation failed: {validation.get('error')}")
            return {
                "success": False,
                "error": validation.get('error', 'Invalid video'),
                "video_path": video_path
            }
        
        total_frames = validation['frame_count']
        fps = validation['fps']
        
        logger.info(f"Processing recorded video: {video_path}")
        logger.info(f"Total frames: {total_frames}, FPS: {fps}")

        frame_extraction_rate, eff_hz = _recorded_sampling_stride(fps)
        logger.info(
            "Recorded frame stride=%s (~%.3f extracts/s of video); "
            "tune RECORDED_TARGET_SAMPLE_HZ (default 1.0)",
            frame_extraction_rate,
            eff_hz,
        )
        
        # Calculate expected extracted frames
        expected_extracted = (total_frames // frame_extraction_rate) + (1 if total_frames % frame_extraction_rate > 0 else 0)
        
        # Notify callback of expected extracted frames before extraction starts
        if progress_callback:
            try:
                progress_callback(0, expected_extracted)
            except Exception as e:
                logger.warning(f"Progress callback error at start: {e}")

        mpw = _video_extract_mp_workers()
        cpu_n = os.cpu_count() or 4
        if mpw > 1:
            mpw = min(mpw, cpu_n, max(1, expected_extracted))
        if mpw > 1 and expected_extracted >= mpw:
            logger.info(
                "Using multiprocessing frame extraction (%s workers, VIDEO_EXTRACT_MP_WORKERS)",
                mpw,
            )
            frames = self.extract_frames_parallel(
                video_path,
                frame_extraction_rate,
                job_id,
                progress_callback,
                total_frames,
                mpw,
                room_id=room_id,
                db_session=db_session,
            )
        else:
            frames = self.extract_frames(
                video_path,
                frame_extraction_rate,
                job_id,
                progress_callback,
                room_id=room_id,
                db_session=db_session,
            )
        
        # Final progress update
        if progress_callback:
            try:
                progress_callback(len(frames), expected_extracted)
            except Exception as e:
                logger.warning(f"Progress callback error at end: {e}")
        
        # Load seat map for bbox-to-student mapping (same dimensions as video frames)
        frame_width = validation.get('width', 1920)
        frame_height = validation.get('height', 1080)
        seat_map = self.get_seat_map_for_room(room_id, frame_width, frame_height, db_session)
        
        return {
            "success": True,
            "total_frames": total_frames,
            "extracted_frames": len(frames),
            "fps": fps,
            "duration": validation['duration'],
            "frames_info": frames,
            "seat_map": seat_map,
            "frame_width": frame_width,
            "frame_height": frame_height,
        }
    
    def save_uploaded_video(self, file_content: bytes, filename: str, 
                           exam_id: str, room_id: str) -> str:
        """
        Save uploaded video file with organized structure.
        
        Args:
            file_content: Video file bytes
            filename: Original filename
            exam_id: Exam identifier
            room_id: Room identifier
            
        Returns:
            Path to saved video file
        """
        # Create organized directory structure
        exam_dir = self.upload_dir / exam_id / room_id
        exam_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate unique filename
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        file_extension = Path(filename).suffix
        new_filename = f"exam_footage_{timestamp}{file_extension}"
        
        file_path = exam_dir / new_filename
        
        # Save file
        with open(file_path, 'wb') as f:
            f.write(file_content)
        
        # Return absolute path to avoid OpenCV path resolution issues
        absolute_path = str(file_path.resolve())
        logger.info(f"Saved video to: {absolute_path}")
        return absolute_path
    
    def get_stream_info(self, source: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a video stream or file.
        
        Args:
            source: Video source (file path or stream URL)
            
        Returns:
            Dictionary with stream information or None
        """
        cap = cv2.VideoCapture(source)
        
        if not cap.isOpened():
            return None
        
        info = {
            "fps": cap.get(cv2.CAP_PROP_FPS),
            "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "codec": int(cap.get(cv2.CAP_PROP_FOURCC))
        }
        
        cap.release()
        return info

