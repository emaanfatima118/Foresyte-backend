"""
ForeSyte full-frame behaviour detection (uploads/frames/detect.py) for ai_engine.

Runs the behaviour YOLO on the entire extracted frame (WBF / optional TTA, etc.),
not a separate person detector + per-crop classifier.

Configure via env (see ForesyteDetectConfig): FORESYTE_BEHAVIOUR_MODEL_PATH, etc.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from .run_detection import ClassificationResult

log = logging.getLogger(__name__)

_SRC_ROOT = Path(__file__).resolve().parents[2]
_FRAMES_DETECT = _SRC_ROOT / "uploads" / "frames" / "detect.py"

_detect_module = None


def _load_detect_module():
    global _detect_module
    if _detect_module is not None:
        return _detect_module
    if not _FRAMES_DETECT.is_file():
        raise FileNotFoundError(
            f"ForeSyte detect script not found: {_FRAMES_DETECT}"
        )
    spec = importlib.util.spec_from_file_location(
        "foresyte_frames_detect", _FRAMES_DETECT
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    _detect_module = mod
    return mod


def _resolve_model_path(path: str) -> str:
    p = Path(path.strip())
    if p.is_absolute() and p.is_file():
        return str(p.resolve())
    roots = (
        _SRC_ROOT / "uploads" / "frames",
        Path(__file__).resolve().parent,
    )
    for root in roots:
        cand = (root / path).resolve()
        if cand.is_file():
            return str(cand)
    return str(p.resolve())


@dataclass
class ForesyteDetectConfig:
    model_path: str = field(
        default_factory=lambda: os.getenv(
            "FORESYTE_BEHAVIOUR_MODEL_PATH", "best11.pt"
        ).strip()
        or "best11.pt"
    )
    thresholds_path: Optional[str] = field(
        default_factory=lambda: (
            os.getenv("FORESYTE_THRESHOLDS_PATH") or ""
        ).strip() or None
    )
    imgsz: int = field(
        default_factory=lambda: int(os.getenv("FORESYTE_IMGSZ", "1280"))
    )
    iou: float = field(
        default_factory=lambda: float(os.getenv("FORESYTE_IOU", "0.35"))
    )
    device: str = field(
        default_factory=lambda: os.getenv("FORESYTE_DEVICE", "0").strip() or "0"
    )
    conf_floor: Optional[float] = field(
        default_factory=lambda: (
            float(os.getenv("FORESYTE_CONF")) if os.getenv("FORESYTE_CONF") else None
        )
    )
    tta: bool = field(
        default_factory=lambda: os.getenv("FORESYTE_TTA", "").lower()
        in ("1", "true", "yes")
    )
    multiscale: bool = field(
        default_factory=lambda: os.getenv("FORESYTE_MULTISCALE", "").lower()
        in ("1", "true", "yes")
    )
    tile: bool = field(
        default_factory=lambda: os.getenv("FORESYTE_TILE", "").lower()
        in ("1", "true", "yes")
    )

    @classmethod
    def from_env(cls) -> "ForesyteDetectConfig":
        return cls()


# Per-thread model cache: parallel frame analysis uses several threads; a single global
# model races on first load and Ultralytics predict is not assumed thread-safe across workers.
_thread_local = threading.local()


def _get_model(cfg: ForesyteDetectConfig):
    resolved = _resolve_model_path(cfg.model_path)
    cache: dict[str, object] | None = getattr(_thread_local, "foresyte_behaviour", None)
    if cache is None:
        cache = {}
        _thread_local.foresyte_behaviour = cache
    if resolved not in cache:
        mod = _load_detect_module()
        log.info("Loading ForeSyte behaviour model: %s", resolved)
        cache[resolved] = mod.load_model(resolved)
    return cache[resolved]


def run_foresyte_on_image(
    image: np.ndarray,
    cfg: Optional[ForesyteDetectConfig] = None,
) -> tuple[list[ClassificationResult], np.ndarray]:
    """
    Run uploads/frames/detect.py pipeline on a BGR frame.
    Returns (ClassificationResult list, annotated BGR image).
    """
    cfg = cfg or ForesyteDetectConfig.from_env()
    mod = _load_detect_module()
    model = _get_model(cfg)
    class_names = list(model.names.values())
    fb = cfg.conf_floor
    if fb is None:
        fb = mod.GLOBAL_CONF_FLOOR
    thresholds = mod.load_thresholds(cfg.thresholds_path, class_names, fb)

    device = mod.resolve_device(cfg.device)

    dets, dt_ms = mod.infer_image_array(
        model,
        image,
        thresholds,
        cfg.imgsz,
        cfg.iou,
        device,
        cfg.tta,
        cfg.multiscale,
        cfg.tile,
    )

    suspicious_set = getattr(mod, "SUSPICIOUS", frozenset())
    suspect_fold = getattr(mod, "is_suspicious_label", None)
    fold_set = getattr(mod, "_SUSPECT_FOLD", None)
    results: list[ClassificationResult] = []
    for i, d in enumerate(dets, start=1):
        box = d["box"]
        x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
        label = d["class_name"]
        score = float(d["score"])
        if callable(suspect_fold):
            susp = bool(suspect_fold(str(label)))
        elif isinstance(fold_set, set):
            susp = str(label).casefold() in fold_set
        else:
            susp = label in suspicious_set
        results.append(
            ClassificationResult(
                bbox=(x1, y1, x2, y2),
                label=label,
                confidence=round(score, 4),
                all_scores={label: round(score, 4)},
                student_index=i,
                suspicious_override=susp,
            )
        )

    annotated = mod.draw_detections(image, dets)
    strategies = [
        s for s, on in (
            ("TTA×7", cfg.tta),
            ("multiscale", cfg.multiscale),
            ("zone-tiling", cfg.tile),
        )
        if on
    ]
    annotated = mod.draw_hud(annotated, dets, "frame", dt_ms, strategies)

    return results, annotated
