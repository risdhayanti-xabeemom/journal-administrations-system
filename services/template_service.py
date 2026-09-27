from __future__ import annotations

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
    extract_placeholders,
    temp_docx_file,
    validate_docx_bytes,
    validate_required_fields,
)
from services.revision_storage import RevisionStorage, get_revision_storage


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


def loa_template_bytes(template: DocumentTemplate, *, storage: RevisionStorage | None = None) -> bytes:
    """Read a master LoA template's DOCX bytes from private Storage."""
    return (storage or get_revision_storage()).read(template.storage_path)


def upload_loa_template(
    session: Session,
    journal: Journal,
    user: User,
    *,
    original_filename: str,
    content: bytes,
    activate: bool = True,
    storage: RevisionStorage | None = None,
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
    # Streamlit Cloud's local disk is not durable: validate against a throwaway temp
    # file, then persist the bytes in private Storage so the template survives reboots.
    with temp_docx_file(content) as path:
        validate_required_fields(path, journal.abbreviation)
        mapping = _default_mapping(path)
    key, digest = (storage or get_revision_storage()).put(journal.id, "loa_template", content, ".docx")
    if activate:
        current = active_loa_template(session, journal.id)
        if current:
            current.status = TemplateStatus.INACTIVE
            current.active_key = None
    template = DocumentTemplate(
        journal_id=journal.id,
        template_type="LOA",
        original_filename=Path(original_filename).name[:255],
        storage_path=key,
        version=version,
        status=TemplateStatus.ACTIVE if activate else TemplateStatus.INACTIVE,
        active_key=f"{journal.id}:LOA" if activate else None,
        uploaded_by=user.id,
        checksum=digest,
        field_mapping=mapping,
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
        new={"filename": template.original_filename, "version": version, "checksum": digest, "active": activate},
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


def save_field_mapping(session: Session, template: DocumentTemplate, user: User, mapping_text: str,
                       *, storage: RevisionStorage | None = None) -> None:
    try:
        mapping = json.loads(mapping_text)
    except json.JSONDecodeError as exc:
        raise TemplateError(f"Field mapping must be valid JSON: {exc}") from exc
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise TemplateError("Field mapping must be a JSON object of placeholder-to-source strings.")
    with temp_docx_file(loa_template_bytes(template, storage=storage)) as path:
        available = {field[2:-2] for field in extract_placeholders(path)}
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


def preview_master_template(session: Session, template: DocumentTemplate, user: User,
                            *, storage: RevisionStorage | None = None) -> Path:
    output = settings.document_dir / "template-previews" / f"loa-template-{template.id}-v{template.version}.pdf"
    with temp_docx_file(loa_template_bytes(template, storage=storage)) as source:
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
