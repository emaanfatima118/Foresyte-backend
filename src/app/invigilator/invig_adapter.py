"""
Invigilator Frame Adapter
=========================
Stateful per-stream adapter that wraps the invigilator detection pipeline
(YOLO + ByteTrack + MediaPipe Pose + phone detector) and returns structured
behaviour dicts compatible with VideoProcessor.

Usage
-----
    adapter = InvigFrameAdapter()            # raises FileNotFoundError if model absent
    behaviors = adapter.process_frame(frame, frame_number, timestamp)
    # returns [] most frames; non-empty only when an alert threshold is crossed

The adapter is intentionally stateful — create one instance per video stream
so tracker IDs and per-invigilator counters stay consistent across frames.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any

import numpy as np  # noqa: F401 — used in type hints (ndarray)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Alert-type → default severity mapping
# ---------------------------------------------------------------------------
_ALERT_SEVERITY: dict[str, str] = {
    "PHONE_USE":          "high",
    "IDLE":               "medium",
    "SITTING_TOO_LONG":   "medium",
    "OUT_OF_CLASSROOM":   "high",
}

# After this many repeated alerts the severity escalates to "critical"
_ESCALATE_AT = 3

# Minimum *processed* frames between two alerts of the same type (cooldown)
_COOLDOWN_FRAMES = 10

# ---------------------------------------------------------------------------
#  Sparse-frame sampling (video pipeline extracts ~1 fps, not 30 fps)
#  Original invig_monitor.py thresholds assume dense frames; at ~1 fps,
#  idle/sitting counters need fewer steps to be reachable. Override via env.
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _severity_from_alert_key(alert_key: str) -> str:
    """Map PHONE_USE_123, IDLE_5, OUT_OF_CLASSROOM, … to a default severity."""
    if alert_key.startswith("PHONE"):
        return _ALERT_SEVERITY["PHONE_USE"]
    if alert_key.startswith("IDLE"):
        return _ALERT_SEVERITY["IDLE"]
    if alert_key.startswith("SITTING"):
        return _ALERT_SEVERITY["SITTING_TOO_LONG"]
    if alert_key.startswith("OUT"):
        return _ALERT_SEVERITY["OUT_OF_CLASSROOM"]
    return "medium"

# ---------------------------------------------------------------------------
#  Lightweight re-exports of constants / classes from invig_monitor.py
# ---------------------------------------------------------------------------
def _load_monitor_components():
    """
    Import the heavy components from invig_monitor lazily so that modules
    that merely import this file don't trigger YOLO / MediaPipe loading.
    """
    from app.invigilator.invig_monitor import (  # noqa: PLC0415
        ActivityClassifier,
        PhoneDetector,
        InvigState,
        INVIG_PT,
        CONF_INVIG,
        PHONE_USE_THRESH,
        IDLE_THRESH,
        SITTING_THRESH,
        ABSENT_THRESH,
        MOVE_THRESH,
        OUT_OF_CLASSROOM,
    )
    return (
        ActivityClassifier,
        PhoneDetector,
        InvigState,
        INVIG_PT,
        CONF_INVIG,
        PHONE_USE_THRESH,
        IDLE_THRESH,
        SITTING_THRESH,
        ABSENT_THRESH,
        MOVE_THRESH,
        OUT_OF_CLASSROOM,
    )


# ---------------------------------------------------------------------------
#  InvigFrameAdapter
# ---------------------------------------------------------------------------
class InvigFrameAdapter:
    """
    Stateful per-stream wrapper around the invigilator YOLO+MediaPipe pipeline.

    Only call process_frame() on frames that belong to the *same* video stream
    so that tracker IDs and per-person counters remain consistent.

    Parameters
    ----------
    model_path : str | None
        Path to the custom ``invig_best.pt`` YOLO weights.
        Defaults to the bundled model path from ``invig_monitor.INVIG_PT``.

    Raises
    ------
    FileNotFoundError
        If the YOLO model weights file is not found at the given path.
    ImportError
        If required dependencies (ultralytics, mediapipe) are not installed.
    """

    def __init__(self, model_path: str | None = None) -> None:
        from ultralytics import YOLO  # noqa: PLC0415

        (
            ActivityClassifier,
            PhoneDetector,
            InvigState,
            INVIG_PT,
            CONF_INVIG,
            PHONE_USE_THRESH,
            IDLE_THRESH,
            SITTING_THRESH,
            ABSENT_THRESH,
            MOVE_THRESH,
            OUT_OF_CLASSROOM,
        ) = _load_monitor_components()

        _pt = model_path or INVIG_PT
        if not os.path.exists(_pt):
            raise FileNotFoundError(
                f"Invigilator model not found: {_pt!r}. "
                "Place invig_best.pt in app/invigilator/models/ to enable "
                "invigilator activity detection."
            )

        log.info("Loading invigilator YOLO model from %s …", _pt)
        self._det = YOLO(_pt)
        self._phone_det = PhoneDetector(YOLO("yolov8n.pt"))
        self._act_clf = ActivityClassifier()
        self._InvigState = InvigState

        # Per-tracker state (keyed by ByteTrack ID)
        self._states: dict[int, Any] = {}

        # Out-of-classroom tracking
        self._ooc_frames = 0
        self._ooc_alerted = False

        # Alert deduplication (uses *processed* frame index, not video frame index)
        self._alert_counts: dict[str, int] = defaultdict(int)
        self._alert_cooldown: dict[str, int] = defaultdict(int)

        self._processed_seq = 0  # 0,1,2,… for each analysed frame in this stream

        # Detection config — env overrides for sparse sampling (see module docstring)
        self._CONF_INVIG = CONF_INVIG
        self._PHONE_USE_THRESH = _env_int("INVIG_PHONE_FRAMES", min(PHONE_USE_THRESH, 3))
        self._IDLE_THRESH = _env_int("INVIG_IDLE_FRAMES", min(IDLE_THRESH, 15))
        self._SITTING_THRESH = _env_int("INVIG_SITTING_FRAMES", min(SITTING_THRESH, 12))
        self._ABSENT_THRESH = _env_int("INVIG_ABSENT_FRAMES", ABSENT_THRESH)
        self._MOVE_THRESH = MOVE_THRESH
        self._OUT_OF_CLASSROOM = OUT_OF_CLASSROOM

        # Emit a low-severity "routine" observation every N processed frames when
        # at least one invigilator is visible (so the DB is not empty on runs
        # where no alert thresholds fire).  Set INVIG_ROUTINE_INTERVAL=0 to disable.
        self._routine_interval = _env_int("INVIG_ROUTINE_INTERVAL", 5)

        log.info(
            "InvigFrameAdapter ready (model=%s, phone≥%d idle≥%d sit≥%d absent≥%d routine_every=%d)",
            _pt,
            self._PHONE_USE_THRESH,
            self._IDLE_THRESH,
            self._SITTING_THRESH,
            self._ABSENT_THRESH,
            self._routine_interval,
        )

    # ------------------------------------------------------------------
    #  Internal helpers
    # ------------------------------------------------------------------

    def _try_alert(
        self,
        alert_key: str,
        behavior_type: str,
        tracker_id: int,
        details: str,
        confidence: float,
        bbox: tuple[int, int, int, int] | None,
    ) -> dict | None:
        """
        Return a behaviour dict if the alert should fire now (cooldown expired),
        else return None.
        """
        if self._frame_no - self._alert_cooldown[alert_key] < _COOLDOWN_FRAMES:
            return None

        self._alert_cooldown[alert_key] = self._frame_no
        self._alert_counts[alert_key] += 1
        count = self._alert_counts[alert_key]
        base_sev = _severity_from_alert_key(alert_key)
        severity = "critical" if count >= _ESCALATE_AT else base_sev
        return {
            "behavior_type":  behavior_type,
            "severity":       severity,
            "confidence":     round(confidence, 4),
            "details":        f"{details} (alert #{count}, tracker_id={tracker_id})",
            "tracker_id":     tracker_id,
            "bbox":           list(bbox) if bbox else None,
            "alert_count":    count,
        }

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def process_frame(
        self,
        frame: np.ndarray,
        frame_number: int,
        timestamp,
    ) -> list[dict]:
        """
        Run invigilator detection on a single BGR frame.

        Returns behaviour dicts for **alert events** plus, every
        ``INVIG_ROUTINE_INTERVAL`` processed frames, **routine observations**
        (severity ``low``) whenever an invigilator is visible — so sparse
        sampling still produces database rows when the model detects someone.

        Parameters
        ----------
        frame       : BGR numpy array (as returned by cv2.imread / VideoCapture)
        frame_number: Sequential frame index within the stream
        timestamp   : datetime or ISO string (used for logging only)

        Returns
        -------
        List of dicts with keys:
            behavior_type, severity, confidence, details,
            tracker_id, bbox, alert_count
        """
        self._processed_seq += 1
        seq = self._processed_seq
        # Cooldown / alert logic uses consecutive *processed* frames (1:1 with
        # extracted frames in the video pipeline), not sparse video frame indices.
        self._frame_no = seq
        behaviors: list[dict] = []

        if frame is None or frame.size == 0:
            return behaviors

        H, W = frame.shape[:2]
        n_invigs = 0

        res = None
        try:
            res = self._det.track(
                frame,
                conf=self._CONF_INVIG,
                persist=True,
                tracker="bytetrack.yaml",
                verbose=False,
            )[0]
        except Exception as exc:
            log.warning(
                "YOLO track() failed on frame %d (%s); retrying with predict() (no ByteTrack)",
                frame_number,
                exc,
            )
            try:
                res = self._det.predict(
                    frame,
                    conf=self._CONF_INVIG,
                    verbose=False,
                )[0]
            except Exception as exc2:
                log.warning(
                    "YOLO predict() also failed on frame %d: %s",
                    frame_number,
                    exc2,
                )
                return behaviors

        if res.boxes is not None and len(res.boxes):
            n_invigs = len(res.boxes)
            self._ooc_frames = 0
            self._ooc_alerted = False

            for i, box in enumerate(res.boxes):
                try:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    det_conf = float(box.conf[0])
                    tid = int(box.id[0]) if box.id is not None else i
                except Exception:
                    continue

                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2
                bw = max(x2 - x1, 1)
                bh = max(y2 - y1, 1)

                if tid not in self._states:
                    self._states[tid] = self._InvigState(tid)
                st = self._states[tid]
                st.see(cx, cy, seq)

                # Crop with slight padding for pose estimation
                pad_x = int(bw * 0.25)
                pad_y = int(bh * 0.15)
                crop = frame[
                    max(0, y1 - pad_y) : min(H, y2 + pad_y),
                    max(0, x1 - pad_x) : min(W, x2 + pad_x),
                ]

                try:
                    (activity, act_conf), _ = self._act_clf.classify(
                        crop, bbox_w=bw, bbox_h=bh,
                        prev_cx=st.prev_cx, curr_cx=cx,
                    )
                except Exception:
                    activity, act_conf = "Unknown", 0.5
                st.set_activity(activity)

                try:
                    phone_now = self._phone_det.detect(frame, x1, y1, x2, y2)
                except Exception:
                    phone_now = False
                st.set_phone(phone_now)

                bbox = (x1, y1, x2, y2)
                pred_activity = "Phone Use" if phone_now else activity

                # Periodic routine rows so invigilator_activities is populated even
                # when no alert thresholds are crossed (common at ~1 fps sampling).
                if self._routine_interval > 0 and (
                    seq == 1 or seq % self._routine_interval == 0
                ):
                    behaviors.append({
                        "behavior_type": pred_activity,
                        "severity": "low",
                        "confidence": round(float(act_conf), 4),
                        "details": (
                            f"Routine observation (video frame {frame_number}, "
                            f"processed index {seq})"
                        ),
                        "tracker_id": tid,
                        "bbox": list(bbox),
                        "is_routine": True,
                    })

                # ── Phone-use alert ────────────────────────────
                if st.phone_frames >= self._PHONE_USE_THRESH and not st.phone_alerted:
                    b = self._try_alert(
                        f"PHONE_USE_{tid}",
                        "Phone Use",
                        tid,
                        f"Phone detected for {st.phone_frames} consecutive frames",
                        act_conf,
                        bbox,
                    )
                    if b:
                        behaviors.append(b)
                        st.phone_alerted = True

                # ── Idle alert ─────────────────────────────────
                if (
                    st.idle_frames >= self._IDLE_THRESH
                    and not st.idle_alerted
                    and activity not in {"Walking"}
                ):
                    b = self._try_alert(
                        f"IDLE_{tid}",
                        "Idle",
                        tid,
                        f"Invigilator stationary for {st.idle_frames} frames",
                        act_conf,
                        bbox,
                    )
                    if b:
                        behaviors.append(b)
                        st.idle_alerted = True

                # ── Sitting-too-long alert ─────────────────────
                if st.sitting_frames >= self._SITTING_THRESH and not st.sitting_alerted:
                    b = self._try_alert(
                        f"SITTING_{tid}",
                        "Sitting Too Long",
                        tid,
                        f"Invigilator seated for {st.sitting_frames} consecutive frames",
                        act_conf,
                        bbox,
                    )
                    if b:
                        behaviors.append(b)
                        st.sitting_alerted = True

        # ── Out-of-classroom alert ─────────────────────────────
        if n_invigs == 0:
            self._ooc_frames += 1

        if self._ooc_frames >= self._ABSENT_THRESH and not self._ooc_alerted:
            b = self._try_alert(
                "OUT_OF_CLASSROOM",
                "Out of Classroom",
                0,
                f"No invigilator detected for {self._ooc_frames} consecutive frames",
                1.0,
                None,
            )
            if b:
                behaviors.append(b)
                self._ooc_alerted = True

        return behaviors
