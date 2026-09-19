from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from models import DocumentTemplate, Journal, Role, User
from services.database import SessionLocal
from services.template_service import activate_loa_template, upload_loa_template


PACKAGED = {
    "ELKOLIND": PROJECT_ROOT / "templates" / "loa" / "elkolind" / "v1" / "LoA Elkolind-2026.docx",
    "JASENS": PROJECT_ROOT / "templates" / "loa" / "jasens" / "v1" / "Draft_LoA_JASENS.docx",
}


def main() -> None:
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.role == Role.SUPER_ADMIN).order_by(User.created_at))
        if not user:
            raise RuntimeError("Create/bootstrap a SUPER_ADMIN user before seeding official templates.")
        for abbreviation, source in PACKAGED.items():
            journal = session.scalar(select(Journal).where(Journal.abbreviation == abbreviation))
            if not journal:
                raise RuntimeError(f"Journal {abbreviation} does not exist.")
            content = source.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            existing = session.scalar(select(DocumentTemplate).where(
                DocumentTemplate.journal_id == journal.id,
                DocumentTemplate.template_type == "LOA",
                DocumentTemplate.checksum == checksum,
            ))
            if existing:
                activate_loa_template(session, existing, user)
                print(f"{abbreviation}: existing template v{existing.version} activated")
            else:
                template = upload_loa_template(
                    session,
                    journal,
                    user,
                    original_filename=source.name,
                    content=content,
                    activate=True,
                )
                print(f"{abbreviation}: official template stored as v{template.version}")
        session.commit()


if __name__ == "__main__":
    main()
