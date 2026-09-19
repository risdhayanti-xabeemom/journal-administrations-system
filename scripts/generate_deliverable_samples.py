from __future__ import annotations

import shutil
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from services.core import generate_invoice_pdf, generate_receipt_pdf
from services.docx_templates import generate_from_template, sha256_file


SAMPLES = PROJECT_ROOT / "samples"


def generate_loas() -> None:
    elkolind = generate_from_template(
        PROJECT_ROOT / "templates" / "loa" / "elkolind" / "v1" / "LoA Elkolind-2026.docx",
        SAMPLES / "Sample_ELKOLIND_LoA.docx",
        {
            "loa_number": "058/SK/ELK/VIII/2026",
            "recipient_name": "Dr. Siti Rahmawati",
            "ojs_submission_id": "10421",
            "authors": "Siti Rahmawati, Budi Santoso, Dewi Kartika",
            "article_title": "Sistem Pemantauan Energi Industri Berbasis Internet of Things dan Analitik Real-Time",
            "volume": "13", "issue": "3", "publication_month": "September",
            "publication_year": "2026", "loa_date": "18 Agustus 2026",
        },
        output_pdf=SAMPLES / "Sample_ELKOLIND_LoA.pdf",
    )
    jasens = generate_from_template(
        PROJECT_ROOT / "templates" / "loa" / "jasens" / "v1" / "Draft_LoA_JASENS.docx",
        SAMPLES / "Sample_JASENS_LoA.docx",
        {
            "loa_number": "04/IX/JASENS/2026",
            "recipient_name": "Nadia Putri",
            "recipient_affiliation": "Department of Electrical Engineering, Example University",
            "article_title": "Adaptive Protection Coordination for Resilient Smart Distribution Networks",
            "authors": "Nadia Putri, Ahmad Fauzan, Maria Lestari",
            "volume": "7", "issue": "2", "publication_month": "September", "publication_year": "2026",
        },
        output_pdf=SAMPLES / "Sample_JASENS_LoA.pdf",
    )
    print(f"ELKOLIND pages={elkolind.page_count} sha256={elkolind.document_hash}")
    print(f"JASENS pages={jasens.page_count} sha256={jasens.document_hash}")


def generate_financial_documents() -> None:
    asset_dir = SAMPLES / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(PROJECT_ROOT / "templates" / "loa" / "elkolind" / "v1" / "LoA Elkolind-2026.docx") as archive:
        logo = asset_dir / "elkolind-official-header.jpeg"
        logo.write_bytes(archive.read("word/media/image1.jpeg"))
    journal = SimpleNamespace(
        name="Jurnal Elektronika dan Otomasi Industri (ELKOLIND)", publisher="Politeknik Negeri Malang",
        address="Jl. Soekarno Hatta No. 9, Malang", logo_path=str(logo), bank_name="Bank Example",
        bank_account="1234567890", account_holder="ELKOLIND Journal", qris_path=None,
        invoice_template="Please include the invoice number in the payment reference.",
        receipt_template="Payment has been received and verified by the journal administration.",
        signature_path=None, stamp_path=None,
    )
    submission = SimpleNamespace(
        ojs_submission_id="10421", corresponding_author="Dr. Siti Rahmawati",
        affiliation="Example University", manuscript_title="Sistem Pemantauan Energi Industri Berbasis Internet of Things dan Analitik Real-Time",
    )
    verification = SimpleNamespace(token="sample-verification-token")
    invoice = SimpleNamespace(
        id=uuid.uuid4(), journal=journal, submission=submission, verification=verification,
        invoice_number="INV/ELKOLIND/2026/0001", invoice_date=date(2026, 8, 18), due_date=date(2026, 9, 1),
        apc_amount=Decimal("300000"), discount=Decimal("0"), additional_charge=Decimal("0"),
        total_amount=Decimal("300000"), currency="IDR", payment_method="Bank transfer", notes=None,
    )
    invoice_path = Path(generate_invoice_pdf(invoice))
    shutil.copy2(invoice_path, SAMPLES / "Sample_Invoice.pdf")
    payment = SimpleNamespace(
        payer_name="Dr. Siti Rahmawati", payment_date=date(2026, 8, 20), payment_method="Bank transfer",
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(), journal=journal, invoice=invoice, payment=payment, verification=verification,
        receipt_number="RCP/ELKOLIND/2026/0001", issue_date=date(2026, 8, 20),
        amount=Decimal("300000"), authorized_person="Journal Finance Officer",
    )
    receipt_path = Path(generate_receipt_pdf(receipt))
    shutil.copy2(receipt_path, SAMPLES / "Sample_Receipt.pdf")
    print(f"Invoice sha256={sha256_file(SAMPLES / 'Sample_Invoice.pdf')}")
    print(f"Receipt sha256={sha256_file(SAMPLES / 'Sample_Receipt.pdf')}")


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    settings.prepare_directories()
    generate_loas()
    generate_financial_documents()


if __name__ == "__main__":
    main()
