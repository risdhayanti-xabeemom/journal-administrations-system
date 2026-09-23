from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import qrcode
from pypdf import PdfReader

import services.core as core


def _journal(qris_path: str | None):
    return SimpleNamespace(
        name="ELKOLIND",
        publisher="Test Publisher",
        address="Test Address",
        logo_path=None,
        bank_name="Test Bank",
        bank_account="1234567890",
        account_holder="Journal",
        qris_path=qris_path,
        invoice_template="Pay using the configured channel.",
        receipt_template="Payment received.",
        signature_path=None,
        stamp_path=None,
    )


def _submission():
    return SimpleNamespace(
        ojs_submission_id="12345",
        corresponding_author="Example Author",
        affiliation="Example University",
        manuscript_title="Example Article",
    )


def test_invoice_keeps_payment_qris_separate_from_verification_qr(tmp_path, monkeypatch):
    monkeypatch.setattr(
        core,
        "settings",
        replace(
            core.settings,
            public_base_url="https://jasv1-journal.streamlit.app",
            document_dir=tmp_path,
        ),
    )
    qris_path = tmp_path / "payment-qris.png"
    qrcode.make("PAYMENT-QRIS-PAYLOAD-ONLY").save(qris_path)
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        journal=_journal(str(qris_path)),
        submission=_submission(),
        verification=SimpleNamespace(token="invoice-token"),
        invoice_number="INV/ELK/2026/0012",
        invoice_date=date(2026, 9, 24),
        due_date=date(2026, 10, 8),
        apc_amount=Decimal("300000"),
        discount=Decimal("0"),
        additional_charge=Decimal("0"),
        total_amount=Decimal("300000"),
        currency="IDR",
        payment_method="QRIS",
        notes=None,
    )
    qr_payloads: list[str] = []
    original_qr_image = core._qr_image

    def capture_verification_qr(url: str):
        qr_payloads.append(url)
        return original_qr_image(url)

    monkeypatch.setattr(core, "_qr_image", capture_verification_qr)
    pdf_path = Path(core.generate_invoice_pdf(invoice))

    assert qr_payloads == [
        "https://jasv1-journal.streamlit.app/?verify=invoice-token"
    ]
    text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    assert "Scan to Pay" in text
    assert "Scan to Verify Invoice" in text


def test_receipt_contains_only_its_persisted_verification_url(tmp_path, monkeypatch):
    monkeypatch.setattr(
        core,
        "settings",
        replace(
            core.settings,
            public_base_url="https://jasv1-journal.streamlit.app",
            document_dir=tmp_path,
        ),
    )
    invoice = SimpleNamespace(
        invoice_number="INV/ELK/2026/0012",
        submission=_submission(),
        currency="IDR",
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        journal=_journal(None),
        invoice=invoice,
        payment=SimpleNamespace(
            payer_name="Example Author",
            payment_date=date(2026, 9, 24),
            payment_method="Bank transfer",
        ),
        verification=SimpleNamespace(token="receipt-token"),
        receipt_number="RCP/ELK/2026/0012",
        issue_date=date(2026, 9, 24),
        amount=Decimal("300000"),
        authorized_person="Finance Officer",
    )
    qr_payloads: list[str] = []
    original_qr_image = core._qr_image

    def capture_verification_qr(url: str):
        qr_payloads.append(url)
        return original_qr_image(url)

    monkeypatch.setattr(core, "_qr_image", capture_verification_qr)
    pdf_path = Path(core.generate_receipt_pdf(receipt))

    assert qr_payloads == [
        "https://jasv1-journal.streamlit.app/?verify=receipt-token"
    ]
    text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    assert "Scan to Verify Receipt" in text
    assert "Scan to Pay" not in text
