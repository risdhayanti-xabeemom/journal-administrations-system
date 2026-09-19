from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from config import settings
from models import Base, Journal


engine_kwargs = {"pool_pre_ping": True, "future": True}
if settings.database_url.startswith("sqlite:"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database() -> None:
    settings.prepare_directories()
    Base.metadata.create_all(engine)
    with session_scope() as session:
        existing = set(session.scalars(select(Journal.abbreviation)))
        defaults = {
            "ELKOLIND": "{sequence:03d}/SK/ELK/{roman_month}/{year}",
            "JASENS": "{sequence:02d}/{roman_month}/JASENS/{year}",
        }
        for abbreviation, loa_pattern in defaults.items():
            if abbreviation not in existing:
                session.add(
                    Journal(
                        name=f"{abbreviation} - configure journal name",
                        abbreviation=abbreviation,
                        publisher="Configure in Settings",
                        editor_in_chief="Configure in Settings",
                        loa_number_format=loa_pattern,
                    )
                )
