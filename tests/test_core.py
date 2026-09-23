from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile
from lxml import etree


TEST_ROOT = tempfile.mkdtemp(prefix="jas-test-")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DOCUMENT_OUTPUT_DIR", os.path.join(TEST_ROOT, "documents"))
os.environ.setdefault("PRIVATE_UPLOAD_DIR", os.path.join(TEST_ROOT, "uploads"))
os.environ.setdefault("TEMPLATE_STORAGE_DIR", os.path.join(TEST_ROOT, "templates"))
os.environ.setdefault("PUBLIC_BASE_URL", "https://journals.example.test")

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import services.core as core
from config import settings
from models import (
    Author, Base, DocumentSequence, DocumentTemplate, DocumentType, EditorialStatus,
    InvoiceStatus, Journal, LoAStatus, PaymentStatus, PublicationStatus, Role,
    Submission, TemplateStatus, User,
)
from services.core import (
    BusinessRuleError, calculate_invoice_total, export_workbook, generate_loa_preview,
    hash_password, issue_invoice, issue_loa, issue_receipt, money,
    next_document_number, public_loa_verification_by_number, public_verification, submit_payment,
    update_editorial_status, update_publication_status, validate_payment_upload,
    verify_password, verify_payment,
)
from services.docx_templates import GeneratedLoA, extract_placeholders, generate_from_template
from services.template_service import active_loa_template, upload_loa_template


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ELKOLIND_TEMPLATE = PROJECT_ROOT / "templates" / "loa" / "elkolind" / "v1" / "LoA Elkolind-2026.docx"
JASENS_TEMPLATE = PROJECT_ROOT / "templates" / "loa" / "jasens" / "v1" / "Draft_LoA_JASENS.docx"


def package_text(path: Path) -> str:
    with ZipFile(path) as archive:
        return "".join(etree.fromstring(archive.read("word/document.xml")).itertext())


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db


@pytest.fixture()
def records(session):
    journal = Journal(
        name="Test Journal", abbreviation="TEST", publisher="Test Publisher",
        editor_in_chief="Dr. Editor", default_apc=Decimal("1000000.00"),
        currency="IDR", loa_number_format="{sequence:03d}/SK/TEST/{roman_month}/{year}",
    )
    user = User(
        email="admin@example.test", display_name="Admin",
        password_hash=hash_password("correct horse battery staple"), role=Role.SUPER_ADMIN,
    )
    session.add_all([journal, user])
    session.flush()
    submission = Submission(
        journal_id=journal.id, ojs_submission_id="123",
        manuscript_title="A deliberately long manuscript title & <limits> for document wrapping verification",
        corresponding_author="A. Author", email="author@example.test",
        affiliation="Example University", date_submitted=date.today(),
        editorial_status=EditorialStatus.ACCEPTED, date_accepted=date.today(),
        planned_volume="14", planned_issue="1", planned_publication_month="September",
        planned_year=2027, created_by=user.id,
    )
    session.add(submission)
    session.flush()
    session.add_all([
        Author(submission_id=submission.id, name="A. Author", email=submission.email, affiliation="Example University", is_corresponding=True, position=1),
        Author(submission_id=submission.id, name="B. Author", affiliation="Second University", is_corresponding=False, position=2),
    ])
    template = DocumentTemplate(
        journal_id=journal.id, template_type="LOA", original_filename=JASENS_TEMPLATE.name,
        storage_path=str(JASENS_TEMPLATE), version=1, status=TemplateStatus.ACTIVE,
        active_key=f"{journal.id}:LOA", uploaded_by=user.id, checksum="test-checksum",
        field_mapping=(
            '{"article_title":"article_title","authors":"authors","issue":"issue",'
            '"loa_number":"loa_number","publication_month":"publication_month",'
            '"publication_year":"publication_year","recipient_affiliation":"recipient_affiliation",'
            '"recipient_name":"recipient_name","volume":"volume"}'
        ),
    )
    session.add(template)
    session.commit()
    return journal, user, submission


@pytest.fixture()
def fake_loa_renderer(monkeypatch):
    def render(template, submission, *, document_number, printed_date, output_stem, overrides=None, verification_token=None):
        output_dir = settings.document_dir / "tests"
        output_dir.mkdir(parents=True, exist_ok=True)
        docx = output_dir / f"{output_stem}.docx"
        pdf = output_dir / f"{output_stem}.pdf"
        docx.write_bytes(JASENS_TEMPLATE.read_bytes())
        pdf.write_bytes(b"%PDF-1.7\n% deterministic test output\n")
        return GeneratedLoA(docx, pdf, 1, "f" * 64)

    monkeypatch.setattr(core, "_generate_template_loa", render)


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first != second
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("incorrect password", first)
    with pytest.raises(ValueError):
        hash_password("too-short")


def test_invoice_formula_currency_and_upload_validation():
    assert calculate_invoice_total(Decimal("100"), Decimal("10"), Decimal("2.5")) == Decimal("92.50")
    assert money(Decimal("300000.00"), "IDR") == "Rp 300.000"
    with pytest.raises(BusinessRuleError):
        calculate_invoice_total(Decimal("10"), Decimal("20"), Decimal("0"))
    assert validate_payment_upload("proof.pdf", b"%PDF-1.7\n") == ".pdf"
    with pytest.raises(BusinessRuleError):
        validate_payment_upload("proof.pdf", b"not a pdf")


def test_numbering_roman_months_and_year_rollover(session, records):
    journal, _, _ = records
    numbers = [next_document_number(session, journal, DocumentType.LOA, date(2026, month, 1)) for month in (9, 10, 11, 12)]
    next_year = next_document_number(session, journal, DocumentType.LOA, date(2027, 1, 1))
    session.commit()
    assert numbers == [
        "001/SK/TEST/IX/2026", "002/SK/TEST/X/2026",
        "003/SK/TEST/XI/2026", "004/SK/TEST/XII/2026",
    ]
    assert next_year == "001/SK/TEST/I/2027"


def test_template_upload_versions_and_retains_previous(session):
    journal = Journal(name="JASENS", abbreviation="JASENS", currency="IDR")
    user = User(email="owner@example.test", display_name="Owner", password_hash=hash_password("long secure password"), role=Role.SUPER_ADMIN)
    session.add_all([journal, user])
    session.flush()
    first = upload_loa_template(session, journal, user, original_filename="jasens-v1.docx", content=JASENS_TEMPLATE.read_bytes())
    second = upload_loa_template(session, journal, user, original_filename="jasens-v2.docx", content=JASENS_TEMPLATE.read_bytes())
    session.commit()
    assert first.version == 1 and second.version == 2
    assert first.status == TemplateStatus.INACTIVE and second.status == TemplateStatus.ACTIVE
    assert Path(first.storage_path).is_file() and Path(second.storage_path).is_file()
    assert active_loa_template(session, journal.id).id == second.id


def test_elkolind_dynamic_replacement_and_static_editor_preserved(tmp_path):
    output = tmp_path / "elkolind-sample.docx"
    generate_from_template(ELKOLIND_TEMPLATE, output, {
        "loa_number": "058/SK/ELK/VIII/2026", "recipient_name": "Dr. Recipient",
        "ojs_submission_id": "9911", "authors": "First Author, Second Author",
        "article_title": "A Long but Complete Manuscript Title", "volume": "13", "issue": "3",
        "publication_month": "September", "publication_year": "2026", "loa_date": "18 Agustus 2026",
    })
    text = package_text(output)
    for expected in ("058/SK/ELK/VIII/2026", "Dr. Recipient", "9911", "First Author, Second Author", "Volume 13", "Nomor 3", "September", "18 Agustus 2026"):
        assert expected in text
    assert "Hari Kurnia Safitri" in text
    assert "197307132002122002" in text
    assert not extract_placeholders(output)


def test_jasens_dynamic_replacement_and_static_editor_preserved(tmp_path):
    output = tmp_path / "jasens-sample.docx"
    generate_from_template(JASENS_TEMPLATE, output, {
        "loa_number": "04/IX/JASENS/2026", "recipient_name": "Recipient Name",
        "recipient_affiliation": "Affiliation Name", "article_title": "Complete JASENS Article Title",
        "authors": "Author One, Author Two", "volume": "7", "issue": "2",
        "publication_month": "September", "publication_year": "2026",
    })
    text = package_text(output)
    for expected in ("04/IX/JASENS/2026", "Recipient Name", "Affiliation Name", "Complete JASENS Article Title", "Author One, Author Two", "Volume 7", "No. 2", "September 2026"):
        assert expected in text
    assert "Prof. Dr. Ratna Ika Putri" in text
    assert not extract_placeholders(output)


def test_optional_loa_qr_is_embedded_without_replacing_official_media(tmp_path):
    output = tmp_path / "jasens-with-qr.docx"
    verification_url = "https://journals.example.test/?verify=test-token"
    generate_from_template(JASENS_TEMPLATE, output, {
        "loa_number": "04/IX/JASENS/2026", "recipient_name": "Recipient Name",
        "recipient_affiliation": "Affiliation Name", "article_title": "Article Title",
        "authors": "Author One, Author Two", "volume": "7", "issue": "2",
        "publication_month": "September", "publication_year": "2026",
    }, qr_url=verification_url, qr_size_mm=18, qr_placement="bottom-right", qr_label="Scan to Verify LoA")
    with ZipFile(JASENS_TEMPLATE) as original, ZipFile(output) as generated:
        assert "word/media/verification-qr.png" in generated.namelist()
        assert b"Scan to Verify LoA" in generated.read("word/document.xml")
        for name in ("word/media/image1.jpeg", "word/media/image2.jpeg"):
            assert generated.read(name) == original.read(name)


def test_preview_does_not_consume_number_or_create_verification(session, records, fake_loa_renderer):
    _, user, submission = records
    before = session.scalar(select(func.count()).select_from(DocumentSequence))
    preview = generate_loa_preview(session, submission, user)
    after = session.scalar(select(func.count()).select_from(DocumentSequence))
    assert preview.page_count == 1
    assert before == after == 0
    assert session.scalar(select(func.count()).select_from(core.DocumentVerification)) == 0


def test_editorial_and_publication_transitions_are_independent(session, records):
    _, user, submission = records
    submission.editorial_status = EditorialStatus.SUBMITTED
    update_editorial_status(session, submission, EditorialStatus.UNDER_REVIEW, user)
    update_editorial_status(session, submission, EditorialStatus.ACCEPTED, user)
    assert submission.publication_status == PublicationStatus.NOT_READY
    update_publication_status(session, submission, PublicationStatus.READY_FOR_PUBLICATION, user)
    assert submission.editorial_status == EditorialStatus.ACCEPTED


def test_complete_document_and_payment_lifecycle(session, records, fake_loa_renderer):
    _, user, submission = records
    loa = issue_loa(session, submission, user)
    session.commit()
    assert loa.status == LoAStatus.VALID
    assert os.path.isfile(loa.docx_path) and os.path.isfile(loa.pdf_path)
    assert public_verification(session, loa.verification.token)["document_status"] == "VALID"
    assert public_loa_verification_by_number(session, loa.document_number)["document_status"] == "VALID"
    with pytest.raises(BusinessRuleError):
        issue_loa(session, submission, user, reissue=True)
    session.rollback()
    reissued = issue_loa(session, submission, user, reissue=True, reissue_reason="Corrected author affiliation")
    session.commit()
    assert reissued.version == 2 and reissued.status == LoAStatus.VALID
    assert loa.status == LoAStatus.SUPERSEDED
    superseded_audit = session.scalar(
        select(core.AuditLog).where(
            core.AuditLog.action == "VERIFICATION_DOCUMENT_SUPERSEDED"
        )
    )
    assert superseded_audit is not None
    assert loa.verification.token not in (superseded_audit.new_value or "")
    invoice, raw_token = issue_invoice(
        session, submission, user, due_date=date.today() + timedelta(days=14),
        apc=Decimal("1000000"), discount=Decimal("100000"), additional_charge=Decimal("25000"),
        payment_method="Bank transfer", notes=None,
    )
    session.commit()
    assert invoice.status == InvoiceStatus.WAITING_PAYMENT
    assert raw_token not in invoice.author_token_hash and os.path.isfile(invoice.pdf_path)
    payment = submit_payment(
        session, invoice, payer_name="A. Author", payment_method="Bank transfer", payment_date=date.today(),
        amount_paid=invoice.total_amount, upload_name="proof.pdf",
        upload_content=b"%PDF-1.7\nminimal test proof", notes="Paid in full",
    )
    session.commit()
    assert payment.status == PaymentStatus.SUBMITTED and invoice.status == InvoiceStatus.PAYMENT_SUBMITTED
    verify_payment(session, payment, user, "Matched bank statement")
    session.commit()
    assert payment.status == PaymentStatus.VERIFIED and invoice.status == InvoiceStatus.PAID
    receipt = issue_receipt(session, payment, user)
    session.commit()
    assert os.path.isfile(receipt.pdf_path)
    assert public_verification(session, receipt.verification.token)["document_type"] == "RECEIPT"
    assert export_workbook(session, [submission.journal_id]).startswith(b"PK")
