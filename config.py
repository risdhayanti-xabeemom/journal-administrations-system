from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


BASE_DIR = Path(__file__).resolve().parent


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def normalize_public_base_url(value: str) -> str:
    """Return a stable origin/path and discard unsafe query/fragment suffixes."""
    candidate = (value or "").strip()
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def streamlit_cloud_detected() -> bool:
    """Detect common Streamlit Community Cloud runtime markers conservatively."""
    explicit = (
        os.getenv("STREAMLIT_CLOUD"),
        os.getenv("STREAMLIT_SHARING_MODE"),
        os.getenv("STREAMLIT_RUNTIME_ENV"),
    )
    return any(
        (value or "").strip().lower() in {"1", "true", "yes", "cloud", "streamlit", "community_cloud"}
        for value in explicit
    ) or Path("/mount/src").is_dir()


def validate_public_base_url(value: str, *, production: bool = False) -> tuple[str, ...]:
    """Validate the public origin used in document-verification QR links.

    The validator reports configuration warnings rather than raising so a bad QR
    origin cannot make the rest of JAS unavailable.
    """
    raw = (value or "").strip()
    candidate = normalize_public_base_url(raw)
    parsed = urlsplit(candidate)
    raw_parsed = urlsplit(raw)
    warnings: list[str] = []
    if not candidate or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ("PUBLIC_BASE_URL must be an absolute HTTP(S) URL.",)
    if parsed.username or parsed.password:
        warnings.append("PUBLIC_BASE_URL must not contain embedded credentials.")
    if raw_parsed.query or raw_parsed.fragment:
        warnings.append("PUBLIC_BASE_URL must not contain query parameters or a fragment.")
    hostname = (parsed.hostname or "").lower()
    if production and parsed.scheme != "https":
        warnings.append("PUBLIC_BASE_URL must use HTTPS in production.")
    if production and hostname in _LOCAL_HOSTS:
        warnings.append("PUBLIC_BASE_URL is not configured for production. QR verification links will be invalid.")
    return tuple(warnings)


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
    revision_storage_backend: str = (_secret("REVISION_STORAGE_BACKEND", "local") or "local").lower()
    revision_storage_dir: Path = Path(_secret("REVISION_STORAGE_DIR", str(BASE_DIR / "private_uploads" / "revisions")) or "")
    revision_supabase_url: str | None = _secret("REVISION_SUPABASE_URL")
    revision_supabase_service_role_key: str | None = _secret("REVISION_SUPABASE_SERVICE_ROLE_KEY")
    revision_supabase_bucket: str = _secret("REVISION_SUPABASE_BUCKET", "jas-private-revisions") or "jas-private-revisions"
    revision_max_file_bytes: int = int(_secret("REVISION_MAX_FILE_BYTES", "20971520") or 20971520)
    revision_default_mode: str = (_secret("REVISION_DEFAULT_MODE", "REVIEWER_DRIVEN") or "REVIEWER_DRIVEN").upper()
    revision_highlight_style: str = (_secret("REVISION_HIGHLIGHT_STYLE", "yellow") or "yellow").lower()
    revision_allow_full_polishing: bool = (_secret("REVISION_ALLOW_FULL_POLISHING", "false") or "false").lower() == "true"
    revision_ai_enabled: bool = (_secret("REVISION_AI_ENABLED", "false") or "false").lower() == "true"
    revision_ai_provider: str = (_secret("REVISION_AI_PROVIDER", "openai_compatible") or "openai_compatible").lower()
    revision_ai_endpoint: str | None = _secret("REVISION_AI_ENDPOINT")
    revision_ai_model: str | None = _secret("REVISION_AI_MODEL")
    revision_ai_api_key: str | None = _secret("REVISION_AI_API_KEY")

    @property
    def is_development_database(self) -> bool:
        return self.database_url.startswith("sqlite:")

    @property
    def normalized_public_base_url(self) -> str:
        return normalize_public_base_url(self.public_base_url)

    def public_base_url_warnings(self) -> tuple[str, ...]:
        return validate_public_base_url(self.public_base_url, production=streamlit_cloud_detected())

    def prepare_directories(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.document_dir.mkdir(parents=True, exist_ok=True)
        self.template_dir.mkdir(parents=True, exist_ok=True)
        if self.revision_storage_backend == "local":
            self.revision_storage_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
