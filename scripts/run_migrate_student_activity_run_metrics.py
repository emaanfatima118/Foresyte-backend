#!/usr/bin/env python3
"""
Apply activity-report columns used by report severity and evidence appendices.

Uses the same DATABASE_URL as the FastAPI app (no psql required).

Usage (from repo root or from Foresyte-backend):

    cd D:\\foresyte\\Foresyte-backend
    python scripts/run_migrate_student_activity_run_metrics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = BACKEND_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from dotenv import load_dotenv

load_dotenv(BACKEND_DIR / ".env")
if not (BACKEND_DIR / ".env").exists():
    load_dotenv(BACKEND_DIR.parent / ".env")

from sqlalchemy import text

from database.db import engine

STATEMENTS = [
    "ALTER TABLE student_activities ADD COLUMN run_frame_count INTEGER",
    "ALTER TABLE student_activities ADD COLUMN severity_rule VARCHAR(32)",
    "ALTER TABLE student_activities ADD COLUMN report_evidence_url TEXT",
    "ALTER TABLE student_activities ADD COLUMN identification_evidence_url TEXT",
    "ALTER TABLE invigilator_activities ADD COLUMN report_evidence_url TEXT",
]


def _already_applied(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "already exists" in msg or "duplicatecolumn" in msg.replace(" ", ""):
        return True
    # PostgreSQL psycopg2 duplicate_column
    if hasattr(exc, "orig") and exc.orig is not None:
        return _already_applied(exc.orig)
    return False


def main() -> None:
    print(f"Using database engine: {engine.url.render_as_string(hide_password=True)}")
    for sql in STATEMENTS:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            print(f"OK: {sql}")
        except Exception as e:
            if _already_applied(e):
                print(f"SKIP (column already present): {sql}")
            else:
                print(f"ERROR: {sql}\n{e}", file=sys.stderr)
                raise
    print("Migration finished.")


if __name__ == "__main__":
    main()
