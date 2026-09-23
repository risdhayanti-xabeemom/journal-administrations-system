from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    DocumentStatus,
    DocumentType,
    DocumentVerification,
    Invoice,
    LoADocument,
    Receipt,
    SystemSetting,
)


_TOKEN_RE = re.compile(r"[A-Za-z0-9._~-]+")
_AMOUNT_SETTING_PREFIX = "public_verification_show_amount:"


def _amount_setting_key(journal_id) -> str:
    return f"{_AMOUNT_SETTING_PREFIX}{journal_id}"


def public_amounts_enabled(session: Session, journal_id) -> bool:
    """Return the journal-level opt-in for public invoice/receipt amounts."""
    setting = session.get(SystemSetting, _amount_setting_key(journal_id))
    return bool(setting and (setting.value or "").strip().lower() in {"1", "true", "yes", "on"})


def set_public_amount_visibility(session: Session, journal_id, enabled: bool) -> None:
    """Persist the privacy setting without adding a journal-schema column."""
    key = _amount_setting_key(journal_id)
    setting = session.get(SystemSetting, key)
    if setting is None:
        session.add(SystemSetting(key=key, value="true" if enabled else "false"))
    else:
        setting.value = "true" if enabled else "false"


def _public_money(value, currency: str) -> str:
    amount = Decimal(value)
    if currency.upper() == "IDR":
        return f"Rp {int(amount.quantize(Decimal('1'))):,}".replace(",", ".")
    return f"{currency.upper()} {amount:,.2f}"


def public_document_verification(session: Session, token: str) -> dict | None:
    """Return the privacy-safe public view for one persisted verification token."""
    candidate = (token or "").strip()
    if not candidate or len(candidate) > 128 or not _TOKEN_RE.fullmatch(candidate):
        return None
    record = session.scalar(select(DocumentVerification).where(DocumentVerification.token == candidate))
    return _payload(session, record) if record else None


def public_document_verification_by_number(session: Session, document_number: str) -> dict | None:
    """Resolve an exact document number without supporting partial enumeration."""
    candidate = (document_number or "").strip()
    if not candidate or len(candidate) > 255:
        return None
    records = list(
        session.scalars(
            select(DocumentVerification)
            .where(func.lower(DocumentVerification.document_number) == candidate.lower())
            .order_by(DocumentVerification.created_at.desc())
        )
    )
    if not records:
        return None
    valid = [record for record in records if record.document_status == DocumentStatus.VALID]
    if len(valid) > 1:
        # An exact number that resolves to multiple current documents is ambiguous;
        # returning no result avoids disclosing unrelated records.
        return None
    record = valid[0] if valid else (records[0] if len(records) == 1 else None)
    if record is None:
        return None
    payload = _payload(session, record)
    payload["has_previous_versions"] = len(records) > 1
    return payload


def _payload(session: Session, record: DocumentVerification) -> dict:
    payload = {
        "document_type": record.document_type.value,
        "journal": record.journal.name,
        "document_number": record.document_number,
        "submission_id": record.submission.ojs_submission_id or "Not recorded",
        "article_title": record.submission.manuscript_title,
        "author": record.submission.corresponding_author,
        "issue_date": record.issue_date.isoformat(),
        "document_status": record.document_status.value,
    }
    if record.document_type == DocumentType.LOA:
        loa = session.scalar(select(LoADocument).where(LoADocument.verification_id == record.id))
        payload["loa_status"] = loa.status.value if loa else record.document_status.value
        payload["acceptance_status"] = record.submission.editorial_status.value
    elif record.document_type == DocumentType.INVOICE:
        invoice = session.scalar(select(Invoice).where(Invoice.verification_id == record.id))
        payload["invoice_status"] = invoice.status.value if invoice else record.document_status.value
        if invoice and public_amounts_enabled(session, record.journal_id):
            payload["amount"] = _public_money(invoice.total_amount, invoice.currency)
    elif record.document_type == DocumentType.RECEIPT:
        receipt = session.scalar(select(Receipt).where(Receipt.verification_id == record.id))
        payload["payment_status"] = receipt.payment.status.value if receipt and receipt.payment else None
        if receipt and public_amounts_enabled(session, record.journal_id):
            payload["amount"] = _public_money(receipt.amount, receipt.invoice.currency)
    return payload
