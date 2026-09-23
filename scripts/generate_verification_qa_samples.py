from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import qrcode


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import services.core as core
import services.docx_templates as docx_templates


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate three document-verification QR QA samples.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--public-base-url", required=True)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    qa_settings = replace(
        core.settings,
        public_base_url=args.public_base_url,
        document_dir=output_dir,
    )
    core.settings = qa_settings
    docx_templates.settings = qa_settings

    loa_token = "qa-loa-token-20260924"
    invoice_token = "qa-invoice-token-20260924"
    receipt_token = "qa-receipt-token-20260924"
    loa_url = core.verification_url(loa_token)
    invoice_url = core.verification_url(invoice_token)
    receipt_url = core.verification_url(receipt_token)

    loa_docx = output_dir / "Sample_Verification_LoA.docx"
    loa_pdf = output_dir / "Sample_Verification_LoA.pdf"
    try:
        loa = docx_templates.generate_from_template(
            PROJECT_ROOT / "templates" / "loa" / "elkolind" / "v1" / "LoA Elkolind-2026.docx",
            loa_docx,
            {
            "loa_number": "058/SK/ELK/IX/2026",
            "recipient_name": "Dr. Siti Rahmawati",
            "ojs_submission_id": "10421",
            "authors": "Siti Rahmawati, Budi Santoso, Dewi Kartika",
            "article_title": "Sistem Pemantauan Energi Industri Berbasis Internet of Things dan Analitik Real-Time",
            "volume": "13",
            "issue": "3",
            "publication_month": "September",
            "publication_year": "2026",
            "loa_date": "24 September 2026",
            },
            output_pdf=loa_pdf,
            qr_url=loa_url,
            qr_size_mm=18,
            qr_placement="bottom-right",
            qr_label="Scan to Verify LoA",
        )
        generated_loa_pdf = loa.pdf_path
    except docx_templates.PDFConverterUnavailable as exc:
        # This is the production behavior too: retain the valid DOCX and report
        # converter configuration honestly instead of pretending a PDF exists.
        generated_loa_pdf = None
        print(f"LoA PDF conversion unavailable: {exc}")

    qris_path = output_dir / "Sample_Payment_QRIS.png"
    qrcode.make("00020101021126670016COM.NOBUBANK.WWW01189360050300000879140214599415941503930303UMI51440014ID.CO.QRIS.WWW0215QA-PAYMENT-ONLY53033605802ID5911JAS QA ONLY6006MALANG6304ABCD").save(qris_path)
    journal = SimpleNamespace(
        name="Jurnal Elektronika dan Otomasi Industri (ELKOLIND)",
        publisher="Politeknik Negeri Malang",
        address="Jl. Soekarno Hatta No. 9, Malang",
        logo_path=None,
        bank_name="Bank Example",
        bank_account="1234567890",
        account_holder="ELKOLIND Journal",
        qris_path=str(qris_path),
        invoice_template="Include the invoice number in the payment reference.",
        receipt_template="Payment has been received and verified by the journal administration.",
        signature_path=None,
        stamp_path=None,
    )
    submission = SimpleNamespace(
        ojs_submission_id="10421",
        corresponding_author="Dr. Siti Rahmawati",
        affiliation="Example University",
        manuscript_title="Sistem Pemantauan Energi Industri Berbasis Internet of Things dan Analitik Real-Time",
    )
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        journal=journal,
        submission=submission,
        verification=SimpleNamespace(token=invoice_token),
        invoice_number="INV/ELK/2026/0012",
        invoice_date=date(2026, 9, 24),
        due_date=date(2026, 10, 8),
        apc_amount=Decimal("300000"),
        discount=Decimal("0"),
        additional_charge=Decimal("0"),
        total_amount=Decimal("300000"),
        currency="IDR",
        payment_method="Bank transfer / QRIS",
        notes=None,
    )
    invoice_pdf = Path(core.generate_invoice_pdf(invoice))
    invoice_output = output_dir / "Sample_Verification_Invoice.pdf"
    if invoice_pdf != invoice_output:
        invoice_pdf.replace(invoice_output)

    payment = SimpleNamespace(
        payer_name="Dr. Siti Rahmawati",
        payment_date=date(2026, 9, 24),
        payment_method="Bank transfer",
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        journal=journal,
        invoice=invoice,
        payment=payment,
        verification=SimpleNamespace(token=receipt_token),
        receipt_number="RCP/ELK/2026/0012",
        issue_date=date(2026, 9, 24),
        amount=Decimal("300000"),
        authorized_person="Journal Finance Officer",
    )
    receipt_pdf = Path(core.generate_receipt_pdf(receipt))
    receipt_output = output_dir / "Sample_Verification_Receipt.pdf"
    if receipt_pdf != receipt_output:
        receipt_pdf.replace(receipt_output)

    print(f"LoA DOCX: {loa_docx} | PDF: {generated_loa_pdf or 'NOT GENERATED'} | QR: {loa_url}")
    print(f"Invoice PDF: {invoice_output} | verification QR: {invoice_url}")
    print(f"Receipt PDF: {receipt_output} | QR: {receipt_url}")
    print(f"Payment QRIS payload is separate: {qris_path}")


if __name__ == "__main__":
    main()
