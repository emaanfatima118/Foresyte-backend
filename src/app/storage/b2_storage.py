"""
Backward-compatible imports. Prefer ``app.storage.blob_storage`` (R2 / B2).
"""

from app.storage.blob_storage import (  # noqa: F401
    EVIDENCE_DOWNLOADS_DIR,
    prepare_evidence_files_for_report,
    upload_evidence_frame,
)

__all__ = [
    "EVIDENCE_DOWNLOADS_DIR",
    "prepare_evidence_files_for_report",
    "upload_evidence_frame",
]
