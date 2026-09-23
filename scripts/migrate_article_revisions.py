"""Back up SQLite (or require a verified PostgreSQL backup) before additive article tables."""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from models import Base
from services.database import engine


def main() -> None:
    backup = None
    if engine.dialect.name == "sqlite":
        database = make_url(settings.database_url).database
        if database and database != ":memory:" and Path(database).exists():
            source = Path(database).resolve()
            backup_dir = source.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{source.stem}-before-article-revisions-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}{source.suffix}.bak"
            with sqlite3.connect(source) as live, sqlite3.connect(backup) as saved:
                live.backup(saved)
    elif engine.dialect.name == "postgresql":
        if os.getenv("JAS_MIGRATION_BACKUP_CONFIRMED") != "1":
            raise RuntimeError("Back up PostgreSQL/Supabase first, then set JAS_MIGRATION_BACKUP_CONFIRMED=1.")
    else:
        raise RuntimeError(f"Unsupported database backend: {engine.dialect.name}")
    tables = [table for table in Base.metadata.sorted_tables if table.name.startswith("article_revision_")]
    Base.metadata.create_all(engine, tables=tables, checkfirst=True)
    inspector = inspect(engine)
    if "article_revision_jobs" in inspector.get_table_names() and "metadata_json" not in {
        column["name"] for column in inspector.get_columns("article_revision_jobs")
    }:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE article_revision_jobs ADD COLUMN metadata_json TEXT"))
        inspector = inspect(engine)
    missing = {table.name for table in tables} - set(inspector.get_table_names())
    if missing:
        raise RuntimeError(f"Article revision migration incomplete: {sorted(missing)}")
    for table in tables:
        present = {column["name"] for column in inspector.get_columns(table.name)}
        if set(table.columns.keys()) - present:
            raise RuntimeError(f"Article revision table {table.name} has an incompatible schema.")
    if backup:
        print(f"SQLite backup: {backup}")
    print("Article revision migration complete. Existing JAS tables were not changed.")


if __name__ == "__main__":
    main()
