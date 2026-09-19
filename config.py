from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _secret(name: str, default: str | None = None) -> str | None:
    """Read environment first, then Streamlit secrets when available."""
    value = os.getenv(name)
    if value is not None:
        return value
    try:
        import streamlit as st

        return st.secrets.get(name, default)
    except Exception:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = _secret("DATABASE_URL", f"sqlite:///{BASE_DIR / 'jas.db'}") or ""
    public_base_url: str = _secret("PUBLIC_BASE_URL", "http://localhost:8501") or ""
    admin_email: str | None = _secret("ADMIN_EMAIL")
    admin_password: str | None = _secret("ADMIN_PASSWORD")
    admin_name: str = _secret("ADMIN_NAME", "System Administrator") or "System Administrator"
    upload_dir: Path = Path(_secret("PRIVATE_UPLOAD_DIR", str(BASE_DIR / "private_uploads")) or "")
    document_dir: Path = Path(_secret("DOCUMENT_OUTPUT_DIR", str(BASE_DIR / "generated_documents")) or "")
    template_dir: Path = Path(_secret("TEMPLATE_STORAGE_DIR", str(BASE_DIR / "private_uploads" / "templates")) or "")
    docx_pdf_converter: str = (_secret("DOCX_PDF_CONVERTER", "auto") or "auto").lower()
    libreoffice_path: str | None = _secret("LIBREOFFICE_PATH")
    max_upload_bytes: int = int(_secret("MAX_UPLOAD_BYTES", "5242880") or 5242880)
    session_hours: int = int(_secret("SESSION_HOURS", "8") or 8)

    @property
    def is_development_database(self) -> bool:
        return self.database_url.startswith("sqlite:")

    def prepare_directories(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.document_dir.mkdir(parents=True, exist_ok=True)
        self.template_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
