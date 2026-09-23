"""Template-formatting jobs, separate from reviewer-response revision rounds."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, GUID


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArticleRevisionJob(Base):
    __tablename__ = "article_revision_jobs"
    __table_args__ = (Index("ix_article_revision_journal_status", "journal_id", "status", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("journals.id", ondelete="RESTRICT"), nullable=False)
    submission_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("submissions.id", ondelete="RESTRICT"))
    template_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("document_templates.id", ondelete="RESTRICT"), nullable=False)
    article_title: Mapped[str] = mapped_column(Text, nullable=False)
    submission_identifier: Mapped[str | None] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="TEMPLATE_ONLY")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="UPLOADED")
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    original_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    findings_json: Mapped[str | None] = mapped_column(Text)
    approved_fixes_json: Mapped[str | None] = mapped_column(Text)
    compliance_score: Mapped[int | None] = mapped_column(Integer)
    integrity_json: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    journal = relationship("Journal")
    submission = relationship("Submission")
    template = relationship("DocumentTemplate")
    artifacts: Mapped[list[ArticleRevisionArtifact]] = relationship(back_populates="job", order_by="ArticleRevisionArtifact.created_at")


class ArticleRevisionArtifact(Base):
    __tablename__ = "article_revision_artifacts"
    __table_args__ = (UniqueConstraint("job_id", "kind", "version", name="uq_article_revision_artifact_version"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("article_revision_jobs.id", ondelete="RESTRICT"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    job: Mapped[ArticleRevisionJob] = relationship(back_populates="artifacts")
