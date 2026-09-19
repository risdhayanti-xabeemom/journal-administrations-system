from __future__ import annotations

import hashlib
import hmac
import io
import json
import calendar
import mimetypes
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Iterable

import pandas as pd
import qrcode
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from config import settings
from models import (
    AuditLog,
    Author,
    DocumentSequence,
    DocumentTemplate,
    DocumentStatus,
    DocumentType,
    DocumentVerification,
    EditorialStatus,
    Invoice,
    InvoiceStatus,
    Journal,
    LoADocument,
    LoAStatus,
    Payment,
    PaymentStatus,
    PublicationStatus,
    Receipt,
    Role,
    Submission,
    User,
    UserJournal,
)
from services.docx_templates import (
    GeneratedLoA,
    INDONESIAN_MONTHS,
    PDFConverterUnavailable,
    TemplateError,
    generate_from_template,
    indonesian_date,
    sha256_file,
)
from services.template_service import active_loa_template, mapped_template_values


PBKDF2_ITERATIONS = 600_000
ALLOWED_UPLOADS = {
    ".pdf": ("application/pdf", b"%PDF"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
}


class BusinessRuleError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


class OJSNotConfiguredError(RuntimeError):
    pass


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(calculated.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bootstrap_admin(session: Session) -> bool:
    if not settings.admin_email or not settings.admin_password:
        return False
    email = settings.admin_email.strip().lower()
    if session.scalar(select(User).where(User.email == email)):
        return False
    session.add(
        User(
            email=email,
            display_name=settings.admin_name,
            password_hash=hash_password(settings.admin_password),
            role=Role.SUPER_ADMIN,
        )
    )
    return True


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == email.strip().lower(), User.active.is_(True)))
    if not user or not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now(timezone.utc)
    session.flush()
    return user


def journals_for_user(session: Session, user: User) -> list[Journal]:
    if user.role == Role.SUPER_ADMIN:
        return list(session.scalars(select(Journal).where(Journal.active.is_(True)).order_by(Journal.abbreviation)))
    return list(
        session.scalars(
            select(Journal)
            .join(UserJournal, UserJournal.journal_id == Journal.id)
            .where(UserJournal.user_id == user.id, Journal.active.is_(True))
            .order_by(Journal.abbreviation)
        )
    )


def assert_journal_access(user: User, journal_id: uuid.UUID, allowed_roles: Iterable[Role], session: Session) -> None:
    if user.role not in set(allowed_roles):
        raise AuthorizationError("Your role cannot perform this action.")
    if user.role == Role.SUPER_ADMIN:
        return
    allowed = session.scalar(
        select(UserJournal).where(UserJournal.user_id == user.id, UserJournal.journal_id == journal_id)
    )
    if not allowed:
        raise AuthorizationError("You do not have access to this journal.")


def log_audit(
    session: Session,
    *,
    action: str,
    object_type: str,
    object_id: object,
    user_id: uuid.UUID | None = None,
    journal_id: uuid.UUID | None = None,
    previous: object | None = None,
    new: object | None = None,
) -> None:
    session.add(
        AuditLog(
            user_id=user_id,
            journal_id=journal_id,
            action=action,
            object_type=object_type,
            object_id=str(object_id),
            previous_value=json.dumps(previous, default=str, ensure_ascii=False) if previous is not None else None,
            new_value=json.dumps(new, default=str, ensure_ascii=False) if new is not None else None,
        )
    )


EDITORIAL_TRANSITIONS = {
    EditorialStatus.SUBMITTED: {EditorialStatus.UNDER_REVIEW, EditorialStatus.WITHDRAWN},
    EditorialStatus.UNDER_REVIEW: {EditorialStatus.ACCEPTED, EditorialStatus.REJECTED, EditorialStatus.WITHDRAWN},
    EditorialStatus.ACCEPTED: set(),
    EditorialStatus.REJECTED: set(),
    EditorialStatus.WITHDRAWN: set(),
}
PUBLICATION_TRANSITIONS = {
    PublicationStatus.NOT_READY: {PublicationStatus.READY_FOR_PUBLICATION},
    PublicationStatus.READY_FOR_PUBLICATION: {PublicationStatus.PUBLISHED, PublicationStatus.NOT_READY},
    PublicationStatus.PUBLISHED: set(),
}


def validate_transition(current, target, transitions) -> None:
    if target == current:
        return
    if target not in transitions.get(current, set()):
        raise BusinessRuleError(f"Invalid transition: {current.value} -> {target.value}")


def update_editorial_status(session: Session, submission: Submission, target: EditorialStatus, user: User) -> None:
    assert_journal_access(user, submission.journal_id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    validate_transition(submission.editorial_status, target, EDITORIAL_TRANSITIONS)
    previous = submission.editorial_status.value
    submission.editorial_status = target
    if target == EditorialStatus.ACCEPTED and not submission.date_accepted:
        submission.date_accepted = date.today()
    log_audit(
        session,
        action="EDITORIAL_STATUS_CHANGED",
        object_type="submission",
        object_id=submission.id,
        user_id=user.id,
        journal_id=submission.journal_id,
        previous={"editorial_status": previous},
        new={"editorial_status": target.value},
    )


def update_publication_status(session: Session, submission: Submission, target: PublicationStatus, user: User) -> None:
    assert_journal_access(user, submission.journal_id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    validate_transition(submission.publication_status, target, PUBLICATION_TRANSITIONS)
    previous = submission.publication_status.value
    submission.publication_status = target
    log_audit(
        session,
        action="PUBLICATION_STATUS_CHANGED",
        object_type="submission",
        object_id=submission.id,
        user_id=user.id,
        journal_id=submission.journal_id,
        previous={"publication_status": previous},
        new={"publication_status": target.value},
    )


def roman_month(month: int) -> str:
    values = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")
    if month < 1 or month > 12:
        raise ValueError("Month must be between 1 and 12.")
    return values[month - 1]


def next_document_number(session: Session, journal: Journal, document_type: DocumentType, issue_date: date) -> str:
    if session.bind and session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        statement = insert(DocumentSequence).values(
            id=uuid.uuid4(), journal_id=journal.id, document_type=document_type, year=issue_date.year, current_value=1
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_document_sequence_scope",
            set_={"current_value": DocumentSequence.current_value + 1, "updated_at": func.now()},
        ).returning(DocumentSequence.current_value)
        value = session.execute(statement).scalar_one()
    else:
        sequence = session.scalar(
            select(DocumentSequence)
            .where(
                DocumentSequence.journal_id == journal.id,
                DocumentSequence.document_type == document_type,
                DocumentSequence.year == issue_date.year,
            )
            .with_for_update()
        )
        if sequence is None:
            sequence = DocumentSequence(
                journal_id=journal.id, document_type=document_type, year=issue_date.year, current_value=1
            )
            session.add(sequence)
            value = 1
        else:
            sequence.current_value += 1
            value = sequence.current_value
        session.flush()

    template = {
        DocumentType.LOA: journal.loa_number_format,
        DocumentType.INVOICE: journal.invoice_number_format,
        DocumentType.RECEIPT: journal.receipt_number_format,
    }[document_type]
    try:
        return template.format(
            sequence=value,
            journal=journal.abbreviation,
            year=issue_date.year,
            month=issue_date.month,
            roman_month=roman_month(issue_date.month),
        )
    except (KeyError, ValueError) as exc:
        raise BusinessRuleError(f"Invalid numbering format for {document_type.value}: {exc}") from exc


def create_verification(
    session: Session,
    *,
    document_type: DocumentType,
    journal: Journal,
    submission: Submission,
    document_number: str,
    issue_date: date,
) -> DocumentVerification:
    record = DocumentVerification(
        token=secrets.token_urlsafe(32),
        document_type=document_type,
        journal_id=journal.id,
        submission_id=submission.id,
        document_number=document_number,
        issue_date=issue_date,
        document_status=DocumentStatus.VALID,
    )
    session.add(record)
    session.flush()
    return record


def verification_url(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/verify/{token}"


def author_payment_url(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/payment/{token}"


def _qr_image(url: str) -> Image:
    image = qrcode.make(url)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return Image(buffer, width=30 * mm, height=30 * mm)


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="JASTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=16, leading=20, alignment=TA_CENTER, spaceAfter=10))
    styles.add(ParagraphStyle(name="JASMeta", parent=styles["Normal"], fontSize=9, leading=12, textColor=colors.HexColor("#475569")))
    styles.add(ParagraphStyle(name="JASRight", parent=styles["Normal"], fontSize=9, alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="JASBody", parent=styles["BodyText"], fontSize=10.5, leading=16, spaceAfter=8))
    styles.add(ParagraphStyle(name="JASHeaderTitle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=14, leading=17, textColor=colors.HexColor("#0F172A"), spaceAfter=3))
    return styles


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#64748B"))
    canvas.drawString(20 * mm, 12 * mm, f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _journal_header(journal: Journal, styles) -> list:
    logo = Spacer(1, 1 * mm)
    if journal.logo_path and Path(journal.logo_path).is_file():
        logo = Image(journal.logo_path)
        scale = min((30 * mm) / logo.imageWidth, (20 * mm) / logo.imageHeight)
        logo.drawWidth = logo.imageWidth * scale
        logo.drawHeight = logo.imageHeight * scale
    identity = [
        Paragraph(escape(journal.name), styles["JASHeaderTitle"]),
        Paragraph(escape(journal.publisher or "Publisher not configured"), styles["JASMeta"]),
        Paragraph(escape(journal.address or "Address not configured"), styles["JASMeta"]),
    ]
    header = Table(
        [[logo, identity]],
        colWidths=[35 * mm, 125 * mm],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 6),
            ("LEFTPADDING", (1, 0), (1, 0), 0),
            ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]),
    )
    rule = Table([[""]], colWidths=[160 * mm], rowHeights=[1.5 * mm], style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1D4ED8"))]))
    return [header, rule, Spacer(1, 5 * mm)]


def _build_pdf(path: Path, story: list) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(path), pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm, topMargin=18 * mm, bottomMargin=20 * mm,
        title=path.stem, author="Journal Administration System",
    )
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return str(path)


def _authors_text(submission: Submission) -> str:
    names = [author.name for author in submission.authors]
    return ", ".join(names) if names else submission.corresponding_author


def _optional_image(path: str | None, width: float, height: float):
    if path and Path(path).is_file():
        return Image(path, width=width, height=height, kind="proportional")
    return Spacer(1, 1 * mm)


def _loa_context(
    submission: Submission,
    *,
    document_number: str,
    printed_date: date,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    journal = submission.journal
    publication_month = submission.planned_publication_month or journal.default_publication_month
    if not publication_month:
        publication_month = (
            INDONESIAN_MONTHS[printed_date.month - 1]
            if journal.abbreviation.upper() == "ELKOLIND"
            else calendar.month_name[printed_date.month]
        )
    context: dict[str, object] = {
        "loa_number": document_number,
        "recipient_name": submission.corresponding_author,
        "recipient_affiliation": submission.affiliation or "",
        "ojs_submission_id": submission.ojs_submission_id or "-",
        "authors": _authors_text(submission),
        "article_title": submission.manuscript_title,
        "volume": submission.planned_volume or journal.default_volume or "-",
        "issue": submission.planned_issue or journal.default_issue or "-",
        "publication_month": publication_month,
        "publication_year": submission.planned_year or journal.default_publication_year or printed_date.year,
        "loa_date": indonesian_date(printed_date) if journal.abbreviation.upper() == "ELKOLIND" else printed_date.isoformat(),
        "journal_url": journal.website or "",
        "journal_email": journal.contact_email or "",
    }
    context.update(overrides or {})
    return context


def _generate_template_loa(
    template: DocumentTemplate,
    submission: Submission,
    *,
    document_number: str,
    printed_date: date,
    output_stem: str,
    overrides: dict[str, object] | None = None,
    verification_token: str | None = None,
) -> GeneratedLoA:
    context = _loa_context(
        submission,
        document_number=document_number,
        printed_date=printed_date,
        overrides=overrides,
    )
    values = mapped_template_values(template, context)
    output_dir = settings.document_dir / "loa"
    return generate_from_template(
        template.storage_path,
        output_dir / f"{output_stem}.docx",
        values,
        output_pdf=output_dir / f"{output_stem}.pdf",
        qr_url=(verification_url(verification_token or "DRAFT-NOT-ISSUED") if submission.journal.loa_qr_enabled else None),
        qr_size_mm=submission.journal.loa_qr_size_mm,
        qr_placement=submission.journal.loa_qr_placement,
    )


def generate_loa_preview(
    session: Session,
    submission: Submission,
    user: User,
    *,
    printed_date: date | None = None,
    overrides: dict[str, object] | None = None,
) -> GeneratedLoA:
    assert_journal_access(user, submission.journal_id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    if submission.editorial_status != EditorialStatus.ACCEPTED:
        raise BusinessRuleError("A LoA preview is available only for an accepted submission.")
    template = active_loa_template(session, submission.journal_id)
    if not template:
        raise BusinessRuleError("No ACTIVE Master LoA template is configured for this journal.")
    return _generate_template_loa(
        template,
        submission,
        document_number="DRAFT PREVIEW — NOT ISSUED",
        printed_date=printed_date or date.today(),
        output_stem=f"preview-{submission.id}-{uuid.uuid4().hex}",
        overrides=overrides,
    )


def money(value: Decimal, currency: str) -> str:
    amount = Decimal(value)
    if currency.upper() == "IDR":
        rounded = int(amount.quantize(Decimal("1")))
        return f"Rp {rounded:,}".replace(",", ".")
    return f"{currency.upper()} {amount:,.2f}"


def calculate_invoice_total(apc: Decimal, discount: Decimal, additional_charge: Decimal) -> Decimal:
    values = [Decimal(apc), Decimal(discount), Decimal(additional_charge)]
    if any(value < 0 for value in values):
        raise BusinessRuleError("Invoice amounts cannot be negative.")
    total = values[0] - values[1] + values[2]
    if total < 0:
        raise BusinessRuleError("Discount cannot make the invoice total negative.")
    return total.quantize(Decimal("0.01"))


def generate_invoice_pdf(invoice: Invoice) -> str:
    styles = _styles()
    journal, submission = invoice.journal, invoice.submission
    story = _journal_header(journal, styles)
    story.extend(
        [
            Paragraph("INVOICE", styles["JASTitle"]),
            Table(
                [
                    ["Invoice Number", invoice.invoice_number],
                    ["Invoice Date", invoice.invoice_date.isoformat()],
                    ["Due Date", invoice.due_date.isoformat()],
                    ["OJS Submission ID", submission.ojs_submission_id or "Manual entry"],
                ],
                colWidths=[45 * mm, 115 * mm],
                style=TableStyle([("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E2E8F0")), ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#CBD5E1")), ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("PADDING", (0, 0), (-1, -1), 7)]),
            ),
            Spacer(1, 6 * mm),
            Paragraph(f"<b>Bill to</b><br/>{escape(submission.corresponding_author)}<br/>{escape(submission.affiliation or '')}", styles["JASBody"]),
            Paragraph(f"<b>Article</b><br/>{escape(submission.manuscript_title)}", styles["JASBody"]),
            Table(
                [
                    ["Description", "Amount"],
                    ["Publication fee / APC", money(invoice.apc_amount, invoice.currency)],
                    ["Discount", f"- {money(invoice.discount, invoice.currency)}"],
                    ["Additional charge", money(invoice.additional_charge, invoice.currency)],
                    ["TOTAL", money(invoice.total_amount, invoice.currency)],
                ],
                colWidths=[105 * mm, 55 * mm],
                style=TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#DBEAFE")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                    ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                    ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#CBD5E1")),
                    ("PADDING", (0, 0), (-1, -1), 7),
                ]),
            ),
            Spacer(1, 6 * mm),
            Paragraph(f"<b>Payment method</b>: {escape(invoice.payment_method or 'Contact journal administration')}", styles["JASBody"]),
            Paragraph(f"<b>Bank</b>: {escape(journal.bank_name or 'Not configured')}<br/><b>Account</b>: {escape(journal.bank_account or 'Not configured')}<br/><b>Holder</b>: {escape(journal.account_holder or 'Not configured')}", styles["JASBody"]),
            _optional_image(journal.qris_path, 35 * mm, 35 * mm),
            Paragraph(f"<b>Payment instructions</b><br/>{escape(journal.invoice_template)}", styles["JASBody"]) if journal.invoice_template else Spacer(1, 1 * mm),
            Table([[_qr_image(verification_url(invoice.verification.token)), Paragraph(f"Scan to verify this invoice.<br/><font size='8'>Verification code: {invoice.verification.token}</font>", styles["JASBody"])]], colWidths=[35 * mm, 125 * mm]),
        ]
    )
    return _build_pdf(settings.document_dir / f"invoice-{invoice.id}.pdf", story)


def generate_receipt_pdf(receipt: Receipt) -> str:
    styles = _styles()
    journal, invoice, payment = receipt.journal, receipt.invoice, receipt.payment
    story = _journal_header(journal, styles)
    story.extend(
        [
            Paragraph("RECEIPT / PAYMENT RECEIPT", styles["JASTitle"]),
            Table(
                [
                    ["Receipt Number", Paragraph(escape(receipt.receipt_number), styles["JASMeta"])],
                    ["Invoice Number", Paragraph(escape(invoice.invoice_number), styles["JASMeta"])],
                    ["OJS Submission ID", Paragraph(escape(invoice.submission.ojs_submission_id or "Manual entry"), styles["JASMeta"])],
                    ["Issue Date", Paragraph(receipt.issue_date.isoformat(), styles["JASMeta"])],
                    ["Article", Paragraph(escape(invoice.submission.manuscript_title), styles["JASMeta"])],
                    ["Corresponding Author", Paragraph(escape(invoice.submission.corresponding_author), styles["JASMeta"])],
                    ["Payer", Paragraph(escape(payment.payer_name), styles["JASMeta"])],
                    ["Amount Received", Paragraph(escape(money(receipt.amount, invoice.currency)), styles["JASMeta"])],
                    ["Payment Date", Paragraph(payment.payment_date.isoformat(), styles["JASMeta"])],
                    ["Payment Method", Paragraph(escape(payment.payment_method), styles["JASMeta"])],
                ],
                colWidths=[45 * mm, 115 * mm],
                style=TableStyle([("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E2E8F0")), ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#CBD5E1")), ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 7)]),
            ),
            Spacer(1, 8 * mm),
            Paragraph(escape(journal.receipt_template), styles["JASBody"]) if journal.receipt_template else Spacer(1, 1 * mm),
            Table(
                [[[
                    _optional_image(journal.signature_path, 40 * mm, 16 * mm),
                    _optional_image(journal.stamp_path, 22 * mm, 22 * mm),
                    Paragraph(f"Authorized by<br/><b>{escape(receipt.authorized_person)}</b>", styles["JASBody"]),
                ], _qr_image(verification_url(receipt.verification.token))]],
                colWidths=[125 * mm, 35 * mm],
                style=TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]),
            ),
            Paragraph(f"Verification code: {receipt.verification.token}", styles["JASMeta"]),
        ]
    )
    return _build_pdf(settings.document_dir / f"receipt-{receipt.id}.pdf", story)


def issue_loa(
    session: Session,
    submission: Submission,
    user: User,
    reissue: bool = False,
    *,
    printed_date: date | None = None,
    reissue_reason: str | None = None,
    allow_overflow: bool = False,
    overrides: dict[str, object] | None = None,
) -> LoADocument:
    assert_journal_access(user, submission.journal_id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    if submission.editorial_status != EditorialStatus.ACCEPTED:
        raise BusinessRuleError("A LoA can only be issued for an accepted submission.")
    valid = session.scalar(select(LoADocument).where(LoADocument.submission_id == submission.id, LoADocument.status == LoAStatus.VALID).order_by(LoADocument.version.desc()))
    if valid and not reissue:
        raise BusinessRuleError("A valid LoA already exists. Use Reissue to preserve version history.")
    if reissue and valid and not (reissue_reason or "").strip():
        raise BusinessRuleError("Reason for reissue is required.")
    template = active_loa_template(session, submission.journal_id)
    if not template:
        raise BusinessRuleError("No ACTIVE Master LoA template is configured for this journal.")
    version = (session.scalar(select(func.max(LoADocument.version)).where(LoADocument.submission_id == submission.id)) or 0) + 1
    issue_date = printed_date or date.today()
    issued_at = datetime.now(timezone.utc)
    number = next_document_number(session, submission.journal, DocumentType.LOA, issue_date)
    verification = create_verification(session, document_type=DocumentType.LOA, journal=submission.journal, submission=submission, document_number=number, issue_date=issue_date)
    loa = LoADocument(
        journal_id=submission.journal_id,
        submission_id=submission.id,
        verification_id=verification.id,
        template_id=template.id,
        document_number=number,
        version=version,
        issue_date=issue_date,
        issued_at=issued_at,
        reissue_reason=(reissue_reason or "").strip() or None,
        created_by=user.id,
    )
    session.add(loa)
    session.flush()
    loa.verification, loa.journal, loa.submission, loa.template = verification, submission.journal, submission, template
    generated = _generate_template_loa(
        template,
        submission,
        document_number=number,
        printed_date=issue_date,
        output_stem=f"loa-{loa.id}-v{version}",
        overrides=overrides,
        verification_token=verification.token,
    )
    if generated.overflow and not allow_overflow:
        raise BusinessRuleError(
            f"The generated LoA is {generated.page_count} pages. Review the preview and explicitly allow issuance."
        )
    loa.docx_path = str(generated.docx_path)
    loa.pdf_path = str(generated.pdf_path) if generated.pdf_path else None
    loa.document_hash = sha256_file(generated.pdf_path or generated.docx_path)
    if valid:
        valid.status = LoAStatus.SUPERSEDED
        valid.verification.document_status = DocumentStatus.SUPERSEDED
        log_audit(
            session,
            action="LOA_REISSUED",
            object_type="loa",
            object_id=valid.id,
            user_id=user.id,
            journal_id=submission.journal_id,
            previous={"status": "VALID", "number": valid.document_number},
            new={"status": "SUPERSEDED", "reason": reissue_reason, "replacement_id": str(loa.id)},
        )
    log_audit(
        session,
        action="LOA_ISSUED",
        object_type="loa",
        object_id=loa.id,
        user_id=user.id,
        journal_id=submission.journal_id,
        new={
            "number": number,
            "version": version,
            "template_id": str(template.id),
            "template_version": template.version,
            "printed_date": issue_date.isoformat(),
            "issued_at": issued_at.isoformat(),
            "document_hash": loa.document_hash,
        },
    )
    return loa


def revoke_loa(session: Session, loa: LoADocument, user: User) -> None:
    assert_journal_access(user, loa.journal_id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    if loa.status != LoAStatus.VALID:
        raise BusinessRuleError("Only a valid LoA can be revoked.")
    loa.status = LoAStatus.REVOKED
    loa.verification.document_status = DocumentStatus.REVOKED
    log_audit(session, action="LOA_REVOKED", object_type="loa", object_id=loa.id, user_id=user.id, journal_id=loa.journal_id, previous={"status": "VALID"}, new={"status": "REVOKED"})


def issue_invoice(
    session: Session,
    submission: Submission,
    user: User,
    *,
    due_date: date,
    apc: Decimal,
    discount: Decimal,
    additional_charge: Decimal,
    payment_method: str | None,
    notes: str | None,
) -> tuple[Invoice, str]:
    assert_journal_access(user, submission.journal_id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    if submission.editorial_status != EditorialStatus.ACCEPTED:
        raise BusinessRuleError("An invoice can only be issued for an accepted submission.")
    invoice_date = date.today()
    if due_date < invoice_date:
        raise BusinessRuleError("Due date cannot be before the invoice date.")
    total = calculate_invoice_total(apc, discount, additional_charge)
    number = next_document_number(session, submission.journal, DocumentType.INVOICE, invoice_date)
    verification = create_verification(session, document_type=DocumentType.INVOICE, journal=submission.journal, submission=submission, document_number=number, issue_date=invoice_date)
    raw_token = secrets.token_urlsafe(32)
    invoice = Invoice(
        journal_id=submission.journal_id,
        submission_id=submission.id,
        verification_id=verification.id,
        invoice_number=number,
        invoice_date=invoice_date,
        due_date=due_date,
        apc_amount=apc,
        discount=discount,
        additional_charge=additional_charge,
        total_amount=total,
        currency=submission.journal.currency,
        payment_method=payment_method,
        notes=notes,
        status=InvoiceStatus.WAITING_PAYMENT,
        author_token_hash=token_hash(raw_token),
        author_token_expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        created_by=user.id,
    )
    session.add(invoice)
    session.flush()
    invoice.verification, invoice.journal, invoice.submission = verification, submission.journal, submission
    invoice.pdf_path = generate_invoice_pdf(invoice)
    log_audit(session, action="INVOICE_ISSUED", object_type="invoice", object_id=invoice.id, user_id=user.id, journal_id=submission.journal_id, new={"number": number, "total": str(total)})
    return invoice, raw_token


def cancel_invoice(session: Session, invoice: Invoice, user: User) -> None:
    assert_journal_access(user, invoice.journal_id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    if invoice.status in {InvoiceStatus.PAID, InvoiceStatus.CANCELLED}:
        raise BusinessRuleError("A paid or cancelled invoice cannot be cancelled.")
    previous = invoice.status.value
    invoice.status = InvoiceStatus.CANCELLED
    invoice.verification.document_status = DocumentStatus.CANCELLED
    log_audit(session, action="INVOICE_CANCELLED", object_type="invoice", object_id=invoice.id, user_id=user.id, journal_id=invoice.journal_id, previous={"status": previous}, new={"status": "CANCELLED"})


def find_invoice_by_author_token(session: Session, raw_token: str) -> Invoice | None:
    invoice = session.scalar(select(Invoice).where(Invoice.author_token_hash == token_hash(raw_token)))
    if not invoice:
        return None
    expires = invoice.author_token_expires_at
    now = datetime.now(timezone.utc)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return invoice if expires >= now else None


def validate_payment_upload(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_UPLOADS:
        raise BusinessRuleError("Only PDF, PNG, JPG, and JPEG files are allowed.")
    if not content or len(content) > settings.max_upload_bytes:
        raise BusinessRuleError(f"Upload must be between 1 byte and {settings.max_upload_bytes} bytes.")
    _, signature = ALLOWED_UPLOADS[suffix]
    if not content.startswith(signature):
        raise BusinessRuleError("The uploaded file content does not match its extension.")
    return suffix


def store_private_upload(filename: str, content: bytes) -> str:
    suffix = validate_payment_upload(filename, content)
    randomized = settings.upload_dir / f"{uuid.uuid4().hex}{suffix}"
    randomized.parent.mkdir(parents=True, exist_ok=True)
    randomized.write_bytes(content)
    return str(randomized)


def submit_payment(
    session: Session,
    invoice: Invoice,
    *,
    payer_name: str,
    payment_method: str,
    payment_date: date,
    amount_paid: Decimal,
    upload_name: str,
    upload_content: bytes,
    notes: str | None,
) -> Payment:
    if invoice.status in {InvoiceStatus.CANCELLED, InvoiceStatus.PAID}:
        raise BusinessRuleError("This invoice no longer accepts payment confirmations.")
    pending = session.scalar(select(Payment).where(Payment.invoice_id == invoice.id, Payment.status == PaymentStatus.SUBMITTED))
    if pending:
        raise BusinessRuleError("A payment confirmation is already awaiting verification.")
    if Decimal(amount_paid) <= 0:
        raise BusinessRuleError("Amount paid must be greater than zero.")
    proof_path = store_private_upload(upload_name, upload_content)
    payment = Payment(invoice_id=invoice.id, payer_name=payer_name.strip(), payment_method=payment_method.strip(), payment_date=payment_date, amount_paid=amount_paid, proof_path=proof_path, proof_original_name=Path(upload_name).name[:255], author_notes=notes, status=PaymentStatus.SUBMITTED)
    session.add(payment)
    invoice.status = InvoiceStatus.PAYMENT_SUBMITTED
    session.flush()
    log_audit(session, action="PAYMENT_SUBMITTED", object_type="payment", object_id=payment.id, journal_id=invoice.journal_id, new={"invoice": invoice.invoice_number, "amount": str(amount_paid)})
    return payment


def verify_payment(session: Session, payment: Payment, user: User, internal_notes: str | None = None) -> None:
    assert_journal_access(user, payment.invoice.journal_id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN, Role.FINANCE}, session)
    if payment.status != PaymentStatus.SUBMITTED:
        raise BusinessRuleError("Only a submitted payment can be verified.")
    payment.status = PaymentStatus.VERIFIED
    payment.internal_notes = internal_notes
    payment.verified_at = datetime.now(timezone.utc)
    payment.verified_by = user.id
    payment.invoice.status = InvoiceStatus.PAID
    log_audit(session, action="PAYMENT_VERIFIED", object_type="payment", object_id=payment.id, user_id=user.id, journal_id=payment.invoice.journal_id, previous={"status": "SUBMITTED"}, new={"status": "VERIFIED"})


def reject_payment(session: Session, payment: Payment, user: User, internal_notes: str) -> None:
    assert_journal_access(user, payment.invoice.journal_id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN, Role.FINANCE}, session)
    if payment.status != PaymentStatus.SUBMITTED:
        raise BusinessRuleError("Only a submitted payment can be rejected.")
    if not internal_notes.strip():
        raise BusinessRuleError("Explain the correction required.")
    payment.status = PaymentStatus.REJECTED
    payment.internal_notes = internal_notes
    payment.invoice.status = InvoiceStatus.WAITING_PAYMENT
    log_audit(session, action="PAYMENT_REJECTED", object_type="payment", object_id=payment.id, user_id=user.id, journal_id=payment.invoice.journal_id, previous={"status": "SUBMITTED"}, new={"status": "REJECTED", "notes": internal_notes})


def issue_receipt(session: Session, payment: Payment, user: User) -> Receipt:
    journal = payment.invoice.journal
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN, Role.FINANCE}, session)
    if payment.status != PaymentStatus.VERIFIED:
        raise BusinessRuleError("Receipt generation requires a verified payment.")
    if session.scalar(select(Receipt).where(Receipt.payment_id == payment.id)):
        raise BusinessRuleError("A receipt already exists for this payment.")
    issue_date = date.today()
    number = next_document_number(session, journal, DocumentType.RECEIPT, issue_date)
    verification = create_verification(session, document_type=DocumentType.RECEIPT, journal=journal, submission=payment.invoice.submission, document_number=number, issue_date=issue_date)
    receipt = Receipt(journal_id=journal.id, invoice_id=payment.invoice.id, payment_id=payment.id, verification_id=verification.id, receipt_number=number, issue_date=issue_date, amount=payment.amount_paid, authorized_person=user.display_name, created_by=user.id)
    session.add(receipt)
    session.flush()
    receipt.journal, receipt.invoice, receipt.payment, receipt.verification = journal, payment.invoice, payment, verification
    receipt.pdf_path = generate_receipt_pdf(receipt)
    log_audit(session, action="RECEIPT_ISSUED", object_type="receipt", object_id=receipt.id, user_id=user.id, journal_id=journal.id, new={"number": number, "amount": str(receipt.amount)})
    return receipt


def public_verification(session: Session, token: str) -> dict | None:
    record = session.scalar(select(DocumentVerification).where(DocumentVerification.token == token))
    if not record:
        return None
    return _verification_payload(record)


def public_loa_verification_by_number(session: Session, document_number: str) -> dict | None:
    record = session.scalar(
        select(DocumentVerification)
        .where(
            DocumentVerification.document_type == DocumentType.LOA,
            func.lower(DocumentVerification.document_number) == document_number.strip().lower(),
        )
        .order_by(DocumentVerification.created_at.desc())
    )
    return _verification_payload(record) if record else None


def _verification_payload(record: DocumentVerification) -> dict:
    return {
        "document_type": record.document_type.value,
        "journal": record.journal.name,
        "document_number": record.document_number,
        "article_title": record.submission.manuscript_title,
        "author": record.submission.corresponding_author,
        "issue_date": record.issue_date.isoformat(),
        "document_status": record.document_status.value,
    }


def global_submission_search(session: Session, journal_ids: list[uuid.UUID], query: str) -> list[Submission]:
    if not journal_ids:
        return []
    term = f"%{query.strip()}%"
    statement = (
        select(Submission)
        .outerjoin(Author, Author.submission_id == Submission.id)
        .outerjoin(LoADocument, LoADocument.submission_id == Submission.id)
        .outerjoin(Invoice, Invoice.submission_id == Submission.id)
        .outerjoin(Receipt, Receipt.invoice_id == Invoice.id)
        .where(
            Submission.journal_id.in_(journal_ids),
            or_(
                Submission.ojs_submission_id.ilike(term), Submission.manuscript_title.ilike(term),
                Submission.corresponding_author.ilike(term), Submission.email.ilike(term),
                Submission.doi.ilike(term), Author.name.ilike(term), LoADocument.document_number.ilike(term),
                Invoice.invoice_number.ilike(term), Receipt.receipt_number.ilike(term),
            ),
        )
        .distinct()
        .limit(100)
    )
    return list(session.scalars(statement))


def export_workbook(session: Session, journal_ids: list[uuid.UUID]) -> bytes:
    submissions = list(session.scalars(select(Submission).where(Submission.journal_id.in_(journal_ids)))) if journal_ids else []
    invoices = list(session.scalars(select(Invoice).where(Invoice.journal_id.in_(journal_ids)))) if journal_ids else []
    payments = list(session.scalars(select(Payment).join(Invoice).where(Invoice.journal_id.in_(journal_ids)))) if journal_ids else []
    loas = list(session.scalars(select(LoADocument).where(LoADocument.journal_id.in_(journal_ids)))) if journal_ids else []
    receipts = list(session.scalars(select(Receipt).where(Receipt.journal_id.in_(journal_ids)))) if journal_ids else []
    frames = {
        "Submissions": pd.DataFrame([{ "ID": str(x.id), "Journal": x.journal.abbreviation, "OJS ID": x.ojs_submission_id, "Title": x.manuscript_title, "Corresponding Author": x.corresponding_author, "Email": x.email, "Editorial Status": x.editorial_status.value, "Publication Status": x.publication_status.value, "Volume": x.planned_volume, "Issue": x.planned_issue, "Month": x.planned_publication_month, "Year": x.planned_year, "DOI": x.doi } for x in submissions]),
        "LoA": pd.DataFrame([{ "Number": x.document_number, "Journal": x.journal.abbreviation, "Submission": str(x.submission_id), "Version": x.version, "Issue Date": x.issue_date, "Status": x.status.value } for x in loas]),
        "Invoices": pd.DataFrame([{ "Number": x.invoice_number, "Journal": x.journal.abbreviation, "Submission": str(x.submission_id), "Date": x.invoice_date, "Due": x.due_date, "Total": float(x.total_amount), "Currency": x.currency, "Status": x.status.value } for x in invoices]),
        "Payments": pd.DataFrame([{ "Payment ID": str(x.id), "Invoice": x.invoice.invoice_number, "Payer": x.payer_name, "Date": x.payment_date, "Amount": float(x.amount_paid), "Method": x.payment_method, "Status": x.status.value } for x in payments]),
        "Receipts": pd.DataFrame([{ "Number": x.receipt_number, "Invoice": x.invoice.invoice_number, "Date": x.issue_date, "Amount": float(x.amount), "Authorized Person": x.authorized_person } for x in receipts]),
        "Issue Administration": pd.DataFrame([{ "Journal": x.journal.abbreviation, "Submission": x.ojs_submission_id, "Title": x.manuscript_title, "Volume": x.planned_volume, "Issue": x.planned_issue, "Month": x.planned_publication_month, "Year": x.planned_year, "LoA": "Yes" if any(l.submission_id == x.id and l.status == LoAStatus.VALID for l in loas) else "No", "Invoice": "Yes" if any(i.submission_id == x.id for i in invoices) else "No", "Paid": "Yes" if any(i.submission_id == x.id and i.status == InvoiceStatus.PAID for i in invoices) else "No", "Published": "Yes" if x.publication_status == PublicationStatus.PUBLISHED else "No" } for x in submissions]),
    }
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in frames.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)
            worksheet = writer.book[name[:31]]
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for column in worksheet.columns:
                width = min(max((len(str(cell.value or "")) for cell in column), default=10) + 2, 60)
                worksheet.column_dimensions[column[0].column_letter].width = width
    return buffer.getvalue()


class OJSService:
    """Integration boundary. Configure and subclass only after an OJS endpoint is verified."""

    def get_submission(self, submission_id: str):
        raise OJSNotConfiguredError("OJS integration is not configured. Use manual entry or import.")

    def get_submission_metadata(self, submission_id: str):
        raise OJSNotConfiguredError("OJS integration is not configured. Use manual entry or import.")

    def get_authors(self, submission_id: str):
        raise OJSNotConfiguredError("OJS integration is not configured. Use manual entry or import.")

    def get_editorial_status(self, submission_id: str):
        raise OJSNotConfiguredError("OJS integration is not configured. Use manual entry or import.")
