from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from models import Base
from services.database import engine


ADDITIONS = {
    "journals": {
        "loa_qr_enabled": {"sqlite": "BOOLEAN NOT NULL DEFAULT 0", "postgresql": "BOOLEAN NOT NULL DEFAULT FALSE"},
        "loa_qr_placement": {"default": "VARCHAR(32) NOT NULL DEFAULT 'bottom-right'"},
        "loa_qr_size_mm": {"default": "INTEGER NOT NULL DEFAULT 20"},
        "default_volume": {"default": "VARCHAR(32)"},
        "default_issue": {"default": "VARCHAR(32)"},
        "default_publication_month": {"default": "VARCHAR(32)"},
        "default_publication_year": {"default": "INTEGER"},
    },
    "submissions": {
        "planned_publication_month": {"default": "VARCHAR(32)"},
    },
    "loa_documents": {
        "template_id": {"sqlite": "CHAR(36)", "postgresql": "UUID"},
        "issued_at": {"sqlite": "DATETIME", "postgresql": "TIMESTAMP WITH TIME ZONE"},
        "docx_path": {"default": "VARCHAR(500)"},
        "document_hash": {"default": "VARCHAR(64)"},
        "reissue_reason": {"default": "TEXT"},
    },
}


def backup_sqlite() -> Path | None:
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    source = Path(url.database).resolve()
    if not source.exists():
        return None
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"{source.stem}-{datetime.now():%Y%m%d-%H%M%S}{source.suffix}.bak"
    shutil.copy2(source, destination)
    return destination


def ensure_backup_policy() -> Path | None:
    dialect = engine.dialect.name
    if dialect == "sqlite":
        return backup_sqlite()
    if dialect == "postgresql" and os.getenv("JAS_MIGRATION_BACKUP_CONFIRMED") != "1":
        raise RuntimeError(
            "Create a PostgreSQL backup with pg_dump, then set JAS_MIGRATION_BACKUP_CONFIRMED=1 and rerun."
        )
    return None


def add_missing_columns() -> list[str]:
    changed: list[str] = []
    dialect = engine.dialect.name
    with engine.begin() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for table, columns in ADDITIONS.items():
            if table not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for column, variants in columns.items():
                if column in existing:
                    continue
                definition = variants.get(dialect, variants.get("default"))
                connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'))
                changed.append(f"{table}.{column}")
        connection.execute(text(
            "UPDATE journals SET loa_number_format = :pattern "
            "WHERE abbreviation = 'ELKOLIND' AND (loa_number_format IS NULL OR loa_number_format = :legacy)"
        ), {"pattern": "{sequence:03d}/SK/ELK/{roman_month}/{year}", "legacy": "{sequence:03d}/LoA/{journal}/{roman_month}/{year}"})
        connection.execute(text(
            "UPDATE journals SET loa_number_format = :pattern "
            "WHERE abbreviation = 'JASENS' AND (loa_number_format IS NULL OR loa_number_format = :legacy)"
        ), {"pattern": "{sequence:02d}/{roman_month}/JASENS/{year}", "legacy": "{sequence:03d}/LoA/{journal}/{roman_month}/{year}"})
        connection.execute(text("UPDATE loa_documents SET issued_at = created_at WHERE issued_at IS NULL"))
    return changed


def main() -> None:
    backup = ensure_backup_policy()
    # Creates only missing tables and indexes; it never drops or recreates existing data.
    Base.metadata.create_all(engine)
    changed = add_missing_columns()
    if backup:
        print(f"SQLite backup: {backup}")
    print("Migration complete. Existing records were preserved.")
    print("Added columns: " + (", ".join(changed) if changed else "none (already current)"))


if __name__ == "__main__":
    main()
