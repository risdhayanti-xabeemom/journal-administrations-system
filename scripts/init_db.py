from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.core import bootstrap_admin
from services.database import SessionLocal, init_database


def main() -> None:
    init_database()
    with SessionLocal() as session:
        created = bootstrap_admin(session)
        session.commit()
    print("Database initialized.")
    print("Bootstrap administrator created." if created else "Bootstrap administrator unchanged or not configured.")


if __name__ == "__main__":
    main()

