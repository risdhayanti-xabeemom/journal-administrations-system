"""Journal-isolated revision lifecycle and immutable artifact records."""

from __future__ import annotations

import io
import json
import uuid
import hashlib
from datetime import datetime, timezone
from html import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy import func, inspect, or_, select
from sqlalchemy.orm import Session

from config import settings
from models import (
    AuditLog, Journal, RevisionArtifact, RevisionComment, RevisionIntegrityCheck,
    RevisionJob, RevisionReviewFile, Role, Submission, User,
)
from services.core import AuthorizationError, assert_journal_access, log_audit
from services.revision_ai import DisabledRevisionAIService, RevisionAIService, RevisionAIUnavailable, configured_ai_service
from services.revision_analysis import (
    analyze_manuscript, citation_tokens, classify_comment, extract_reviewer_comments,
    map_comment, numeric_tokens,
)
from services.revision_docx import ApprovedPatch, UnsafeRevisionError, check_integrity, patch_manuscript, response_to_reviewers
from services.revision_storage import RevisionFileError, RevisionStorage, get_revision_storage, safe_filename, validate_revision_upload


MODES = ("CONSERVATIVE", "REVIEWER_DRIVEN", "FULL_ACADEMIC_POLISHING")
APPROVED = {"ACCEPTED", "EDITED"}
FINISHED_DECISIONS = APPROVED | {"REJECTED", "ALREADY_ADDRESSED", "RESOLVED"}
AUTHOR_REQUIRED = {"DATA_CHANGE", "REFERENCE", "EQUATION"}


class RevisionRuleError(ValueError):
    pass


def revision_tables_ready(session: Session) -> bool:
    expected = {"revision_jobs", "revision_review_files", "revision_comments", "revision_artifacts", "revision_integrity_checks"}
    return expected <= set(inspect(session.bind).get_table_names())


def _job(session: Session, job_id: uuid.UUID, journal: Journal, user: User) -> RevisionJob:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    job = session.scalar(select(RevisionJob).where(RevisionJob.id == job_id, RevisionJob.journal_id == journal.id))
    if job is None:
        raise AuthorizationError("Revision job was not found in the active journal.")
    return job


def create_revision_job(session: Session, journal: Journal, user: User, *, submission: Submission | None,
                        article_title: str, submission_identifier: str | None, revision_round: int,
                        mode: str, manuscript_filename: str, manuscript_content: bytes,
                        storage: RevisionStorage | None = None) -> RevisionJob:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    if submission and submission.journal_id != journal.id:
        raise AuthorizationError("Submission belongs to a different journal.")
    if not article_title.strip() or revision_round < 1 or revision_round > 99 or mode not in MODES:
        raise RevisionRuleError("Article title, valid revision round, and revision mode are required.")
    if mode == "FULL_ACADEMIC_POLISHING" and not settings.revision_allow_full_polishing:
        raise RevisionRuleError("Full Academic Polishing is disabled by configuration.")
    validate_revision_upload(manuscript_filename, manuscript_content, manuscript=True)
    analyze_manuscript(manuscript_content)
    if submission and session.scalar(select(RevisionJob.id).where(RevisionJob.submission_id == submission.id, RevisionJob.revision_round == revision_round)):
        raise RevisionRuleError("This submission already has a revision job for that round.")
    job = RevisionJob(journal_id=journal.id, submission_id=submission.id if submission else None,
                      article_title=article_title.strip(), submission_identifier=submission_identifier.strip() if submission_identifier else None,
                      revision_round=revision_round, mode=mode, status="DRAFT", original_filename=safe_filename(manuscript_filename),
                      created_by=user.id)
    session.add(job)
    session.flush()
    store = storage or get_revision_storage()
    job.original_storage_key, job.original_sha256 = store.put(job.id, "original", manuscript_content, ".docx")
    log_audit(session, action="REVISION_JOB_CREATED", object_type="revision_job", object_id=job.id, user_id=user.id, journal_id=journal.id,
              new={"submission_id": str(job.submission_id) if job.submission_id else None, "round": revision_round, "mode": mode})
    log_audit(session, action="REVISION_MANUSCRIPT_UPLOADED", object_type="revision_job", object_id=job.id, user_id=user.id, journal_id=journal.id,
              new={"filename": job.original_filename, "sha256": job.original_sha256})
    session.commit()
    return job


def add_reviewer_file(session: Session, journal: Journal, user: User, job_id: uuid.UUID, *, reviewer_label: str,
                      filename: str, content: bytes, storage: RevisionStorage | None = None) -> RevisionReviewFile:
    job = _job(session, job_id, journal, user)
    if job.status not in {"DRAFT", "EDITOR_REVIEW", "WAITING_AUTHOR_INPUT"} or job.comments:
        raise RevisionRuleError("Reviewer files cannot be replaced after analysis. Start a new revision round.")
    if not reviewer_label.strip() or len(reviewer_label) > 128:
        raise RevisionRuleError("A reviewer label is required.")
    suffix = validate_revision_upload(filename, content)
    store = storage or get_revision_storage()
    key, digest = store.put(job.id, "review", content, suffix)
    record = RevisionReviewFile(job_id=job.id, reviewer_label=reviewer_label.strip(), original_filename=safe_filename(filename),
                                storage_key=key, sha256=digest, uploaded_by=user.id)
    session.add(record)
    log_audit(session, action="REVISION_REVIEW_FILE_UPLOADED", object_type="revision_job", object_id=job.id,
              user_id=user.id, journal_id=journal.id, new={"reviewer": record.reviewer_label, "filename": record.original_filename, "sha256": digest})
    session.commit()
    return record


def analyze_revision_job(session: Session, journal: Journal, user: User, job_id: uuid.UUID, *, ai_consent: bool,
                         ai_service: RevisionAIService | None = None, storage: RevisionStorage | None = None) -> list[RevisionComment]:
    job = _job(session, job_id, journal, user)
    if job.comments:
        raise RevisionRuleError("This round has already been analyzed. Existing decisions are preserved.")
    if not job.review_files or not job.original_storage_key:
        raise RevisionRuleError("Upload the original DOCX and at least one reviewer file first.")
    store = storage or get_revision_storage()
    manuscript = store.read(job.original_storage_key)
    structure = analyze_manuscript(manuscript)
    job.status = "ANALYZING"
    job.ai_consent = bool(ai_consent and settings.revision_ai_enabled)
    try:
        ai = ai_service or configured_ai_service()
    except RevisionAIUnavailable:
        ai = DisabledRevisionAIService()
    records: list[RevisionComment] = []
    used_ai = False
    for file in job.review_files:
        source = store.read(file.storage_key)
        for extracted in extract_reviewer_comments(file.original_filename, source, file.reviewer_label):
            match = map_comment(extracted, structure)
            status = "AUTHOR_INPUT_REQUIRED" if extracted.author_input_required else "EDITOR_CONFIRMATION_REQUIRED"
            proposed = None
            reason = "Reviewer request may require new or corrected scientific data. Numerical or experimental values will not be changed automatically." if extracted.category == "DATA_CHANGE" else None
            if extracted.category == "REFERENCE":
                reason = "REFERENCE_REQUIRED: provide and verify a real source before revising citations."
            if extracted.category == "EQUATION":
                reason = "Equation-related requests require editor or author confirmation; Word equations remain untouched."
            if not extracted.author_input_required and match.paragraph_id and match.confidence >= 70 and job.ai_consent:
                try:
                    proposal = ai.generate_revision_proposal(reviewer_comment=extracted.raw_text,
                        paragraph_id=match.paragraph_id, original_text=match.original_text or "", mode=job.mode)
                    used_ai = True
                    if proposal.paragraph_id != match.paragraph_id or proposal.original_text != match.original_text:
                        raise RevisionAIUnavailable("AI proposal target did not match the manuscript.")
                    if proposal.requires_author_input:
                        status, reason = "AUTHOR_INPUT_REQUIRED", proposal.reason
                    elif numeric_tokens(proposal.original_text) != numeric_tokens(proposal.proposed_text) or citation_tokens(proposal.original_text) != citation_tokens(proposal.proposed_text):
                        status = "AUTHOR_INPUT_REQUIRED"
                        reason = "AI proposal changed a protected number or citation and was not accepted."
                    elif proposal.proposed_text != proposal.original_text:
                        status, proposed, reason = "PROPOSED", proposal.proposed_text, proposal.reason
                except RevisionAIUnavailable:
                    status, reason = "EDITOR_CONFIRMATION_REQUIRED", "Revision AI service is currently unavailable. Enter a proposal manually."
            record = RevisionComment(job_id=job.id, review_file_id=file.id, reviewer_label=extracted.reviewer,
                comment_number=extracted.number, raw_text=extracted.raw_text, category=extracted.category,
                severity=extracted.severity, suggested_section=match.section or extracted.suggested_section,
                target_paragraph_id=match.paragraph_id, mapping_confidence=match.confidence,
                original_text=match.original_text, proposed_text=proposed, reason=reason, status=status)
            session.add(record)
            records.append(record)
    if not records:
        raise RevisionRuleError("No reviewer comments were detected. Check the file content before analysis.")
    if job.mode == "FULL_ACADEMIC_POLISHING":
        reviewer_targets = {item.target_paragraph_id for item in records}
        candidates = [paragraph for paragraph in structure.paragraphs
                      if paragraph.patchable and paragraph.kind == "PARAGRAPH"
                      and paragraph.section_id != "FRONT_MATTER"
                      and paragraph.identifier not in reviewer_targets]
        if len(candidates) > 500:
            raise RevisionRuleError("Full Academic Polishing exceeds 500 safe paragraphs. Split the manuscript or use Reviewer-driven mode.")
        polish_ai_available = job.ai_consent
        for number, paragraph in enumerate(candidates, 1):
            status, proposed = "EDITOR_CONFIRMATION_REQUIRED", None
            reason = "Editor-selected language polish; no reviewer comment was invented. Enter or approve wording manually."
            if polish_ai_available:
                try:
                    suggestion = ai.generate_revision_proposal(
                        reviewer_comment="Editor-selected language polishing only. Improve grammar and clarity without changing scientific meaning, numbers, citations, or claims.",
                        paragraph_id=paragraph.identifier, original_text=paragraph.text, mode=job.mode)
                    used_ai = True
                    if suggestion.paragraph_id != paragraph.identifier or suggestion.original_text != paragraph.text:
                        raise RevisionAIUnavailable("AI proposal target did not match the manuscript.")
                    if numeric_tokens(paragraph.text) != numeric_tokens(suggestion.proposed_text) or citation_tokens(paragraph.text) != citation_tokens(suggestion.proposed_text):
                        reason = "AI polishing changed a protected number or citation and was discarded."
                    elif suggestion.requires_author_input:
                        status, reason = "AUTHOR_INPUT_REQUIRED", suggestion.reason
                    elif suggestion.proposed_text != paragraph.text:
                        status, proposed, reason = "PROPOSED", suggestion.proposed_text, suggestion.reason
                    else:
                        continue
                except RevisionAIUnavailable:
                    polish_ai_available = False
                    reason = "Revision AI service is currently unavailable. Enter polishing wording manually."
            record = RevisionComment(job_id=job.id, review_file_id=None, reviewer_label="Editorial polishing",
                comment_number=number, raw_text=f"Editor-selected Full Academic Polishing for {paragraph.identifier}.",
                source_type="EDITORIAL_POLISHING", category="LANGUAGE", severity="EDITORIAL",
                suggested_section=paragraph.section, target_paragraph_id=paragraph.identifier,
                mapping_confidence=100, original_text=paragraph.text, proposed_text=proposed,
                reason=reason, status=status)
            session.add(record)
            records.append(record)
    job.status = "WAITING_AUTHOR_INPUT" if records and all(item.status == "AUTHOR_INPUT_REQUIRED" for item in records) else "EDITOR_REVIEW"
    session.flush()
    session.expire(job, ["comments"])
    log_audit(session, action="REVISION_ANALYZED", object_type="revision_job", object_id=job.id, user_id=user.id, journal_id=journal.id,
              new={"comment_count": len(records), "reviewer_count": len(job.review_files), "ai_used": used_ai, "mode": job.mode})
    if used_ai:
        log_audit(session, action="REVISION_AI_ANALYSIS_EXECUTED", object_type="revision_job", object_id=job.id,
                  user_id=user.id, journal_id=journal.id, new={"provider": settings.revision_ai_provider, "comment_count": len(records)})
    session.commit()
    return records


def decide_comment(session: Session, journal: Journal, user: User, job_id: uuid.UUID, comment_id: uuid.UUID,
                   *, action: str, paragraph_id: str | None = None, proposed_text: str | None = None,
                   reason: str | None = None, author_input_confirmed: bool = False,
                   numeric_approval_reason: str | None = None, storage: RevisionStorage | None = None) -> RevisionComment:
    job = _job(session, job_id, journal, user)
    comment = session.scalar(select(RevisionComment).where(RevisionComment.id == comment_id, RevisionComment.job_id == job.id))
    if comment is None or job.status in {"COMPLETED", "CANCELLED"}:
        raise RevisionRuleError("Comment is unavailable for decision in this job.")
    if action not in {"ACCEPTED", "EDITED", "REJECTED", "AUTHOR_INPUT_REQUIRED", "ALREADY_ADDRESSED"}:
        raise RevisionRuleError("Unsupported revision decision.")
    previous = comment.status
    if action in APPROVED:
        store = storage or get_revision_storage()
        structure = analyze_manuscript(store.read(job.original_storage_key))
        target_id = paragraph_id or comment.target_paragraph_id
        target = next((para for para in structure.paragraphs if para.identifier == target_id), None)
        if target is None or not target.patchable:
            raise RevisionRuleError("Select a safe, editable manuscript paragraph before approving this revision.")
        already_targeted = session.scalar(select(RevisionComment.id).where(
            RevisionComment.job_id == job.id, RevisionComment.id != comment.id,
            RevisionComment.target_paragraph_id == target.identifier, RevisionComment.status.in_(APPROVED)))
        if already_targeted:
            raise RevisionRuleError("Another approved revision already targets this paragraph. Combine the requests in one proposal and mark the other already addressed.")
        replacement = (proposed_text if proposed_text is not None else comment.proposed_text) or ""
        if not replacement.strip() or replacement == target.text:
            raise RevisionRuleError("Approved revision must contain a changed, non-empty paragraph.")
        if citation_tokens(target.text) != citation_tokens(replacement):
            raise RevisionRuleError("Citation identifiers cannot be changed through automatic paragraph patching.")
        if comment.category in AUTHOR_REQUIRED and not (author_input_confirmed and reason and reason.strip()):
            raise RevisionRuleError("Scientific, equation, and reference requests require confirmed author input and a documented source before approval.")
        if numeric_tokens(target.text) != numeric_tokens(replacement) and not (author_input_confirmed and numeric_approval_reason and numeric_approval_reason.strip()):
            raise RevisionRuleError("Numerical changes require explicit author confirmation and an approval reason.")
        comment.target_paragraph_id = target.identifier
        comment.suggested_section = target.section
        comment.original_text = target.text
        comment.proposed_text = replacement
        comment.author_input_confirmed = author_input_confirmed
        comment.numeric_approval_reason = numeric_approval_reason.strip() if numeric_approval_reason else None
        comment.reason = reason.strip() if reason else comment.reason
    elif action in {"REJECTED", "ALREADY_ADDRESSED"} and not (reason and reason.strip()):
        raise RevisionRuleError("A justification is required for rejection or already-addressed decisions.")
    else:
        comment.decision_reason = reason.strip() if reason else None
    comment.status = action
    comment.decided_by = user.id
    comment.decided_at = datetime.now(timezone.utc)
    if action in {"REJECTED", "ALREADY_ADDRESSED"}:
        comment.decision_reason = reason.strip()
    session.flush()
    all_statuses = list(session.scalars(select(RevisionComment.status).where(RevisionComment.job_id == job.id)))
    if any(status == "AUTHOR_INPUT_REQUIRED" for status in all_statuses):
        job.status = "WAITING_AUTHOR_INPUT"
    elif all(status in FINISHED_DECISIONS for status in all_statuses):
        job.status = "READY_TO_GENERATE"
    else:
        job.status = "EDITOR_REVIEW"
    log_audit(session, action=f"REVISION_PROPOSAL_{action}", object_type="revision_comment", object_id=comment.id,
              user_id=user.id, journal_id=journal.id, previous={"status": previous},
              new={"status": action, "job_id": str(job.id), "target": comment.target_paragraph_id,
                   "proposal_sha256": hashlib.sha256((comment.proposed_text or "").encode()).hexdigest() if action in APPROVED else None,
                   "author_input_confirmed": author_input_confirmed,
                   "numeric_change_approved": bool(comment.numeric_approval_reason)})
    session.commit()
    return comment


def _response_rows(job: RevisionJob) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index, comment in enumerate(job.comments, 1):
        if comment.source_type != "REVIEWER":
            continue
        location = f"{comment.suggested_section or 'Location to confirm'} / {comment.target_paragraph_id or 'Unmapped'}"
        if comment.status in APPROVED:
            responses = (
                "We revised the identified manuscript passage to address this comment.",
                "The specified paragraph has been updated in response to this point.",
                "We addressed this request through a targeted revision in the manuscript.",
            )
            response = responses[(index - 1) % len(responses)]
            made = f"Approved wording applied at {location}."
            status = "Addressed"
        elif comment.status == "ALREADY_ADDRESSED":
            response = f"The point is already addressed in the manuscript. {comment.decision_reason or ''}".strip()
            made, status = "No manuscript change made.", "Already addressed"
        elif comment.status == "REJECTED":
            response = f"We considered the request. No manuscript change was made. {comment.decision_reason or ''}".strip()
            made, status = "No manuscript change made.", "Rejected with justification"
        else:
            response, made, status = "Pending author input.", "No manuscript change made.", "Pending author input"
        rows.append({"reviewer": comment.reviewer_label, "number": str(comment.comment_number),
                     "raw_text": comment.raw_text, "response": response, "revision_made": made,
                     "location": location, "status": status})
    return rows


def draft_response_document(session: Session, journal: Journal, user: User, job_id: uuid.UUID) -> bytes:
    job = _job(session, job_id, journal, user)
    if not job.comments:
        raise RevisionRuleError("Analyze reviewer comments before preparing a draft response.")
    return response_to_reviewers(job.article_title, job.submission_identifier or "", job.revision_round,
                                 _response_rows(job), draft=True)


def _revision_log_pdf(job: RevisionJob, report_json: str) -> bytes:
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=42, rightMargin=42, topMargin=42, bottomMargin=42)
    story = [Paragraph("Revision Log and Integrity Report", styles["Title"]), Spacer(1, 12),
             Paragraph(escape(job.article_title), styles["Heading2"]),
             Paragraph(f"Revision round: {job.revision_round} | Job: {job.id}", styles["Normal"]), Spacer(1, 10)]
    for comment in job.comments:
        story.append(Paragraph(escape(f"{comment.reviewer_label} - Comment {comment.comment_number}: {comment.status}"), styles["Heading3"]))
        story.append(Paragraph(escape(comment.raw_text), styles["Normal"]))
        story.append(Paragraph(escape(f"Location: {comment.suggested_section or 'Unmapped'} / {comment.target_paragraph_id or '-'}"), styles["Normal"]))
        if comment.decision_reason:
            story.append(Paragraph(escape(f"Decision reason: {comment.decision_reason}"), styles["Normal"]))
        story.append(Spacer(1, 8))
    report = json.loads(report_json)
    story.append(Paragraph("Integrity check", styles["Heading2"]))
    for key, value in report["checks"].items():
        story.append(Paragraph(escape(f"{key.replace('_', ' ').title()}: {value}"), styles["Normal"]))
    story.append(Paragraph(escape(f"Overall: {'PASS' if report['passed'] else 'FAIL'}"), styles["Heading2"]))
    document.build(story)
    return buffer.getvalue()


def generate_revision_artifacts(session: Session, journal: Journal, user: User, job_id: uuid.UUID,
                                *, storage: RevisionStorage | None = None) -> list[RevisionArtifact]:
    job = _job(session, job_id, journal, user)
    if job.status not in {"READY_TO_GENERATE", "GENERATED"} or not job.comments:
        raise RevisionRuleError("All reviewer comments require an editor decision before document generation.")
    if any(comment.status not in FINISHED_DECISIONS for comment in job.comments):
        raise RevisionRuleError("Unreviewed or author-input-required comments block final manuscript generation.")
    store = storage or get_revision_storage()
    original = store.read(job.original_storage_key)
    if hashlib.sha256(original).hexdigest() != job.original_sha256:
        raise RevisionRuleError("Original manuscript hash changed in storage; generation stopped.")
    patches = [ApprovedPatch(comment.target_paragraph_id, comment.original_text, comment.proposed_text,
                             comment.numeric_approval_reason)
               for comment in job.comments if comment.status in APPROVED]
    clean = patch_manuscript(original, patches, highlight=False)
    revised = patch_manuscript(original, patches, highlight=True, highlight_color=settings.revision_highlight_style)
    clean_check = check_integrity(original, clean, patches)
    revised_check = check_integrity(original, revised, patches)
    check_data = {"passed": clean_check.passed and revised_check.passed,
                  "checks": clean_check.checks, "revised_checks": revised_check.checks,
                  "warnings": list(clean_check.warnings)}
    version = int(session.scalar(select(func.max(RevisionArtifact.version)).where(RevisionArtifact.job_id == job.id)) or 0) + 1
    session.add(RevisionIntegrityCheck(job_id=job.id, artifact_version=version, passed=check_data["passed"],
                                       result_json=json.dumps(check_data, ensure_ascii=False)))
    if not check_data["passed"]:
        log_audit(session, action="REVISION_INTEGRITY_FAILED", object_type="revision_job", object_id=job.id,
                  user_id=user.id, journal_id=journal.id, new={"version": version, "failed_checks": [key for key, value in clean_check.checks.items() if value is False]})
        session.commit()
        raise UnsafeRevisionError("Revision integrity check failed. No final artifacts were stored.")
    response = response_to_reviewers(job.article_title, job.submission_identifier or "", job.revision_round, _response_rows(job))
    log_pdf = _revision_log_pdf(job, json.dumps(check_data, ensure_ascii=False))
    outputs = (
        ("REVISED_MANUSCRIPT", "Revised_Manuscript.docx", revised, ".docx"),
        ("CLEAN_MANUSCRIPT", "Clean_Manuscript.docx", clean, ".docx"),
        ("RESPONSE_TO_REVIEWERS", "Response_to_Reviewers.docx", response, ".docx"),
        ("REVISION_LOG", "Revision_Log.pdf", log_pdf, ".pdf"),
    )
    artifacts = []
    for kind, filename, content, suffix in outputs:
        key, digest = store.put(job.id, kind.lower(), content, suffix)
        artifact = RevisionArtifact(job_id=job.id, kind=kind, version=version, filename=filename,
                                    storage_key=key, sha256=digest, created_by=user.id)
        session.add(artifact)
        artifacts.append(artifact)
    job.status = "GENERATED"
    log_audit(session, action="REVISION_ARTIFACTS_GENERATED", object_type="revision_job", object_id=job.id,
              user_id=user.id, journal_id=journal.id, new={"version": version, "artifact_count": len(artifacts), "integrity_passed": True})
    session.commit()
    return artifacts


def complete_revision_job(session: Session, journal: Journal, user: User, job_id: uuid.UUID) -> RevisionJob:
    job = _job(session, job_id, journal, user)
    if job.status != "GENERATED":
        raise RevisionRuleError("Generate and inspect all revision artifacts before completing the job.")
    latest = session.scalar(select(RevisionIntegrityCheck).where(RevisionIntegrityCheck.job_id == job.id).order_by(RevisionIntegrityCheck.created_at.desc()))
    if not latest or not latest.passed:
        raise RevisionRuleError("A passing integrity check is required for completion.")
    job.status = "COMPLETED"
    log_audit(session, action="REVISION_COMPLETED", object_type="revision_job", object_id=job.id,
              user_id=user.id, journal_id=journal.id, new={"integrity_check_id": str(latest.id)})
    session.commit()
    return job


def authorized_artifact_bytes(session: Session, journal: Journal, user: User, artifact_id: uuid.UUID,
                              *, storage: RevisionStorage | None = None) -> tuple[str, bytes]:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    artifact = session.scalar(select(RevisionArtifact).join(RevisionJob).where(RevisionArtifact.id == artifact_id, RevisionJob.journal_id == journal.id))
    if artifact is None:
        raise AuthorizationError("Revision artifact is not available in this journal.")
    content = (storage or get_revision_storage()).read(artifact.storage_key)
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise RevisionRuleError("Revision artifact hash mismatch.")
    return artifact.filename, content


def authorized_original_bytes(session: Session, journal: Journal, user: User, job_id: uuid.UUID,
                              *, storage: RevisionStorage | None = None) -> tuple[str, bytes]:
    job = _job(session, job_id, journal, user)
    content = (storage or get_revision_storage()).read(job.original_storage_key)
    if hashlib.sha256(content).hexdigest() != job.original_sha256:
        raise RevisionRuleError("Original manuscript hash mismatch.")
    return job.original_filename, content


def revision_history(session: Session, journal: Journal, user: User, job_id: uuid.UUID) -> list[AuditLog]:
    job = _job(session, job_id, journal, user)
    object_ids = [str(job_id), *(str(comment.id) for comment in job.comments)]
    return list(session.scalars(select(AuditLog).where(AuditLog.journal_id == journal.id,
        AuditLog.object_id.in_(object_ids), AuditLog.action.like("REVISION_%")).order_by(AuditLog.created_at.desc())))
