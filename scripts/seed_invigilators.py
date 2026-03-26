#!/usr/bin/env python3
"""
Seed named invigilators for ForeSyte (idempotent by email).
Run from repo: python scripts/seed_invigilators.py
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent.absolute()
BACKEND_DIR = SCRIPT_DIR.parent
SRC_DIR = BACKEND_DIR / "src"

env_file = BACKEND_DIR / ".env"
if env_file.exists():
    load_dotenv(env_file)
else:
    load_dotenv(BACKEND_DIR.parent / ".env")

sys.path.insert(0, str(SRC_DIR))
os.chdir(BACKEND_DIR)

from database.db import SessionLocal
from database.models import Invigilator
from database.auth import hash_password

# Default login password for seeded invigilators (change after first login in production)
DEFAULT_PASSWORD = "Invigilator123!"

SEED_INVIGILATORS = [
    {"name": "Ms. Saira Qamar", "email": "saira.qamar@invigilator.foresyte.local"},
    {"name": "Mr. Inam Ullah Shaikh", "email": "inam.shaikh@invigilator.foresyte.local"},
    {"name": "Ms. Aden Sial", "email": "aden.sial@invigilator.foresyte.local"},
    {"name": "Ms. Emaan Fatima", "email": "emaan.fatima@invigilator.foresyte.local"},
]


def main():
    db = SessionLocal()
    try:
        added = 0
        skipped = 0
        for row in SEED_INVIGILATORS:
            existing = db.query(Invigilator).filter(Invigilator.email == row["email"]).first()
            if existing:
                skipped += 1
                print(f"Skip (exists): {row['name']} <{row['email']}>")
                continue
            inv = Invigilator(
                name=row["name"],
                email=row["email"],
                password_hash=hash_password(DEFAULT_PASSWORD),
                created_at=datetime.now(timezone.utc),
            )
            db.add(inv)
            added += 1
            print(f"Added: {row['name']} <{row['email']}>")
        db.commit()
        print(f"\nDone. Added {added}, skipped {skipped}. Default password: {DEFAULT_PASSWORD}")
    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
