from __future__ import annotations

import hashlib
import io
import secrets
from datetime import date

import qrcode
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    DocumentStatus,
    DocumentType,
    DocumentVerification,
    Invoice,
    InvoiceStatus,
    Journal,
    LoADocument,
    Receipt,
    Role,
    User,
)


def verification_qr_png(url: str) -> bytes:
    """Render a QR image without changing the document's persisted token."""
    buffer = io.BytesIO()
    qrcode.make(url).save(buffer, format="PNG")
    return buffer.getvalue()


def _fresh_token(session: Session) -> str:
    for _ in range(8):
        token = secrets.token_urlsafe(32)
        if session.scalar(
            select(DocumentVerification.id).where(DocumentVerification.token == token)
        ) is None:
            return token
    raise RuntimeError("A unique verification token could not be created.")


def _document_details(document) -> tuple[DocumentType, str, date, DocumentStatus]:
    if isinstance(document, LoADocument):
        status = DocumentStatus(document.status.value)
        return DocumentType.LOA, document.document_number, document.issue_date, status
    if isinstance(document, Invoice):
        status = (
            DocumentStatus.CANCELLED
            if document.status == InvoiceStatus.CANCELLED
            else DocumentStatus.VALID
        )
        return DocumentType.INVOICE, document.invoice_number, document.invoice_date, status
    if isinstance(document, Receipt):
        return DocumentType.RECEIPT, document.receipt_number, document.issue_date, DocumentStatus.VALID
    raise TypeError(f"Unsupported verification document: {type(document).__name__}")


def generate_missing_verification_tokens(
    session: Session,
    *,
    journal: Journal,
    user: User,
) -> list[DocumentVerification]:
    """Repair only legacy documents that have no token; never rotate existing tokens."""
    from services.core import assert_journal_access, log_audit

    assert_journal_access(
        user,
        journal.id,
        {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN},
        session,
    )
    documents = [
        *session.scalars(select(LoADocument).where(LoADocument.journal_id == journal.id)),
        *session.scalars(select(Invoice).where(Invoice.journal_id == journal.id)),
        *session.scalars(select(Receipt).where(Receipt.journal_id == journal.id)),
    ]
    repaired: list[DocumentVerification] = []
    for document in documents:
        verification = getattr(document, "verification", None)
        if verification is not None and (verification.token or "").strip():
            continue

        document_type, document_number, issue_date, status = _document_details(document)
        token = _fresh_token(session)
        if verification is None:
            verification = DocumentVerification(
                token=token,
                document_type=document_type,
                journal_id=journal.id,
                submission_id=document.submission_id
                if hasattr(document, "submission_id")
                else document.invoice.submission_id,
                document_number=document_number,
                document_status=status,
                issue_date=issue_date,
            )
            session.add(verification)
            session.flush()
            document.verification_id = verification.id
            document.verification = verification
        else:
            verification.token = token

        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        log_audit(
            session,
            action="MISSING_VERIFICATION_TOKEN_GENERATED",
            object_type="document_verification",
            object_id=verification.id,
            user_id=user.id,
            journal_id=journal.id,
            new={
                "document_type": document_type.value,
                "document_number": document_number,
                "token_fingerprint": fingerprint,
            },
        )
        repaired.append(verification)
    return repaired

