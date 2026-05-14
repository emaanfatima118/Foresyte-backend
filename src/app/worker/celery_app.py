"""
Celery application for offload of long-running video processing.

Run workers (Linux / production):
  cd src && celery -A app.worker.celery_app worker --loglevel=INFO --pool=prefork --concurrency=4 -Q celery

Windows: prefork is not supported; use threads or solo:
  cd src && celery -A app.worker.celery_app worker --loglevel=INFO --pool=threads --concurrency=4 -Q celery

Requires Redis (or set CELERY_BROKER_URL to your broker).
"""
import os
import sys
from pathlib import Path

# Ensure project root on path (sibling of `app`: `database`, uploads, etc.) regardless of CWD.
_src_root = Path(__file__).resolve().parents[2]
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

from celery import Celery
from dotenv import load_dotenv

load_dotenv()

_broker = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0").strip()
_result = os.getenv("CELERY_RESULT_BACKEND", _broker).strip()

app = Celery(
    "foresyte",
    broker=_broker or "redis://localhost:6379/0",
    backend=_result or _broker,
    include=["app.worker.tasks"],
)

app.conf.update(
    task_default_queue=os.getenv("CELERY_TASK_QUEUE", "celery"),
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Long videos: tune via env if needed
    task_time_limit=int(os.getenv("CELERY_TASK_TIME_LIMIT", str(86400))),
    worker_prefetch_multiplier=1,
)


__all__ = ["app"]
