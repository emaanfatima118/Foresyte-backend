"""
Sandbox entrypoint for first-frame seat mapping tests.

Canonical implementation lives in ``app.video_processing.first_frame_seat_mapping``.
"""

from app.video_processing.first_frame_seat_mapping import run_lab_from_paths as run_first_frame_mapping

__all__ = ["run_first_frame_mapping"]
