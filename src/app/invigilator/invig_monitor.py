"""
Invigilator Monitoring System — v4  (Prediction CSV export added)
══════════════════════════════════════════════════════════════════

NEW IN v4 vs v3
───────────────
1. PREDICTION CSV  — predictions_TIMESTAMP.csv:
      • If ≥1 invigilator is detected: one row per person (as before).
      • If nobody is detected: one row with invigilator_id=0 and
        pred_activity="Out of Classroom" (matches label.py).
      • Phone is not a separate column: pred_activity is "Phone Use" when the
        phone detector fires (same column as Standing / Walking / …).

   This CSV can be directly compared against ground_truth.csv by
   join_and_evaluate.py without re-running inference.

2. ASPECT RATIO LOGGING  — bbox_aspect column helps you tune
   BBOX_STANDING_MIN_ASPECT / BBOX_SITTING_MAX_ASPECT thresholds.

3. MINOR FIXES
   • PhoneDetector.detect() returns (bool, conf) for internal use.
   • InvigState tracks sitting_frames separately from act_frame_count so
     resuming "Sitting" after a brief Standing doesn't reset the counter.
"""

import os
import cv2
import csv
import time
import glob
import logging
import urllib.request
import numpy as np
import pandas as pd
from datetime import datetime
from collections import deque, defaultdict
from ultralytics import YOLO
import mediapipe as mp

_log = logging.getLogger(__name__)

# ╔══════════════════════════════════════════════════════════════╗
#  CONFIGURATION
# ╚══════════════════════════════════════════════════════════════╝

BASE       = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE, "models")
FRAMES_DIR = os.path.join(BASE, "frames")
OUTPUT_DIR = os.path.join(BASE, "output")
LOGS_DIR   = os.path.join(BASE, "logs")

for d in [MODELS_DIR, FRAMES_DIR, OUTPUT_DIR, LOGS_DIR]:
    os.makedirs(d, exist_ok=True)

INVIG_PT = os.path.join(MODELS_DIR, "invig_best.pt")

# ── Detection thresholds ──────────────────────────────────────
CONF_INVIG        = 0.26
ABSENT_THRESH     = 3
PHONE_USE_THRESH  = 5
IDLE_THRESH       = 45
SITTING_THRESH    = 30
OUTPUT_VIDEO_FPS  = 1
SHOW_SKELETON     = True
ESCALATE_COUNT    = 3

# ── Bounding-box aspect ratio thresholds ─────────────────────
BBOX_STANDING_MIN_ASPECT = 1.7
BBOX_SITTING_MAX_ASPECT  = 1.4

# Movement threshold (pixels)
MOVE_THRESH = 12

# Colors (BGR)
C_BOX   = (0, 165, 255)
C_PHONE = (0,   0, 255)
C_OK    = (0, 200,   0)
C_WARN  = (0, 200, 255)
C_ALERT = (0,   0, 255)
C_BG    = (20,  20,  20)

ALERT_ACTIVITIES = {"Sitting", "Phone Use"}

# Prediction CSV + label.py ground truth (must match exactly)
OUT_OF_CLASSROOM = "Out of Classroom"

# MediaPipe 0.10.30+ Windows wheels only ship the Tasks API (`mediapipe.tasks`), not
# `mediapipe.solutions`. We use PoseLandmarker with a one-time model download.
POSE_LANDMARKER_LITE_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
)


def _ensure_pose_landmarker_task_file() -> str:
    path = os.path.join(MODELS_DIR, "pose_landmarker_lite.task")
    if os.path.isfile(path) and os.path.getsize(path) > 100_000:
        return path
    os.makedirs(MODELS_DIR, exist_ok=True)
    _log.info("Downloading MediaPipe pose_landmarker_lite.task (one-time) …")
    urllib.request.urlretrieve(POSE_LANDMARKER_LITE_URL, path)
    return path


# ╔══════════════════════════════════════════════════════════════╗
#  Activity Classifier
# ╚══════════════════════════════════════════════════════════════╝

class ActivityClassifier:
    VIS = 0.30

    def __init__(self):
        # Prefer legacy solutions API; else Tasks API (current PyPI Windows wheels).
        self.pose = None
        self.mp_pose = None
        self.mp_draw = None
        self._pose_landmarker = None
        self._pose_backend = None  # "legacy" | "tasks" | None

        if hasattr(mp, "solutions"):
            try:
                mp_pose = mp.solutions.pose
                self.pose = mp_pose.Pose(
                    static_image_mode=True,
                    model_complexity=1,
                    enable_segmentation=False,
                    min_detection_confidence=0.25,
                    min_tracking_confidence=0.25,
                )
                self.mp_pose = mp_pose
                self.mp_draw = mp.solutions.drawing_utils
                self._pose_backend = "legacy"
                _log.info("MediaPipe pose: legacy solutions API")
            except Exception as exc:
                _log.warning("MediaPipe legacy Pose() failed: %s", exc)

        if self._pose_backend is None:
            try:
                from mediapipe.tasks.python import vision
                from mediapipe.tasks.python.core import base_options as base_options_module

                task_path = _ensure_pose_landmarker_task_file()
                options = vision.PoseLandmarkerOptions(
                    base_options=base_options_module.BaseOptions(
                        model_asset_path=task_path
                    ),
                    running_mode=vision.RunningMode.IMAGE,
                    min_pose_detection_confidence=0.25,
                    min_pose_presence_confidence=0.25,
                    min_tracking_confidence=0.25,
                )
                self._pose_landmarker = vision.PoseLandmarker.create_from_options(
                    options
                )
                self._pose_backend = "tasks"
                _log.info("MediaPipe pose: Tasks API (PoseLandmarker)")
            except Exception as exc:
                _log.warning(
                    "MediaPipe Tasks pose unavailable (%s). Using bbox-only heuristics.",
                    exc,
                )

    def _upscale(self, img, min_side=280):
        h, w = img.shape[:2]
        s = max(1.0, min_side / max(h, w, 1))
        if s > 1.0:
            img = cv2.resize(img, (int(w*s), int(h*s)),
                             interpolation=cv2.INTER_LINEAR)
        return img

    def _lm(self, lms, idx):
        l = lms[idx]
        return l.x, l.y, l.visibility

    def classify(self, crop_bgr, bbox_w, bbox_h,
                 prev_cx=None, curr_cx=None):
        debug_img = crop_bgr.copy() if crop_bgr is not None else None
        moving = (prev_cx is not None and curr_cx is not None
                  and abs(curr_cx - prev_cx) > MOVE_THRESH)
        aspect = bbox_h / max(bbox_w, 1)

        if moving and aspect >= BBOX_STANDING_MIN_ASPECT:
            return ("Walking", 0.88), debug_img

        if aspect >= BBOX_STANDING_MIN_ASPECT:
            mp_result = self._run_mediapipe(crop_bgr)
            if mp_result is not None:
                (label, conf), debug_img = mp_result
                if label == "Sitting" and aspect >= BBOX_STANDING_MIN_ASPECT:
                    return ("Standing", 0.80), debug_img
                return (label, conf), debug_img
            return ("Standing", 0.82), debug_img

        if aspect < BBOX_SITTING_MAX_ASPECT:
            return ("Sitting", 0.85), debug_img

        mp_result = self._run_mediapipe(crop_bgr)
        if mp_result is not None:
            (label, conf), debug_img = mp_result
            return (label, conf), debug_img

        return ("Standing" if not moving else "Walking", 0.50), debug_img

    def _run_mediapipe(self, crop_bgr):
        if self._pose_backend is None:
            return None
        if crop_bgr is None or crop_bgr.size == 0:
            return None
        if self._pose_backend == "legacy":
            return self._run_mediapipe_legacy(crop_bgr)
        if self._pose_backend == "tasks":
            return self._run_mediapipe_tasks(crop_bgr)
        return None

    def _run_mediapipe_legacy(self, crop_bgr):
        img = self._upscale(crop_bgr)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        result = self.pose.process(rgb)
        debug = img.copy()

        if not result.pose_landmarks:
            return None

        lms = result.pose_landmarks.landmark
        self.mp_draw.draw_landmarks(
            debug, result.pose_landmarks,
            self.mp_pose.POSE_CONNECTIONS,
            self.mp_draw.DrawingSpec((0, 255, 0), 1, 2),
            self.mp_draw.DrawingSpec((255, 255, 0), 1),
        )

        def lm(i):
            return self._lm(lms, i)

        return self._activity_from_pose_lm(lm, debug)

    def _run_mediapipe_tasks(self, crop_bgr):
        img = self._upscale(crop_bgr)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self._pose_landmarker.detect(mp_image)
        debug = img.copy()

        if not result.pose_landmarks:
            return None

        lms = result.pose_landmarks[0]

        def lm(i):
            p = lms[i]
            vis = p.visibility if p.visibility is not None else 0.0
            return p.x, p.y, float(vis)

        return self._activity_from_pose_lm(lm, debug)

    def _activity_from_pose_lm(self, lm, debug):
        """Shared BlazePose topology (indices 11,12 shoulders; 23–26 hips/knees)."""
        _, ls_y, ls_v = lm(11)
        _, rs_y, rs_v = lm(12)
        _, lh_y, lh_v = lm(23)
        _, rh_y, rh_v = lm(24)
        _, lk_y, lk_v = lm(25)
        _, rk_y, rk_v = lm(26)

        sh_vis = ls_v > self.VIS or rs_v > self.VIS
        hip_vis = lh_v > self.VIS or rh_v > self.VIS
        knee_vis = lk_v > self.VIS or rk_v > self.VIS

        if not sh_vis:
            return None

        avg_sh_y = ((ls_y if ls_v > self.VIS else rs_y) +
                    (rs_y if rs_v > self.VIS else ls_y)) / 2

        if knee_vis and hip_vis:
            avg_hip_y = ((lh_y if lh_v > self.VIS else rh_y) +
                         (rh_y if rh_v > self.VIS else lh_y)) / 2
            avg_knee_y = ((lk_y if lk_v > self.VIS else rk_y) +
                          (rk_y if rk_v > self.VIS else lk_y)) / 2
            if avg_knee_y < avg_hip_y - 0.05:
                return ("Sitting", 0.82), debug

        if hip_vis:
            avg_hip_y = ((lh_y if lh_v > self.VIS else rh_y) +
                         (rh_y if rh_v > self.VIS else lh_y)) / 2
            dist = avg_hip_y - avg_sh_y
            if dist > 0.30:
                return ("Standing", 0.80), debug
            if dist < 0.12:
                return ("Sitting", 0.78), debug

        if avg_sh_y > 0.65:
            return ("Sitting", 0.65), debug

        return ("Standing", 0.65), debug


# ╔══════════════════════════════════════════════════════════════╗
#  Phone Detector
# ╚══════════════════════════════════════════════════════════════╝

CONF_PHONE       = 0.22
PHONE_MIN_ASPECT = 0.45
PHONE_MAX_ASPECT = 4.5
PHONE_MIN_AREA   = 300
PHONE_BRIGHT_MAX = 155
PHONE_EDGE_MIN   = 0.04


class PhoneDetector:
    COCO_PHONE = 67

    def __init__(self, model):
        self.model = model

    @staticmethod
    def _valid_shape(bx1, by1, bx2, by2):
        dw   = max(bx2 - bx1, 1)
        dh   = max(by2 - by1, 1)
        ar   = dh / dw
        area = dw * dh
        return (PHONE_MIN_ASPECT <= ar <= PHONE_MAX_ASPECT
                and area >= PHONE_MIN_AREA)

    @staticmethod
    def _looks_like_phone(region_bgr):
        if region_bgr is None or region_bgr.size == 0:
            return False
        h, w = region_bgr.shape[:2]
        if h < 8 or w < 8:
            return False
        gray        = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2GRAY)
        mean_bright = float(np.mean(gray))
        if mean_bright > PHONE_BRIGHT_MAX:
            return False
        edges     = cv2.Canny(gray, 50, 150)
        edge_frac = float(np.count_nonzero(edges)) / max(gray.size, 1)
        return edge_frac >= PHONE_EDGE_MIN

    def _run(self, crop_full, scale_factor):
        if crop_full is None or crop_full.size == 0 or min(crop_full.shape[:2]) < 8:
            return False
        h, w = crop_full.shape[:2]
        uh   = int(h * scale_factor)
        uw   = int(w * scale_factor)
        up   = cv2.resize(crop_full, (uw, uh), interpolation=cv2.INTER_LINEAR)
        pr   = self.model(up, conf=CONF_PHONE,
                          classes=[self.COCO_PHONE], verbose=False)[0]
        if pr.boxes is None or len(pr.boxes) == 0:
            return False
        for box in pr.boxes:
            bx1, by1, bx2, by2 = map(int, box.xyxy[0])
            if not self._valid_shape(bx1, by1, bx2, by2):
                continue
            ox1 = max(int(bx1 / scale_factor), 0)
            oy1 = max(int(by1 / scale_factor), 0)
            ox2 = min(int(bx2 / scale_factor), w)
            oy2 = min(int(by2 / scale_factor), h)
            if self._looks_like_phone(crop_full[oy1:oy2, ox1:ox2]):
                return True
        return False

    def detect(self, frame, x1, y1, x2, y2):
        """Returns (bool phone_detected, float approx_conf)."""
        H, W = frame.shape[:2]
        bw   = x2 - x1
        bh   = y2 - y1
        pad  = int(bw * 0.60)
        ex1  = max(0, x1 - pad)
        ex2  = min(W, x2 + pad)
        ey1  = max(0, y1)
        ey2  = min(H, y2)
        ebh  = ey2 - ey1
        zone_h = max(ebh * 0.5, 1)
        scale  = max(1.0, 220 / zone_h)

        zones = [
            (ey1,                   ey1 + int(ebh * 0.50)),
            (ey1 + int(ebh * 0.25), ey1 + int(ebh * 0.75)),
            (ey1 + int(ebh * 0.50), ey2),
        ]
        for zy1, zy2 in zones:
            if zy2 - zy1 < 8:
                continue
            if self._run(frame[zy1:zy2, ex1:ex2], scale):
                return True

        cx = (ex1 + ex2) // 2
        for hx1, hx2 in [(ex1, cx), (cx, ex2)]:
            if self._run(frame[ey1:ey2, hx1:hx2], scale):
                return True

        return False


# ╔══════════════════════════════════════════════════════════════╗
#  Alert Logger
# ╚══════════════════════════════════════════════════════════════╝

class AlertLogger:
    def __init__(self, path):
        self.path         = path
        self.cooldown     = defaultdict(int)
        self.alert_counts = defaultdict(int)
        self.cd_frames    = 10
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(
                ["timestamp", "alert_type", "severity",
                 "detail", "frame", "total_count"]
            )

    def log(self, atype, detail, frame_no):
        if frame_no - self.cooldown[atype] < self.cd_frames:
            return None
        self.cooldown[atype] = frame_no
        self.alert_counts[atype] += 1
        count    = self.alert_counts[atype]
        severity = "CRITICAL" if count >= ESCALATE_COUNT else "WARNING"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow(
                [ts, atype, severity, detail, frame_no, count]
            )
        icon = "🔴" if severity == "CRITICAL" else "🚨"
        print(f"  {icon} [{ts}] {severity} {atype}: {detail} (x{count})")
        return severity


# ╔══════════════════════════════════════════════════════════════╗
#  Prediction CSV Writer  ← NEW in v4
# ╚══════════════════════════════════════════════════════════════╝

class PredictionWriter:
    """
    Writes one row per detected invigilator per frame so the output can
    be compared directly against ground_truth.csv by evaluate_accuracy.py.
    """
    # Phone is not a separate column — use pred_activity == "Phone Use"
    FIELDS = [
        "frame_filename",
        "invigilator_id",
        "pred_activity",
        "pred_conf",
        "bbox_aspect",       # bbox_h / bbox_w  — useful for threshold tuning
        "idle_frames",
        "det_conf",          # YOLO detection confidence
        "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2",
        "frame_no",
        "timestamp",
    ]

    def __init__(self, path):
        self.path = path
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(self.FIELDS)
        print(f"  📝 Prediction log → {path}")

    def write(self, fname, tid, activity, act_conf, bbox_aspect,
              idle_frames, det_conf,
              x1, y1, x2, y2, frame_no):
        row = [
            fname, tid, activity, round(act_conf, 4),
            round(bbox_aspect, 3),
            idle_frames, round(det_conf, 4),
            x1, y1, x2, y2, frame_no,
            datetime.now().strftime("%H:%M:%S.%f")[:12],
        ]
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow(row)


# ╔══════════════════════════════════════════════════════════════╗
#  Drawing helpers
# ╚══════════════════════════════════════════════════════════════╝

def put_label(img, text, x, y, color=C_BOX, scale=0.55):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    cv2.rectangle(img, (x, y - th - 7), (x + tw + 6, y + 2), C_BG, -1)
    cv2.putText(img, text, (x + 3, y - 2),
                cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def draw_hud(img, n_invigs, fname, alerts):
    lines = [f"Frame:{fname}  Invigs:{n_invigs}"] + alerts[:5]
    cv2.rectangle(img, (0, 0), (600, len(lines) * 26 + 10), C_BG, -1)
    for i, ln in enumerate(lines):
        cv2.putText(img, ln, (8, 22 + i * 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.58,
                    C_ALERT if i > 0 else C_OK, 1, cv2.LINE_AA)


def duration_bar(img, x1, y, x2, dur, thresh):
    fill = int((x2 - x1) * min(dur / max(thresh, 1), 1.0))
    col  = (C_ALERT if dur / thresh > 0.7
            else C_WARN if dur / thresh > 0.4 else C_OK)
    cv2.rectangle(img, (x1, y), (x2, y + 5), (50, 50, 50), -1)
    cv2.rectangle(img, (x1, y), (x1 + fill, y + 5), col, -1)


def paste_thumbnail(img, thumb, x, y):
    h, w   = thumb.shape[:2]
    H, W   = img.shape[:2]
    x2, y2 = min(x + w, W), min(y + h, H)
    if x2 > x and y2 > y:
        img[y:y2, x:x2] = thumb[:y2-y, :x2-x]


# ╔══════════════════════════════════════════════════════════════╗
#  Per-invigilator state tracker
# ╚══════════════════════════════════════════════════════════════╝

class InvigState:
    def __init__(self, tid):
        self.tid              = tid
        self.activity         = None
        self.act_frame_count  = 0
        self.sitting_frames   = 0   # ← NEW: persists across brief non-sitting periods
        self.phone_frames     = 0
        self.idle_frames      = 0
        self.positions        = deque(maxlen=60)
        self.last_seen_frame  = 0
        self.phone_alerted    = False
        self.idle_alerted     = False
        self.sitting_alerted  = False

    def see(self, cx, cy, frame_no):
        self.last_seen_frame = frame_no
        if self.positions:
            px, py = self.positions[-1]
            moved  = ((cx-px)**2 + (cy-py)**2)**0.5 > 20
            if moved:
                self.idle_frames  = 0
                self.idle_alerted = False
            else:
                self.idle_frames += 1
        self.positions.append((cx, cy))

    def set_activity(self, act):
        if act == self.activity:
            self.act_frame_count += 1
        else:
            self.activity        = act
            self.act_frame_count = 1
            if act != "Sitting":
                self.sitting_alerted = False

        # Track cumulative sitting time separately
        if act == "Sitting":
            self.sitting_frames += 1
        else:
            self.sitting_frames = 0

    def set_phone(self, detected):
        if detected:
            self.phone_frames += 1
        else:
            self.phone_frames  = 0
            self.phone_alerted = False

    @property
    def prev_cx(self):
        return self.positions[-2][0] if len(self.positions) >= 2 else None


# ╔══════════════════════════════════════════════════════════════╗
#  Main Monitor
# ╚══════════════════════════════════════════════════════════════╝

class InvigMonitor:
    def __init__(self, log_path, pred_path):
        print("Loading models …")
        if not os.path.exists(INVIG_PT):
            raise FileNotFoundError(
                f"Model not found: {INVIG_PT}\n"
                f"Place invig_best.pt in {MODELS_DIR}"
            )
        self.det          = YOLO(INVIG_PT)
        phone_yolo        = YOLO("yolov8n.pt")
        self.phone_det    = PhoneDetector(phone_yolo)
        self.act_clf      = ActivityClassifier()
        self.logger       = AlertLogger(log_path)
        self.pred_writer  = PredictionWriter(pred_path)   # ← NEW
        self.states       = {}
        self.ooc_frames   = 0
        self.ooc_alerted  = False
        self.frame_no     = 0
        print("✅ Models loaded")

    def process(self, frame, fname):
        img    = frame.copy()
        alerts = []
        H, W   = frame.shape[:2]
        self.frame_no += 1

        res = self.det.track(
            frame, conf=CONF_INVIG, persist=True,
            tracker="bytetrack.yaml", verbose=False,
        )[0]

        n_invigs = 0

        if res.boxes is not None and len(res.boxes):
            n_invigs        = len(res.boxes)
            self.ooc_frames  = 0
            self.ooc_alerted = False

            for i, box in enumerate(res.boxes):
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf  = float(box.conf[0])
                tid   = int(box.id[0]) if box.id is not None else i
                cx    = (x1 + x2) // 2
                cy    = (y1 + y2) // 2
                bw    = x2 - x1
                bh    = y2 - y1
                aspect = bh / max(bw, 1)

                if tid not in self.states:
                    self.states[tid] = InvigState(tid)
                st = self.states[tid]
                st.see(cx, cy, self.frame_no)

                pad_x = int(bw * 0.25)
                pad_y = int(bh * 0.15)
                crop  = frame[max(0, y1-pad_y):min(H, y2+pad_y),
                               max(0, x1-pad_x):min(W, x2+pad_x)]

                (activity, act_conf), debug_crop = self.act_clf.classify(
                    crop,
                    bbox_w=bw, bbox_h=bh,
                    prev_cx=st.prev_cx,
                    curr_cx=cx,
                )
                st.set_activity(activity)

                phone_now = self.phone_det.detect(frame, x1, y1, x2, y2)
                st.set_phone(phone_now)

                # Same column as activity: Phone Use when detector fires
                pred_activity = "Phone Use" if phone_now else activity

                # ── Write prediction row ← NEW ──────────────
                self.pred_writer.write(
                    fname, tid, pred_activity, act_conf, aspect,
                    st.idle_frames, conf,
                    x1, y1, x2, y2, self.frame_no,
                )

                disp_act   = pred_activity
                disp_color = C_ALERT if phone_now else (
                    C_ALERT if activity in ALERT_ACTIVITIES else C_OK
                )

                # ── Alerts ─────────────────────────────────────
                if st.phone_frames >= PHONE_USE_THRESH and not st.phone_alerted:
                    alerts.append(f"📱 INVIG#{tid} PHONE {st.phone_frames}f")
                    self.logger.log("PHONE_USE",
                                    f"ID#{tid} {st.phone_frames} frames",
                                    self.frame_no)
                    st.phone_alerted = True

                if (st.idle_frames >= IDLE_THRESH
                        and not st.idle_alerted
                        and activity not in {"Walking"}):
                    alerts.append(f"💤 INVIG#{tid} IDLE {st.idle_frames}f")
                    self.logger.log("IDLE",
                                    f"ID#{tid} {st.idle_frames} frames",
                                    self.frame_no)
                    st.idle_alerted = True

                # ← use sitting_frames for sustained-sitting alert
                if (st.sitting_frames >= SITTING_THRESH
                        and not st.sitting_alerted):
                    alerts.append(f"🪑 INVIG#{tid} SITTING {st.sitting_frames}f")
                    self.logger.log("SITTING_TOO_LONG",
                                    f"ID#{tid} {st.sitting_frames} frames",
                                    self.frame_no)
                    st.sitting_alerted = True

                # ── Draw ───────────────────────────────────────
                bc = C_PHONE if phone_now else C_BOX
                cv2.rectangle(img, (x1, y1), (x2, y2), bc, 2)
                put_label(img, f"Invig #{tid}  {conf:.2f}", x1, y1, bc)
                put_label(img,
                          f"{disp_act}  {act_conf:.2f}  ar:{aspect:.1f}",
                          x1, y2 + 20, disp_color, 0.50)

                if phone_now:
                    put_label(img, f"📱 {st.phone_frames}f",
                              x1, y2 + 40, C_PHONE, 0.50)

                duration_bar(img, x1, y2 + 48, x2,
                             st.idle_frames, IDLE_THRESH)

                if SHOW_SKELETON and debug_crop is not None:
                    th  = min(130, H // 5)
                    tw  = int(th * debug_crop.shape[1]
                               / max(debug_crop.shape[0], 1))
                    thr = cv2.resize(debug_crop, (tw, th))
                    ox  = W - tw - 10 - i * (tw + 5)
                    paste_thumbnail(img, thr, max(0, ox), 10)
                    cv2.rectangle(img,
                                  (max(0, ox), 10),
                                  (max(0, ox)+tw, 10+th),
                                  (200, 200, 200), 1)
                    put_label(img, f"#{tid} ar:{aspect:.1f}",
                              max(0, ox), 10+th+14, C_BOX, 0.40)

        if n_invigs == 0:
            self.ooc_frames += 1
            # One row per frame when nobody detected (aligns with label.py id=0)
            self.pred_writer.write(
                fname, 0, OUT_OF_CLASSROOM, 1.0, 0.0,
                0, 0.0,
                0, 0, 0, 0, self.frame_no,
            )

        if self.ooc_frames >= ABSENT_THRESH and not self.ooc_alerted:
            alerts.append(f"🚨 NO INVIG — ABSENT {self.ooc_frames}f!")
            self.logger.log("OUT_OF_CLASSROOM",
                            f"{self.ooc_frames} frames without any invigilator",
                            self.frame_no)
            self.ooc_alerted = True

        draw_hud(img, n_invigs, fname, alerts)
        return img


# ╔══════════════════════════════════════════════════════════════╗
#  Entry point
# ╚══════════════════════════════════════════════════════════════╝

def verify_setup():
    frames = sorted(
        glob.glob(os.path.join(FRAMES_DIR, "*.jpg"))
        + glob.glob(os.path.join(FRAMES_DIR, "*.png"))
    )
    ok_model  = os.path.exists(INVIG_PT) and os.path.getsize(INVIG_PT) > 1e6
    print(f"  {'✅' if ok_model  else '❌'} invig_best.pt")
    print(f"  {'✅' if frames    else '❌'} frames: {len(frames)} found")
    if frames:
        s = cv2.imread(frames[0])
        if s is not None:
            print(f"     Resolution : {s.shape[1]}x{s.shape[0]}")
    if not frames or not ok_model:
        raise SystemExit("❌ Fix missing files above before continuing.")
    print("\n✅ All files ready")
    return frames


def main():
    print("=" * 60)
    print("Invigilator Monitoring System  v4  (Prediction CSV export)")
    print("=" * 60)
    print(f"\n📁 {BASE}")

    frames  = verify_setup()
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUT_DIR = os.path.join(OUTPUT_DIR, f"annotated_{ts}")
    LOG     = os.path.join(LOGS_DIR,   f"alerts_{ts}.csv")
    PRED    = os.path.join(LOGS_DIR,   f"predictions_{ts}.csv")   # ← NEW
    VID     = os.path.join(OUTPUT_DIR, f"monitored_{ts}.mp4")
    os.makedirs(OUT_DIR, exist_ok=True)

    s0 = cv2.imread(frames[0])
    if s0 is None:
        raise ValueError(f"Cannot read: {frames[0]}")
    W0, H0 = s0.shape[1], s0.shape[0]
    W, H   = W0 - W0 % 2, H0 - H0 % 2

    writer = cv2.VideoWriter(VID,
                              cv2.VideoWriter_fourcc(*"mp4v"),
                              OUTPUT_VIDEO_FPS, (W, H))
    if not writer.isOpened():
        raise RuntimeError(f"VideoWriter failed for {VID!r}")

    monitor = InvigMonitor(log_path=LOG, pred_path=PRED)
    print(f"\n▶  {len(frames)} frames  →  {VID}\n")

    t0, written = time.time(), 0
    for i, fp in enumerate(frames):
        frame = cv2.imread(fp)
        fname = os.path.basename(fp)
        if frame is None:
            frame = np.zeros((H, W, 3), dtype=np.uint8)
            put_label(frame, f"UNREADABLE {fname}", 8, 28, C_ALERT)

        out = monitor.process(frame, fname)
        if out.shape[1] != W or out.shape[0] != H:
            out = cv2.resize(out, (W, H), interpolation=cv2.INTER_AREA)

        cv2.imwrite(os.path.join(OUT_DIR, fname), out)
        writer.write(out)
        written += 1

        if (i + 1) % 50 == 0:
            el  = time.time() - t0
            eta = el / (i+1) * (len(frames)-i-1)
            print(f"  [{(i+1)/len(frames)*100:.1f}%]  "
                  f"{i+1}/{len(frames)}  elapsed:{el:.0f}s  ETA:{eta:.0f}s")

    writer.release()
    elapsed = time.time() - t0
    print(f"\n✅ Done in {elapsed:.0f}s  — {written}/{len(frames)} frames")
    print(f"   📹 {VID}")
    print(f"   📋 {LOG}")
    print(f"   📝 {PRED}")   # ← NEW

    # ── Print alert summary ───────────────────────────────────
    if os.path.exists(LOG):
        df = pd.read_csv(LOG)
        print(f"\n{'='*60}\nAlert Summary — {len(df)} total\n{'='*60}")
        if len(df):
            print(df.groupby(["alert_type","severity"])
                  .size().rename("count")
                  .sort_values(ascending=False).to_string())
            crit = df[df.severity == "CRITICAL"]
            print("\n── CRITICAL ─────────────────────────────────────")
            print(crit[["timestamp","alert_type","detail","frame"]]
                  .to_string(index=False) if len(crit) else "  None ✅")
        else:
            print("No alerts ✅")

    # ── Print prediction summary ← NEW ───────────────────────
    if os.path.exists(PRED):
        pdf = pd.read_csv(PRED)
        print(f"\n{'='*60}\nPrediction Summary — {len(pdf)} detections\n{'='*60}")
        print(pdf["pred_activity"].value_counts().to_string())
        print(f"\n  Mean confidence : {pdf['pred_conf'].mean():.3f}")
        n_phone = int((pdf["pred_activity"] == "Phone Use").sum())
        print(f"  Rows with pred_activity == Phone Use: {n_phone}")
        print(f"\n  To evaluate accuracy, run:")
        print(f"  python evaluate_accuracy.py --pred_csv {PRED} "
              f"--gt ground_truth.csv --output ./eval_results")


if __name__ == "__main__":
    main()