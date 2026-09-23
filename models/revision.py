from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, GUID


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RevisionJob(Base):
    __tablename__ = "revision_jobs"
    __table_args__ = (
        UniqueConstraint("submission_id", "revision_round", name="uq_revision_submission_round"),
        Index("ix_revision_job_journal_status", "journal_id", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="RESTRICT"), nullable=False)
    submission_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("submissions.id", ondelete="RESTRICT"))
    article_title: Mapped[str] = mapped_column(Text, nullable=False)
    submission_identifier: Mapped[str | None] = mapped_column(String(128))
    revision_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="REVIEWER_DRIVEN")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    original_filename: Mapped[str | None] = mapped_column(String(255))
    original_storage_key: Mapped[str | None] = mapped_column(String(500))
    original_sha256: Mapped[str | None] = mapped_column(String(64))
    ai_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    journal = relationship("Journal")
    submission = relationship("Submission")
    review_files: Mapped[list[RevisionReviewFile]] = relationship(back_populates="job", order_by="RevisionReviewFile.uploaded_at")
    comments: Mapped[list[RevisionComment]] = relationship(back_populates="job", order_by="RevisionComment.created_at")
    artifacts: Mapped[list[RevisionArtifact]] = relationship(back_populates="job", order_by="RevisionArtifact.created_at")


class RevisionReviewFile(Base):
    __tablename__ = "revision_review_files"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("revision_jobs.id", ondelete="RESTRICT"), nullable=False, index=True)
    reviewer_label: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    job: Mapped[RevisionJob] = relationship(back_populates="review_files")


class RevisionComment(Base):
    __tablename__ = "revision_comments"
    __table_args__ = (UniqueConstraint("job_id", "review_file_id", "comment_number", name="uq_revision_comment_number"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("revision_jobs.id", ondelete="RESTRICT"), nullable=False, index=True)
    review_file_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("revision_review_files.id", ondelete="RESTRICT"))
    reviewer_label: Mapped[str] = mapped_column(String(128), nullable=False)
    comment_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="REVIEWER")
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="OTHER")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="MINOR")
    suggested_section: Mapped[str | None] = mapped_column(String(255))
    target_paragraph_id: Mapped[str | None] = mapped_column(String(64))
    mapping_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    original_text: Mapped[str | None] = mapped_column(Text)
    proposed_text: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    response_text: Mapped[str | None] = mapped_column(Text)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    numeric_approval_reason: Mapped[str | None] = mapped_column(Text)
    author_input_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="UNREVIEWED")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    job: Mapped[RevisionJob] = relationship(back_populates="comments")
    review_file: Mapped[RevisionReviewFile] = relationship()


class RevisionArtifact(Base):
    __tablename__ = "revision_artifacts"
    __table_args__ = (UniqueConstraint("job_id", "kind", "version", name="uq_revision_artifact_version"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("revision_jobs.id", ondelete="RESTRICT"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    job: Mapped[RevisionJob] = relationship(back_populates="artifacts")


class RevisionIntegrityCheck(Base):
    __tablename__ = "revision_integrity_checks"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("revision_jobs.id", ondelete="RESTRICT"), nullable=False, index=True)
    artifact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    job: Mapped[RevisionJob] = relationship()
