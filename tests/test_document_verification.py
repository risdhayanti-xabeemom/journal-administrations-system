from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import services.core as core
from config import normalize_public_base_url, validate_public_base_url
from models import (
    Base,
    DocumentStatus,
    DocumentType,
    EditorialStatus,
    Invoice,
    InvoiceStatus,
    Journal,
    LoADocument,
    LoAStatus,
    Payment,
    PaymentStatus,
    Receipt,
    Role,
    Submission,
    User,
)
from services.public_verification import (
    public_amounts_enabled,
    public_document_verification,
    public_document_verification_by_number,
    set_public_amount_visibility,
)
from services.verification_admin import generate_missing_verification_tokens


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db


@pytest.fixture()
def official_documents(session):
    journal = Journal(
        name="ELKOLIND",
        abbreviation="ELK",
        currency="IDR",
        default_apc=Decimal("300000"),
    )
    submission = Submission(
        journal=journal,
        ojs_submission_id="12345",
        manuscript_title="A Publicly Verifiable Article",
        corresponding_author="Example Author",
        email="private-author@example.test",
        editorial_status=EditorialStatus.ACCEPTED,
    )
    user = User(
        email="admin@example.test",
        display_name="Journal Administrator",
        password_hash="not-used-by-this-test",
        role=Role.SUPER_ADMIN,
    )
    session.add_all([journal, submission, user])
    session.flush()

    verifications = {}
    for kind, number in (
        (DocumentType.LOA, "058/SK/ELK/IX/2026"),
        (DocumentType.INVOICE, "INV/ELK/2026/0012"),
        (DocumentType.RECEIPT, "RCP/ELK/2026/0012"),
    ):
        verifications[kind] = core.create_verification(
            session,
            document_type=kind,
            journal=journal,
            submission=submission,
            document_number=number,
            issue_date=date(2026, 9, 23),
        )

    loa = LoADocument(
        journal=journal,
        submission=submission,
        verification=verifications[DocumentType.LOA],
        document_number="058/SK/ELK/IX/2026",
        version=1,
        issue_date=date(2026, 9, 23),
        status=LoAStatus.VALID,
    )
    invoice = Invoice(
        journal=journal,
        submission=submission,
        verification=verifications[DocumentType.INVOICE],
        invoice_number="INV/ELK/2026/0012",
        invoice_date=date(2026, 9, 23),
        due_date=date(2026, 10, 7),
        apc_amount=Decimal("300000"),
        discount=Decimal("0"),
        additional_charge=Decimal("0"),
        total_amount=Decimal("300000"),
        currency="IDR",
        status=InvoiceStatus.WAITING_PAYMENT,
        author_token_hash="author-payment-token-hash",
        author_token_expires_at=datetime.now(timezone.utc) + timedelta(days=90),
    )
    payment = Payment(
        invoice=invoice,
        payer_name="Example Author",
        payment_method="Bank transfer",
        payment_date=date(2026, 9, 23),
        amount_paid=Decimal("300000"),
        proof_path="private/proofs/never-public.pdf",
        proof_original_name="payment-proof.pdf",
        internal_notes="Private finance note",
        status=PaymentStatus.VERIFIED,
    )
    receipt = Receipt(
        journal=journal,
        invoice=invoice,
        payment=payment,
        verification=verifications[DocumentType.RECEIPT],
        receipt_number="RCP/ELK/2026/0012",
        issue_date=date(2026, 9, 23),
        amount=Decimal("300000"),
        authorized_person="Finance Officer",
    )
    session.add_all([loa, invoice, payment, receipt])
    session.commit()
    return {
        "journal": journal,
        "user": user,
        "loa": loa,
        "invoice": invoice,
        "payment": payment,
        "receipt": receipt,
    }


def _use_production_origin(monkeypatch) -> str:
    origin = "https://jasv1-journal.streamlit.app"
    monkeypatch.setattr(core, "settings", replace(core.settings, public_base_url=origin))
    return origin


def test_qr_payload_is_absolute_public_query_url_for_loa_invoice_and_receipt(
    monkeypatch, official_documents
):
    origin = _use_production_origin(monkeypatch)

    for name in ("loa", "invoice", "receipt"):
        token = official_documents[name].verification.token
        assert core.verification_url(token) == f"{origin}/?verify={token}"

    assert core.verification_url("abc123") == (
        "https://jasv1-journal.streamlit.app/?verify=abc123"
    )


def test_tokens_are_unique_unpredictable_and_reused_on_repeated_verification(
    session, monkeypatch, official_documents
):
    _use_production_origin(monkeypatch)
    documents = [official_documents[name] for name in ("loa", "invoice", "receipt")]
    initial_tokens = [document.verification.token for document in documents]
    initial_count = session.scalar(select(func.count()).select_from(core.DocumentVerification))

    assert len(set(initial_tokens)) == 3
    assert all(len(token) >= 40 for token in initial_tokens)
    assert all("12345" not in token for token in initial_tokens)

    first_urls = [core.verification_url(token) for token in initial_tokens]
    for token in initial_tokens:
        assert public_document_verification(session, token) is not None
        assert public_document_verification(session, token) is not None
    second_urls = [core.verification_url(document.verification.token) for document in documents]

    assert second_urls == first_urls
    assert [document.verification.token for document in documents] == initial_tokens
    assert session.scalar(select(func.count()).select_from(core.DocumentVerification)) == initial_count


def test_public_verification_valid_documents_and_receipt_payment_status(
    session, official_documents
):
    loa = public_document_verification(session, official_documents["loa"].verification.token)
    invoice = public_document_verification(session, official_documents["invoice"].verification.token)
    receipt = public_document_verification(session, official_documents["receipt"].verification.token)

    assert (loa["document_type"], loa["document_status"], loa["loa_status"]) == (
        "LOA",
        "VALID",
        "VALID",
    )
    assert (
        invoice["document_type"],
        invoice["document_status"],
        invoice["invoice_status"],
    ) == ("INVOICE", "VALID", "WAITING_PAYMENT")
    assert (
        receipt["document_type"],
        receipt["document_status"],
        receipt["payment_status"],
    ) == ("RECEIPT", "VALID", "VERIFIED")
    assert receipt["submission_id"] == "12345"


def test_invalid_verification_token_has_no_result(session, official_documents):
    assert public_document_verification(session, "invalid-unregistered-token") is None


def test_manual_number_lookup_requires_an_exact_document_number(session, official_documents):
    invoice = official_documents["invoice"]
    payload = public_document_verification_by_number(session, invoice.invoice_number)
    assert payload["document_type"] == "INVOICE"
    assert payload["document_number"] == invoice.invoice_number
    assert public_document_verification_by_number(session, "INV/ELK/2026") is None


@pytest.mark.parametrize(
    ("loa_status", "verification_status"),
    [
        (LoAStatus.REVOKED, DocumentStatus.REVOKED),
        (LoAStatus.SUPERSEDED, DocumentStatus.SUPERSEDED),
    ],
)
def test_public_loa_retains_revoked_and_superseded_status(
    session, official_documents, loa_status, verification_status
):
    loa = official_documents["loa"]
    original_token = loa.verification.token
    loa.status = loa_status
    loa.verification.document_status = verification_status
    session.commit()

    payload = public_document_verification(session, original_token)
    assert payload["document_status"] == verification_status.value
    assert payload["loa_status"] == loa_status.value
    assert loa.verification.token == original_token


def test_public_invoice_reports_cancelled_without_replacing_token(
    session, official_documents
):
    invoice = official_documents["invoice"]
    original_token = invoice.verification.token
    invoice.status = InvoiceStatus.CANCELLED
    invoice.verification.document_status = DocumentStatus.CANCELLED
    session.commit()

    payload = public_document_verification(session, original_token)
    assert payload["document_status"] == "CANCELLED"
    assert payload["invoice_status"] == "CANCELLED"
    assert invoice.verification.token == original_token


def test_revocation_and_cancellation_audit_verification_status_without_logging_tokens(
    session, official_documents
):
    loa = official_documents["loa"]
    invoice = official_documents["invoice"]
    loa_token = loa.verification.token
    invoice_token = invoice.verification.token

    core.revoke_loa(session, loa, official_documents["user"])
    core.cancel_invoice(session, invoice, official_documents["user"])
    session.commit()

    actions = {
        audit.action: audit
        for audit in session.scalars(
            select(core.AuditLog).where(
                core.AuditLog.action.in_(
                    {
                        "VERIFICATION_DOCUMENT_REVOKED",
                        "VERIFICATION_DOCUMENT_CANCELLED",
                    }
                )
            )
        )
    }
    assert set(actions) == {
        "VERIFICATION_DOCUMENT_REVOKED",
        "VERIFICATION_DOCUMENT_CANCELLED",
    }
    assert all(loa_token not in (audit.new_value or "") for audit in actions.values())
    assert all(invoice_token not in (audit.new_value or "") for audit in actions.values())
    assert public_document_verification(session, loa_token)["document_status"] == "REVOKED"
    assert public_document_verification(session, invoice_token)["document_status"] == "CANCELLED"


def test_public_payload_excludes_private_payment_and_contact_data(
    session, official_documents
):
    payload = public_document_verification(
        session, official_documents["receipt"].verification.token
    )
    forbidden_keys = {
        "email",
        "amount",
        "total_amount",
        "bank_account",
        "payment_proof",
        "proof_path",
        "internal_notes",
        "storage_path",
        "verification_token",
    }
    assert forbidden_keys.isdisjoint(payload)
    assert "private-author@example.test" not in repr(payload)
    assert "never-public.pdf" not in repr(payload)
    assert "Private finance note" not in repr(payload)


def test_public_amount_is_hidden_by_default_and_requires_journal_opt_in(
    session, official_documents
):
    journal = official_documents["journal"]
    invoice_token = official_documents["invoice"].verification.token
    receipt_token = official_documents["receipt"].verification.token

    assert public_amounts_enabled(session, journal.id) is False
    assert "amount" not in public_document_verification(session, invoice_token)
    assert "amount" not in public_document_verification(session, receipt_token)

    set_public_amount_visibility(session, journal.id, True)
    session.commit()

    assert public_amounts_enabled(session, journal.id) is True
    assert public_document_verification(session, invoice_token)["amount"] == "Rp 300.000"
    assert public_document_verification(session, receipt_token)["amount"] == "Rp 300.000"


def test_missing_token_repair_keeps_existing_tokens_and_audits_fingerprint_only(
    session, official_documents
):
    loa = official_documents["loa"]
    invoice = official_documents["invoice"]
    receipt = official_documents["receipt"]
    invoice_token = invoice.verification.token
    receipt_token = receipt.verification.token
    loa.verification.token = ""
    session.commit()

    repaired = generate_missing_verification_tokens(
        session,
        journal=official_documents["journal"],
        user=official_documents["user"],
    )
    session.commit()

    assert repaired == [loa.verification]
    assert len(loa.verification.token) >= 40
    assert invoice.verification.token == invoice_token
    assert receipt.verification.token == receipt_token
    audit = session.scalar(
        select(core.AuditLog).where(
            core.AuditLog.action == "MISSING_VERIFICATION_TOKEN_GENERATED"
        )
    )
    assert audit is not None
    assert loa.verification.token not in (audit.new_value or "")
    assert "token_fingerprint" in (audit.new_value or "")


def test_public_base_url_is_normalized_and_warns_for_unsafe_production_values():
    assert normalize_public_base_url("https://jas.example/app/?old=1#section") == "https://jas.example/app"
    assert validate_public_base_url("https://jas.example", production=True) == ()
    warnings = validate_public_base_url("http://localhost:8501/?old=1", production=True)
    assert any("HTTPS" in warning for warning in warnings)
    assert any("not configured for production" in warning for warning in warnings)
    assert any("query parameters" in warning for warning in warnings)
