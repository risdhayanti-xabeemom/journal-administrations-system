"""Isolated OJS import smoke check; never opens the configured JAS database."""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import AuditLog, Base, Journal, Role, Submission, User
from services.ojs_import import build_preview, confirm_import, detect_columns, read_import_file


def main() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sample = (
        "Submission-ID;Article Title;Contributors;Primary Contact;Contact Email\n"
        "2401;Valid OJS title;Zara;Zara;zara@example.test\n"
        "2402;Sparse OJS title;;;\n"
        ";Missing ID;A;A;a@example.test\n"
        "2403;Broken row;extra;columns;here;unexpected\n"
    ).encode("utf-8-sig")
    with Session(engine, expire_on_commit=False) as session:
        journal = Journal(name="Smoke Journal", abbreviation="SMOKE")
        user = User(email="smoke@example.test", display_name="Smoke", password_hash="unused", role=Role.SUPER_ADMIN)
        session.add_all([journal, user])
        session.commit()
        table = read_import_file("ojs-smoke.csv", sample)
        mapping, warnings = detect_columns(table.headers)
        preview = build_preview(session, journal, table, mapping)
        print("Auto mapping:", {key: table.headers[index] for key, index in mapping.items() if index is not None})
        print("Ambiguity warnings:", warnings)
        print("Preview:", [(row.source_row, row.status, row.action) for row in preview])
        print("Rows before confirmation:", session.scalar(select(func.count(Submission.id))))
        result = confirm_import(session, journal, user, table, mapping, "ojs-smoke.csv")
        print("Result:", {"imported": result.imported, "skipped_duplicates": result.skipped_duplicates,
                          "updated": result.updated, "incomplete": result.incomplete, "invalid": result.invalid})
        print("Rows after confirmation:", session.scalar(select(func.count(Submission.id))))
        print("Batch audit events:", session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "SUBMISSION_IMPORT_BATCH")))
        assert (result.imported, result.invalid) == (2, 2)


if __name__ == "__main__":
    main()
