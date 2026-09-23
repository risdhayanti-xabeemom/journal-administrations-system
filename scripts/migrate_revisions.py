"""Add revision-only tables without touching existing JAS records."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from models import Base
from services.database import engine


def main() -> None:
    backend = engine.dialect.name
    backup = None
    if backend == "sqlite":
        database = make_url(settings.database_url).database
        if database and database != ":memory:" and Path(database).exists():
            source = Path(database).resolve()
            backup_dir = source.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{source.stem}-before-revisions-{datetime.now():%Y%m%d-%H%M%S}{source.suffix}.bak"
            # SQLite's online backup API also copies committed WAL pages consistently.
            with sqlite3.connect(source) as live, sqlite3.connect(backup) as saved:
                live.backup(saved)
    elif backend == "postgresql":
        if os.getenv("JAS_MIGRATION_BACKUP_CONFIRMED") != "1":
            raise RuntimeError("Back up PostgreSQL/Supabase first, then set JAS_MIGRATION_BACKUP_CONFIRMED=1.")
    else:
        raise RuntimeError(f"Unsupported database backend for revision migration: {backend}")
    revision_tables = [table for table in Base.metadata.sorted_tables if table.name.startswith("revision_")]
    Base.metadata.create_all(engine, tables=revision_tables, checkfirst=True)
    missing = {table.name for table in revision_tables} - set(inspect(engine).get_table_names())
    if missing:
        raise RuntimeError(f"Revision migration incomplete: {sorted(missing)}")
    for table in revision_tables:
        actual_columns = {column["name"] for column in inspect(engine).get_columns(table.name)}
        missing_columns = set(table.columns.keys()) - actual_columns
        if missing_columns:
            raise RuntimeError(f"Revision table {table.name} has an incompatible pre-existing schema: {sorted(missing_columns)}")
    if backup:
        print(f"SQLite backup: {backup}")
    print("Revision migration complete; existing JAS tables and rows were preserved.")


if __name__ == "__main__":
    main()
