"""Celery tasks for video ingestion and processing."""

import asyncio
import logging
import os

from celery import current_app

from app.worker.celery_app import app

logger = logging.getLogger(__name__)


def _sync_frame_workers_with_celery_pool() -> None:
    """
    Celery --concurrency N applies to concurrent *tasks*, not to threads inside one task.
    When the user runs e.g. --concurrency 4, mirror that into FRAME_ANALYSIS_THREAD_WORKERS
    so a single video still uses N parallel inference threads (unless already set in env).
    """
    if os.getenv("FRAME_ANALYSIS_THREAD_WORKERS") or os.getenv(
        "VIDEO_PIPELINE_PARALLEL_WORKERS"
    ):
        return
    wc = getattr(current_app.conf, "worker_concurrency", None)
    if wc is None:
        return
    try:
        n = max(1, int(wc))
    except (TypeError, ValueError):
        return
    os.environ["FRAME_ANALYSIS_THREAD_WORKERS"] = str(n)
    logger.debug("FRAME_ANALYSIS_THREAD_WORKERS=%s (from Celery worker_concurrency)", n)


@app.task(name="foresyte.process_uploaded_video", bind=False)
def process_uploaded_video(
    stream_id: str,
    source: str,
    exam_id: str,
    room_id: str,
    stream_type: str,
    use_database: bool,
):
    """Run async `process_video_background` in an isolated worker process."""
    _sync_frame_workers_with_celery_pool()
    from database.api.video_streams import process_video_background

    asyncio.run(
        process_video_background(
            stream_id,
            source,
            exam_id,
            room_id,
            stream_type,
            use_database,
        )
    )


__all__ = ["process_uploaded_video"]
