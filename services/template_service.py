from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import settings
from models import DocumentTemplate, Journal, TemplateStatus, User
from services.docx_templates import (
    REQUIRED_FIELDS,
    TemplateError,
    convert_docx_to_pdf,
    escaped_filename,
    extract_placeholders,
    validate_docx_bytes,
    validate_required_fields,
)


def active_loa_template(session: Session, journal_id) -> DocumentTemplate | None:
    return session.scalar(
        select(DocumentTemplate).where(
            DocumentTemplate.journal_id == journal_id,
            DocumentTemplate.template_type == "LOA",
            DocumentTemplate.status == TemplateStatus.ACTIVE,
        )
    )


def _default_mapping(path: Path) -> str:
    mapping = {field[2:-2]: field[2:-2] for field in sorted(extract_placeholders(path))}
    return json.dumps(mapping, ensure_ascii=False, sort_keys=True)


def upload_loa_template(
    session: Session,
    journal: Journal,
    user: User,
    *,
    original_filename: str,
    content: bytes,
    activate: bool = True,
) -> DocumentTemplate:
    if Path(original_filename).suffix.lower() != ".docx":
        raise TemplateError("Master LoA templates must be uploaded as DOCX files.")
    validate_docx_bytes(content)
    version = (session.scalar(
        select(func.max(DocumentTemplate.version)).where(
            DocumentTemplate.journal_id == journal.id,
            DocumentTemplate.template_type == "LOA",
        )
    ) or 0) + 1
    folder = settings.template_dir / "loa" / journal.abbreviation.lower() / f"v{version}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / escaped_filename(Path(original_filename).name)
    target.write_bytes(content)
    try:
        validate_required_fields(target, journal.abbreviation)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    checksum = hashlib.sha256(content).hexdigest()
    if activate:
        current = active_loa_template(session, journal.id)
        if current:
            current.status = TemplateStatus.INACTIVE
            current.active_key = None
    template = DocumentTemplate(
        journal_id=journal.id,
        template_type="LOA",
        original_filename=Path(original_filename).name[:255],
        storage_path=str(target),
        version=version,
        status=TemplateStatus.ACTIVE if activate else TemplateStatus.INACTIVE,
        active_key=f"{journal.id}:LOA" if activate else None,
        uploaded_by=user.id,
        checksum=checksum,
        field_mapping=_default_mapping(target),
    )
    session.add(template)
    session.flush()
    from services.core import log_audit
    log_audit(
        session,
        action="LOA_TEMPLATE_UPLOADED",
        object_type="document_template",
        object_id=template.id,
        user_id=user.id,
        journal_id=journal.id,
        new={"filename": template.original_filename, "version": version, "checksum": checksum, "active": activate},
    )
    return template


def activate_loa_template(session: Session, template: DocumentTemplate, user: User) -> None:
    current = active_loa_template(session, template.journal_id)
    if current and current.id == template.id:
        return
    if current:
        current.status = TemplateStatus.INACTIVE
        current.active_key = None
    template.status = TemplateStatus.ACTIVE
    template.active_key = f"{template.journal_id}:LOA"
    from services.core import log_audit
    log_audit(
        session,
        action="LOA_TEMPLATE_ACTIVATED",
        object_type="document_template",
        object_id=template.id,
        user_id=user.id,
        journal_id=template.journal_id,
        previous={"template_id": str(current.id), "version": current.version} if current else None,
        new={"template_id": str(template.id), "version": template.version},
    )


def save_field_mapping(session: Session, template: DocumentTemplate, user: User, mapping_text: str) -> None:
    try:
        mapping = json.loads(mapping_text)
    except json.JSONDecodeError as exc:
        raise TemplateError(f"Field mapping must be valid JSON: {exc}") from exc
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise TemplateError("Field mapping must be a JSON object of placeholder-to-source strings.")
    available = {field[2:-2] for field in extract_placeholders(template.storage_path)}
    missing = sorted(available - set(mapping))
    if missing:
        raise TemplateError(f"Mapping is missing placeholders: {', '.join(missing)}")
    allowed_sources = {
        "loa_number", "recipient_name", "recipient_affiliation", "ojs_submission_id", "authors",
        "article_title", "volume", "issue", "publication_month", "publication_year", "loa_date",
        "journal_url", "journal_email", "verification_qr",
    }
    invalid = sorted(set(mapping.values()) - allowed_sources)
    if invalid:
        raise TemplateError(f"Unknown mapping data sources: {', '.join(invalid)}")
    previous = template.field_mapping
    template.field_mapping = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
    from services.core import log_audit
    log_audit(
        session,
        action="LOA_TEMPLATE_MAPPING_CHANGED",
        object_type="document_template",
        object_id=template.id,
        user_id=user.id,
        journal_id=template.journal_id,
        previous=previous,
        new=template.field_mapping,
    )


def mapped_template_values(template: DocumentTemplate, context: dict[str, object]) -> dict[str, object]:
    mapping = json.loads(template.field_mapping or "{}")
    return {placeholder: context.get(source, "") for placeholder, source in mapping.items()}


def preview_master_template(session: Session, template: DocumentTemplate, user: User) -> Path:
    source = Path(template.storage_path)
    output = settings.document_dir / "template-previews" / f"loa-template-{template.id}-v{template.version}.pdf"
    template.page_count = convert_docx_to_pdf(source, output)
    template.preview_pdf_path = str(output)
    from services.core import log_audit
    log_audit(
        session,
        action="LOA_TEMPLATE_PREVIEWED",
        object_type="document_template",
        object_id=template.id,
        user_id=user.id,
        journal_id=template.journal_id,
        new={"page_count": template.page_count},
    )
    return output


def required_fields_for(journal: Journal) -> set[str]:
    return REQUIRED_FIELDS.get(journal.abbreviation.upper(), set())
