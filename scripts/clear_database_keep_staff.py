#!/usr/bin/env python3
"""
Clear operational data while preserving staff accounts.

Preserved tables:
  - admins
  - investigators
  - invigilators

Everything else in the application schema is deleted.

Usage:
  cd D:\\foresyte\\Foresyte-backend
  python scripts/clear_database_keep_staff.py --yes
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

from sqlalchemy import delete

from database.db import engine, SessionLocal
from database.models import Base

PRESERVE_TABLES = {
    "admins",
    "investigators",
    "invigilators",
}


def _tables_to_clear():
    # Delete children before parents.
    tables = [
        table
        for table in reversed(Base.metadata.sorted_tables)
        if table.name not in PRESERVE_TABLES
    ]
    return tables


def main() -> None:
    if "--yes" not in sys.argv:
        print("Refusing to run without --yes")
        print("This will permanently delete all data except admins, investigators, and invigilators.")
        print("Run: python scripts/clear_database_keep_staff.py --yes")
        sys.exit(1)

    tables = _tables_to_clear()
    print(f"Using database engine: {engine.url.render_as_string(hide_password=True)}")
    print("Preserving tables:", ", ".join(sorted(PRESERVE_TABLES)))
    print("Clearing tables in order:")
    for table in tables:
        print(f"  - {table.name}")

    session = SessionLocal()
    try:
        for table in tables:
            session.execute(delete(table))
        session.commit()
        print("Database clear completed successfully.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
