"""Private revision-file storage boundary; local backend is development-compatible."""

from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlparse
from zipfile import BadZipFile, ZipFile

from lxml import etree
from pypdf import PdfReader

from config import settings


class RevisionFileError(ValueError):
    pass


class StorageUnavailable(RuntimeError):
    pass


class RevisionStorage(Protocol):
    def put(self, job_id: uuid.UUID, kind: str, content: bytes, suffix: str) -> tuple[str, str]: ...
    def read(self, key: str) -> bytes: ...


class LocalRevisionStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root):
            raise RevisionFileError("Invalid private storage key.")
        return candidate

    def put(self, job_id: uuid.UUID, kind: str, content: bytes, suffix: str) -> tuple[str, str]:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", kind) or suffix not in {".docx", ".pdf", ".txt", ".json", ".xlsx"}:
            raise RevisionFileError("Invalid revision artifact type.")
        key = f"{job_id}/{kind}/{uuid.uuid4().hex}{suffix}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(content)
        return key, hashlib.sha256(content).hexdigest()

    def read(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise RevisionFileError("Revision file is unavailable from private storage.")
        return path.read_bytes()


class SupabaseRevisionStorage:
    """Server-side access to an administrator-created PRIVATE Storage bucket."""

    def __init__(self, project_url: str, service_role_key: str, bucket: str):
        parsed = urlparse(project_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise StorageUnavailable("Revision Supabase URL must be an HTTPS project URL without credentials.")
        if not service_role_key or not re.fullmatch(r"[a-zA-Z0-9_-]{3,63}", bucket):
            raise StorageUnavailable("Configure the revision Storage service key and a valid private bucket name.")
        self.base = project_url.rstrip("/") + "/storage/v1"
        self.key = service_role_key
        self.bucket = bucket

    def _request(self, method: str, url: str, *, content: bytes | None = None, mime: str | None = None) -> bytes:
        headers = {"Authorization": f"Bearer {self.key}", "apikey": self.key}
        if mime:
            headers.update({"Content-Type": mime, "x-upsert": "false"})
        request = urllib.request.Request(url, data=content, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read(settings.revision_max_file_bytes + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            # Never include upstream URLs or responses: they may contain unpublished content.
            # The HTTP status code / network reason is safe to show and makes misconfiguration diagnosable.
            detail = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            raise StorageUnavailable(f"Private revision storage is unavailable ({method} {detail}); no public download was created.") from exc

    def _object_url(self, key: str, *, authenticated: bool) -> str:
        if not re.fullmatch(r"[0-9a-f-]{36}/[a-z][a-z0-9_]*/[0-9a-f]{32}\.(?:docx|pdf|txt|json|xlsx)", key):
            raise RevisionFileError("Invalid private revision storage key.")
        prefix = "object/authenticated" if authenticated else "object"
        return f"{self.base}/{prefix}/{quote(self.bucket)}/{quote(key, safe='/')}"

    def _ensure_private_bucket(self) -> None:
        try:
            metadata = json.loads(self._request("GET", f"{self.base}/bucket/{quote(self.bucket)}"))
        except (ValueError, TypeError) as exc:
            raise StorageUnavailable("Private revision bucket metadata could not be verified.") from exc
        if not isinstance(metadata, dict) or metadata.get("public") is not False:
            raise StorageUnavailable("Revision bucket must be PRIVATE. No unpublished document was uploaded or downloaded.")

    def put(self, job_id: uuid.UUID, kind: str, content: bytes, suffix: str) -> tuple[str, str]:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", kind) or suffix not in {".docx", ".pdf", ".txt", ".json", ".xlsx"}:
            raise RevisionFileError("Invalid revision artifact type.")
        key = f"{job_id}/{kind}/{uuid.uuid4().hex}{suffix}"
        mime = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".pdf": "application/pdf", ".txt": "text/plain", ".json": "application/json",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[suffix]
        self._ensure_private_bucket()
        self._request("POST", self._object_url(key, authenticated=False), content=content, mime=mime)
        return key, hashlib.sha256(content).hexdigest()

    def read(self, key: str) -> bytes:
        self._ensure_private_bucket()
        content = self._request("GET", self._object_url(key, authenticated=True))
        if len(content) > settings.revision_max_file_bytes:
            raise RevisionFileError("Private revision object exceeds the configured size limit.")
        return content


def get_revision_storage() -> RevisionStorage:
    if settings.revision_storage_backend == "local":
        return LocalRevisionStorage(settings.revision_storage_dir)
    if settings.revision_storage_backend == "supabase":
        if not settings.revision_supabase_url or not settings.revision_supabase_service_role_key:
            raise StorageUnavailable("Configure REVISION_SUPABASE_URL and REVISION_SUPABASE_SERVICE_ROLE_KEY in Streamlit Secrets.")
        return SupabaseRevisionStorage(settings.revision_supabase_url, settings.revision_supabase_service_role_key,
                                       settings.revision_supabase_bucket)
    raise StorageUnavailable("Revision storage backend is not configured. Set REVISION_STORAGE_BACKEND to a supported private backend.")


def safe_filename(filename: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "_", filename.replace("\\", "/").split("/")[-1])[:255]


def validate_revision_upload(filename: str, content: bytes, *, manuscript: bool = False) -> str:
    suffix = Path(filename).suffix.lower()
    allowed = {".docx"} if manuscript else {".docx", ".pdf", ".txt"}
    if suffix not in allowed:
        if manuscript and suffix == ".pdf":
            raise RevisionFileError("PDF manuscript cannot be safely patched while preserving editable journal formatting. Upload the original DOCX for final revision.")
        raise RevisionFileError("Unsupported revision file type. Manuscripts require DOCX; reviewer files may be DOCX, PDF, or TXT.")
    if not content or len(content) > settings.revision_max_file_bytes:
        raise RevisionFileError(f"Revision file must be between 1 byte and {settings.revision_max_file_bytes} bytes.")
    if suffix == ".docx":
        if not content.startswith(b"PK"):
            raise RevisionFileError("DOCX signature does not match the filename.")
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                    raise RevisionFileError("DOCX package is missing required Word parts.")
                if len(names) > 2000 or sum(item.file_size for item in archive.infolist()) > 150_000_000:
                    raise RevisionFileError("DOCX package exceeds safe extraction limits.")
                if any(name.lower().endswith("vbaproject.bin") or name.startswith("/") or ".." in Path(name).parts for name in names):
                    raise RevisionFileError("Macro-enabled or unsafe DOCX packages are not accepted.")
                for name in names:
                    if name.endswith(".rels"):
                        root = etree.fromstring(archive.read(name), parser=etree.XMLParser(resolve_entities=False, no_network=True))
                        for node in root:
                            if node.get("TargetMode") != "External":
                                continue
                            relationship_type = node.get("Type", "")
                            target = node.get("Target", "")
                            scheme = urlparse(target).scheme.lower()
                            if not relationship_type.endswith("/hyperlink") or scheme not in {"http", "https", "mailto"}:
                                raise RevisionFileError("DOCX with external embedded resources is not accepted; ordinary web hyperlinks are allowed.")
        except BadZipFile as exc:
            raise RevisionFileError("DOCX package is damaged.") from exc
    elif suffix == ".pdf":
        if not content.startswith(b"%PDF"):
            raise RevisionFileError("PDF signature does not match the filename.")
        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted or len(reader.pages) > 200:
                raise RevisionFileError("Reviewer PDF must be unencrypted and contain at most 200 pages.")
        except RevisionFileError:
            raise
        except Exception as exc:
            raise RevisionFileError("Reviewer PDF cannot be read safely.") from exc
    else:
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise RevisionFileError("Reviewer TXT must be UTF-8 encoded.") from exc
    return suffix
