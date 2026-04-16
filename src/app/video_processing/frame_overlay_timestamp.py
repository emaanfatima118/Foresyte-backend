"""
Read exam date/time burned into CCTV frames (top-left overlay) using Tesseract OCR.

Requires the ``tesseract`` binary on PATH (https://github.com/tesseract-ocr/tesseract).
Python package: ``pytesseract``. Disable with env ``EXAM_TIMESTAMP_OCR=0``.

Crop defaults target a typical top-left stamp like: ``11-25-2025 Tue 16:02:24``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

try:
    import pytesseract as _pytesseract  # type: ignore
except ImportError:
    _pytesseract = None

_MISSING_TESSERACT_WARNED = False


def _preprocess_roi(roi_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    scale = float(os.getenv("EXAM_TS_OCR_SCALE", "2.2"))
    if scale > 1.0:
        gray = cv2.resize(
            gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if float(np.mean(th)) > 127.0:
        th = 255 - th
    return th


def parse_datetime_from_ocr_text(raw: str) -> Optional[datetime]:
    """
    Parse common CCTV overlay formats from noisy OCR text.
    Examples: ``11-25-2025 Tue 16:02:24``, ``11/25/2025  4:02:24 PM`` (best-effort).
    """
    if not raw or not raw.strip():
        return None
    s = " ".join(raw.split())
    # mm-dd-yyyy [weekday] HH:MM:SS (weekday optional / noisy)
    m = re.search(
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})\D{0,12}(\d{1,2}):(\d{2}):(\d{2})",
        s,
        re.IGNORECASE,
    )
    if m:
        mm, dd, yyyy, hh, mi, ss = (int(m.group(i)) for i in range(1, 7))
        try:
            return datetime(yyyy, mm, dd, hh, mi, ss)
        except ValueError:
            pass
    # yyyy-mm-dd HH:MM:SS
    m2 = re.search(
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})\D{0,8}(\d{1,2}):(\d{2}):(\d{2})",
        s,
    )
    if m2:
        yyyy, mm, dd, hh, mi, ss = (int(m2.group(i)) for i in range(1, 7))
        try:
            return datetime(yyyy, mm, dd, hh, mi, ss)
        except ValueError:
            pass
    return None


def parse_exam_timestamp_from_frame(frame_bgr: np.ndarray) -> Optional[datetime]:
    """
    Crop top-left region, OCR, parse datetime. Returns None if disabled, failed, or
    Tesseract unavailable.
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return None
    if os.getenv("EXAM_TIMESTAMP_OCR", "1").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return None

    h, w = frame_bgr.shape[:2]
    top_frac = float(os.getenv("EXAM_TS_CROP_TOP_FRAC", "0.0"))
    left_frac = float(os.getenv("EXAM_TS_CROP_LEFT_FRAC", "0.0"))
    crop_h_frac = float(os.getenv("EXAM_TS_CROP_HEIGHT_FRAC", "0.16"))
    crop_w_frac = float(os.getenv("EXAM_TS_CROP_WIDTH_FRAC", "0.62"))

    y1 = max(0, int(h * top_frac))
    x1 = max(0, int(w * left_frac))
    y2 = min(h, int(h * (top_frac + crop_h_frac)))
    x2 = min(w, int(w * (left_frac + crop_w_frac)))
    if y2 <= y1 or x2 <= x1:
        return None

    roi = frame_bgr[y1:y2, x1:x2]
    proc = _preprocess_roi(roi)

    global _MISSING_TESSERACT_WARNED
    if _pytesseract is None:
        if not _MISSING_TESSERACT_WARNED:
            _MISSING_TESSERACT_WARNED = True
            logger.warning(
                "Exam timestamp OCR skipped: install pytesseract and the tesseract binary "
                "(pip install pytesseract; add tesseract to PATH)."
            )
        return None

    tess_cfg = os.getenv("EXAM_TS_TESSERACT_CONFIG", "--psm 6").strip() or "--psm 6"
    try:
        text = _pytesseract.image_to_string(proc, config=tess_cfg)
    except Exception as e:
        logger.warning("Tesseract OCR failed on exam timestamp crop: %s", e)
        return None

    dt = parse_datetime_from_ocr_text(text)
    if dt:
        logger.debug("Exam overlay OCR timestamp: %s (raw snippet: %r)", dt.isoformat(), text[:100])
    else:
        logger.debug("Exam overlay OCR could not parse datetime from: %r", text[:200])
    return dt
