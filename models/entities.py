from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, GUID
from .enums import (
    DocumentStatus,
    DocumentType,
    EditorialStatus,
    InvoiceStatus,
    LoAStatus,
    PaymentStatus,
    PublicationStatus,
    Role,
    TemplateStatus,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enum_column(enum_type):
    return SAEnum(enum_type, native_enum=False, validate_strings=True, length=32)


class Journal(Base):
    __tablename__ = "journals"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    abbreviation: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    issn: Mapped[str | None] = mapped_column(String(32))
    e_issn: Mapped[str | None] = mapped_column(String(32))
    publisher: Mapped[str | None] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(String(500))
    logo_path: Mapped[str | None] = mapped_column(String(500))
    address: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    editor_in_chief: Mapped[str | None] = mapped_column(String(255))
    signature_path: Mapped[str | None] = mapped_column(String(500))
    stamp_path: Mapped[str | None] = mapped_column(String(500))
    default_apc: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="IDR")
    bank_name: Mapped[str | None] = mapped_column(String(255))
    bank_account: Mapped[str | None] = mapped_column(String(128))
    account_holder: Mapped[str | None] = mapped_column(String(255))
    qris_path: Mapped[str | None] = mapped_column(String(500))
    loa_number_format: Mapped[str] = mapped_column(String(255), default="{sequence:03d}/LoA/{journal}/{roman_month}/{year}")
    invoice_number_format: Mapped[str] = mapped_column(String(255), default="INV/{journal}/{year}/{sequence:04d}")
    receipt_number_format: Mapped[str] = mapped_column(String(255), default="RCP/{journal}/{year}/{sequence:04d}")
    loa_template: Mapped[str | None] = mapped_column(Text)
    invoice_template: Mapped[str | None] = mapped_column(Text)
    receipt_template: Mapped[str | None] = mapped_column(Text)
    loa_qr_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    loa_qr_placement: Mapped[str] = mapped_column(String(32), default="bottom-right", nullable=False)
    loa_qr_size_mm: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    default_volume: Mapped[str | None] = mapped_column(String(32))
    default_issue: Mapped[str | None] = mapped_column(String(32))
    default_publication_month: Mapped[str | None] = mapped_column(String(32))
    default_publication_year: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(500))
    role: Mapped[Role] = mapped_column(enum_column(Role), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    journal_links: Mapped[list[UserJournal]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserJournal(Base):
    __tablename__ = "user_journals"

    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="CASCADE"), primary_key=True)

    user: Mapped[User] = relationship(back_populates="journal_links")
    journal: Mapped[Journal] = relationship()


class DocumentTemplate(Base):
    __tablename__ = "document_templates"
    __table_args__ = (
        UniqueConstraint("journal_id", "template_type", "version", name="uq_document_template_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="RESTRICT"), index=True)
    template_type: Mapped[str] = mapped_column(String(32), default="LOA", index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[TemplateStatus] = mapped_column(enum_column(TemplateStatus), default=TemplateStatus.INACTIVE, index=True)
    active_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    checksum: Mapped[str] = mapped_column(String(64))
    field_mapping: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    preview_pdf_path: Mapped[str | None] = mapped_column(String(500))

    journal: Mapped[Journal] = relationship()
    uploader: Mapped[User | None] = relationship()


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("journal_id", "ojs_submission_id", name="uq_submission_journal_ojs"),
        Index("ix_submission_search", "journal_id", "editorial_status", "publication_status", "planned_year"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="RESTRICT"), index=True)
    ojs_submission_id: Mapped[str | None] = mapped_column(String(128))
    manuscript_title: Mapped[str] = mapped_column(Text)
    corresponding_author: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), index=True)
    affiliation: Mapped[str | None] = mapped_column(Text)
    date_submitted: Mapped[date | None] = mapped_column(Date)
    date_accepted: Mapped[date | None] = mapped_column(Date)
    editorial_status: Mapped[EditorialStatus] = mapped_column(enum_column(EditorialStatus), default=EditorialStatus.SUBMITTED, index=True)
    publication_status: Mapped[PublicationStatus] = mapped_column(enum_column(PublicationStatus), default=PublicationStatus.NOT_READY, index=True)
    planned_volume: Mapped[str | None] = mapped_column(String(32))
    planned_issue: Mapped[str | None] = mapped_column(String(32))
    planned_publication_month: Mapped[str | None] = mapped_column(String(32))
    planned_year: Mapped[int | None] = mapped_column(Integer)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    article_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    journal: Mapped[Journal] = relationship()
    authors: Mapped[list[Author]] = relationship(back_populates="submission", cascade="all, delete-orphan", order_by="Author.position")


class Author(Base):
    __tablename__ = "authors"
    __table_args__ = (UniqueConstraint("submission_id", "position", name="uq_author_position"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    affiliation: Mapped[str | None] = mapped_column(Text)
    is_corresponding: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer)

    submission: Mapped[Submission] = relationship(back_populates="authors")


class DocumentVerification(Base):
    __tablename__ = "document_verifications"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    document_type: Mapped[DocumentType] = mapped_column(enum_column(DocumentType), index=True)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="RESTRICT"))
    submission_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("submissions.id", ondelete="RESTRICT"))
    document_number: Mapped[str] = mapped_column(String(255), index=True)
    document_status: Mapped[DocumentStatus] = mapped_column(enum_column(DocumentStatus), default=DocumentStatus.VALID)
    issue_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    journal: Mapped[Journal] = relationship()
    submission: Mapped[Submission] = relationship()


class LoADocument(Base):
    __tablename__ = "loa_documents"
    __table_args__ = (
        UniqueConstraint("document_number", name="uq_loa_document_number"),
        UniqueConstraint("submission_id", "version", name="uq_loa_submission_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="RESTRICT"), index=True)
    submission_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("submissions.id", ondelete="RESTRICT"), index=True)
    verification_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("document_verifications.id", ondelete="RESTRICT"), unique=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("document_templates.id", ondelete="RESTRICT"))
    document_number: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1)
    issue_date: Mapped[date] = mapped_column(Date)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[LoAStatus] = mapped_column(enum_column(LoAStatus), default=LoAStatus.VALID)
    docx_path: Mapped[str | None] = mapped_column(String(500))
    pdf_path: Mapped[str | None] = mapped_column(String(500))
    document_hash: Mapped[str | None] = mapped_column(String(64))
    reissue_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    journal: Mapped[Journal] = relationship()
    submission: Mapped[Submission] = relationship()
    verification: Mapped[DocumentVerification] = relationship()
    template: Mapped[DocumentTemplate | None] = relationship()


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("invoice_number", name="uq_invoice_number"),
        CheckConstraint("apc_amount >= 0 AND discount >= 0 AND additional_charge >= 0 AND total_amount >= 0", name="ck_invoice_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="RESTRICT"), index=True)
    submission_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("submissions.id", ondelete="RESTRICT"), index=True)
    verification_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("document_verifications.id", ondelete="RESTRICT"), unique=True)
    invoice_number: Mapped[str] = mapped_column(String(255))
    invoice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    apc_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    additional_charge: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    payment_method: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[InvoiceStatus] = mapped_column(enum_column(InvoiceStatus), default=InvoiceStatus.ISSUED, index=True)
    author_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    author_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pdf_path: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    journal: Mapped[Journal] = relationship()
    submission: Mapped[Submission] = relationship()
    verification: Mapped[DocumentVerification] = relationship()


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount_paid > 0", name="ck_payment_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("invoices.id", ondelete="RESTRICT"), index=True)
    payer_name: Mapped[str] = mapped_column(String(255))
    payment_method: Mapped[str] = mapped_column(String(255))
    payment_date: Mapped[date] = mapped_column(Date)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    proof_path: Mapped[str] = mapped_column(String(500))
    proof_original_name: Mapped[str] = mapped_column(String(255))
    author_notes: Mapped[str | None] = mapped_column(Text)
    internal_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[PaymentStatus] = mapped_column(enum_column(PaymentStatus), default=PaymentStatus.SUBMITTED, index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))

    invoice: Mapped[Invoice] = relationship()


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="RESTRICT"), index=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("invoices.id", ondelete="RESTRICT"), index=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("payments.id", ondelete="RESTRICT"), unique=True)
    verification_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("document_verifications.id", ondelete="RESTRICT"), unique=True)
    receipt_number: Mapped[str] = mapped_column(String(255), unique=True)
    issue_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    authorized_person: Mapped[str] = mapped_column(String(255))
    pdf_path: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    journal: Mapped[Journal] = relationship()
    invoice: Mapped[Invoice] = relationship()
    payment: Mapped[Payment] = relationship()
    verification: Mapped[DocumentVerification] = relationship()


class DocumentSequence(Base):
    __tablename__ = "document_sequences"
    __table_args__ = (UniqueConstraint("journal_id", "document_type", "year", name="uq_document_sequence_scope"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="RESTRICT"))
    document_type: Mapped[DocumentType] = mapped_column(enum_column(DocumentType))
    year: Mapped[int] = mapped_column(Integer)
    current_value: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_journal_created", "journal_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"), index=True)
    journal_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    object_type: Mapped[str] = mapped_column(String(128))
    object_id: Mapped[str] = mapped_column(String(128))
    previous_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
