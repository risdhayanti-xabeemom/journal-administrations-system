"""Revision views rendered inside the authenticated JAS application."""

from __future__ import annotations

import uuid
import json

import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from models import Journal, RevisionIntegrityCheck, RevisionJob, Role, Submission, User
from services.core import AuthorizationError, assert_journal_access
from services.revision_analysis import analyze_manuscript
from services.revision_docx import UnsafeRevisionError
from services.revision_service import (
    MODES, RevisionRuleError, add_reviewer_file, analyze_revision_job, authorized_artifact_bytes,
    authorized_original_bytes, complete_revision_job, create_revision_job, decide_comment,
    draft_response_document, generate_revision_artifacts, revision_history, revision_tables_ready,
)
from services.revision_storage import RevisionFileError, StorageUnavailable, get_revision_storage


def _ready(session: Session, journal: Journal, user: User) -> bool:
    try:
        assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
        if not revision_tables_ready(session):
            st.error("Revision tables are not installed. Back up the database and run scripts/migrate_revisions.py. Other JAS modules remain available.")
            return False
        return True
    except (AuthorizationError, StorageUnavailable) as exc:
        st.error(str(exc))
        return False


def _jobs(session: Session, journal: Journal) -> list[RevisionJob]:
    return list(session.scalars(select(RevisionJob).where(RevisionJob.journal_id == journal.id).order_by(RevisionJob.updated_at.desc())))


def _settings_caption() -> None:
    with st.expander("Revision settings and privacy", expanded=False):
        st.write(f"AI enabled: {'ON' if settings.revision_ai_enabled else 'OFF'}")
        st.write(f"AI provider/model: {settings.revision_ai_provider} / {settings.revision_ai_model or 'not configured'}")
        st.write(f"Maximum file size: {settings.revision_max_file_bytes / 1024 / 1024:.0f} MB")
        st.write(f"Default mode: {settings.revision_default_mode}")
        st.write(f"Revision marker: {settings.revision_highlight_style} highlight (not native Track Changes)")
        st.write(f"Full Academic Polishing allowed: {'YES' if settings.revision_allow_full_polishing else 'NO'}")
        if settings.revision_storage_backend == "local":
            st.warning("Local private storage may be temporary on Streamlit Cloud. Configure a persistent private storage adapter before relying on cloud retention.")
        st.caption("When AI is enabled and consent is given, only reviewer comment text and mapped manuscript paragraph text are sent to the configured provider. No manuscript file is sent as a binary upload.")


def _create_job(session: Session, journal: Journal, user: User) -> None:
    submissions = list(session.scalars(select(Submission).where(Submission.journal_id == journal.id).order_by(Submission.created_at.desc())))
    with st.expander("Create revision job", expanded=not bool(_jobs(session, journal))):
        with st.form("revision-create"):
            option = st.selectbox("Submission", [None, *[item.id for item in submissions]],
                                  format_func=lambda value: "Standalone article" if value is None else next(f"{item.ojs_submission_id or 'Manual'} · {item.manuscript_title[:90]}" for item in submissions if item.id == value))
            selected = next((item for item in submissions if item.id == option), None)
            title = st.text_input("Article title", value=selected.manuscript_title if selected else "")
            identifier = st.text_input("Submission ID", value=selected.ojs_submission_id or "" if selected else "")
            round_number = st.number_input("Revision round", min_value=1, max_value=99, value=1, step=1)
            choices = [mode for mode in MODES if mode != "FULL_ACADEMIC_POLISHING" or settings.revision_allow_full_polishing]
            mode = st.selectbox("Revision mode", choices,
                                index=choices.index(settings.revision_default_mode) if settings.revision_default_mode in choices else choices.index("REVIEWER_DRIVEN"))
            manuscript = st.file_uploader("Original manuscript DOCX in journal template", type=["docx", "pdf"], help="Approved edits are patched into this original DOCX to retain its journal layout. A PDF cannot be safely patched; final revision requires DOCX.")
            reviewer_one = st.file_uploader("Reviewer 1", type=["docx", "pdf", "txt"])
            reviewer_two = st.file_uploader("Reviewer 2", type=["docx", "pdf", "txt"])
            extra = st.file_uploader("Additional reviewer files", type=["docx", "pdf", "txt"], accept_multiple_files=True)
            editor_file = st.file_uploader("Editor comments", type=["docx", "pdf", "txt"])
            submit = st.form_submit_button("Create revision job", type="primary")
        if submit:
            if not manuscript:
                st.error("Upload the original manuscript DOCX.")
                return
            try:
                job = create_revision_job(session, journal, user, submission=selected, article_title=title,
                    submission_identifier=identifier, revision_round=int(round_number), mode=mode,
                    manuscript_filename=manuscript.name, manuscript_content=manuscript.getvalue())
                uploads = [("Reviewer 1", reviewer_one), ("Reviewer 2", reviewer_two), ("Editor", editor_file)]
                uploads.extend((f"Reviewer {index + 3}", file) for index, file in enumerate(extra or []))
                for label, file in uploads:
                    if file:
                        add_reviewer_file(session, journal, user, job.id, reviewer_label=label,
                                          filename=file.name, content=file.getvalue())
                st.session_state.revision_active_job = str(job.id)
                st.success("Revision job created. The original manuscript is retained unchanged.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                st.error(str(exc))


def _job_selector(session: Session, journal: Journal) -> RevisionJob | None:
    jobs = _jobs(session, journal)
    if not jobs:
        return None
    options = [str(job.id) for job in jobs]
    active = st.session_state.get("revision_active_job")
    index = options.index(active) if active in options else 0
    selected_id = st.selectbox("Revision job", options, index=index,
        format_func=lambda value: next(f"Round {job.revision_round} · {job.article_title[:85]} · {job.status}" for job in jobs if str(job.id) == value))
    st.session_state.revision_active_job = selected_id
    return next(job for job in jobs if str(job.id) == selected_id)


def _job_detail(session: Session, journal: Journal, user: User, job: RevisionJob) -> None:
    st.subheader(f"Round {job.revision_round} · {job.article_title}")
    st.caption(f"Status: {job.status} · Mode: {job.mode} · Submission: {job.submission_identifier or 'Standalone'}")
    storage = get_revision_storage()
    try:
        _, original = authorized_original_bytes(session, journal, user, job.id, storage=storage)
        structure = analyze_manuscript(original)
    except Exception as exc:
        st.error(str(exc))
        return
    with st.expander("Manuscript structure", expanded=False):
        st.write(f"Paragraphs: {len(structure.paragraphs)} · Tables: {structure.table_count} · Figures: {structure.figure_count} · Equations: {structure.equation_count} · References: {len(structure.references)}")
        st.dataframe(pd.DataFrame([{"ID": para.identifier, "Section": para.section, "Kind": para.kind,
                                   "Patchable": para.patchable, "Text": para.text[:200]} for para in structure.paragraphs]),
                     use_container_width=True, hide_index=True)
    if not job.comments:
        st.write(f"Reviewer files: {len(job.review_files)}")
        if job.status == "DRAFT":
            with st.expander("Add another reviewer file"):
                label = st.text_input("Reviewer label", key=f"label-{job.id}")
                file = st.file_uploader("Reviewer DOCX, PDF, or TXT", type=["docx", "pdf", "txt"], key=f"extra-{job.id}")
                if st.button("Add reviewer file", key=f"add-{job.id}") and file:
                    try:
                        add_reviewer_file(session, journal, user, job.id, reviewer_label=label,
                                          filename=file.name, content=file.getvalue(), storage=storage)
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        st.error(str(exc))
        consent = st.checkbox("I authorize sending reviewer comment text and mapped manuscript paragraph text to the configured AI provider for this analysis.",
                              disabled=not settings.revision_ai_enabled)
        if not settings.revision_ai_enabled:
            st.info("Revision AI service is currently unavailable. The editor can enter proposals manually after comment extraction.")
        if st.button("Analyze Reviews", type="primary", disabled=not job.review_files):
            try:
                analyze_revision_job(session, journal, user, job.id, ai_consent=consent, storage=storage)
                st.success("Reviewer comments extracted and mapped. Review each proposed change before generating documents.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                st.error(str(exc))
        return
    _review_comments(session, journal, user, job, structure, storage)
    _artifacts(session, journal, user, job, storage)


def _review_comments(session: Session, journal: Journal, user: User, job: RevisionJob, structure, storage) -> None:
    st.subheader("Review comments and proposed revisions")
    reviewers = sorted({item.reviewer_label for item in job.comments})
    categories = sorted({item.category for item in job.comments})
    statuses = sorted({item.status for item in job.comments})
    a, b, c = st.columns(3)
    reviewer_filter = a.selectbox("Reviewer", ["ALL", *reviewers])
    category_filter = b.selectbox("Category", ["ALL", *categories])
    status_filter = c.selectbox("Status", ["ALL", *statuses])
    only_author = st.checkbox("Author input required only")
    editable = [para for para in structure.paragraphs if para.patchable]
    by_id = {para.identifier: para for para in editable}
    for comment in job.comments:
        if reviewer_filter != "ALL" and comment.reviewer_label != reviewer_filter:
            continue
        if category_filter != "ALL" and comment.category != category_filter:
            continue
        if status_filter != "ALL" and comment.status != status_filter:
            continue
        if only_author and comment.status != "AUTHOR_INPUT_REQUIRED":
            continue
        with st.expander(f"{comment.reviewer_label} · Comment {comment.comment_number} · {comment.category} · {comment.status}", expanded=comment.status in {"PROPOSED", "EDITOR_CONFIRMATION_REQUIRED"}):
            st.write("**Reviewer Comment**")
            st.write(comment.raw_text)
            st.write(f"**Severity:** {comment.severity} · **Mapping confidence:** {comment.mapping_confidence}%")
            st.write(f"**Mapped location:** {comment.suggested_section or 'Uncertain'} / {comment.target_paragraph_id or 'Editor confirmation required'}")
            st.caption("Page estimate is unavailable without a rendered Word layout.")
            if comment.category == "DATA_CHANGE":
                st.error("Reviewer request may require new or corrected scientific data. Numerical or experimental values will not be changed automatically.")
            if comment.category == "REFERENCE":
                st.warning("REFERENCE_REQUIRED: provide and verify the source. No citation will be fabricated.")
            target_ids = [None, *by_id]
            current = comment.target_paragraph_id if comment.target_paragraph_id in by_id else None
            target = st.selectbox("Manuscript location", target_ids, index=target_ids.index(current),
                format_func=lambda value: "— Select a paragraph —" if value is None else f"{value} · {by_id[value].section} · {by_id[value].text[:80]}",
                key=f"rev-target-{comment.id}")
            original_text = by_id[target].text if target else comment.original_text or ""
            st.write("**Original text**")
            st.code(original_text or "No safe paragraph selected.", language=None)
            proposed = st.text_area("Proposed revision", value=comment.proposed_text or original_text,
                                    key=f"rev-proposal-{comment.id}", height=150)
            st.write(f"**Reason:** {comment.reason or 'Editor to provide a reason.'}")
            action = st.selectbox("Decision", ["ACCEPTED", "EDITED", "REJECTED", "AUTHOR_INPUT_REQUIRED", "ALREADY_ADDRESSED"],
                                  key=f"rev-action-{comment.id}")
            reason = st.text_area("Decision reason / source of author input", key=f"rev-reason-{comment.id}")
            author_confirmed = st.checkbox("Author supplied and confirmed any new scientific facts", key=f"rev-author-{comment.id}")
            numeric_reason = st.text_input("Explicit numeric-change approval and source (only if values change)", key=f"rev-numeric-{comment.id}")
            if st.button("Save editor decision", key=f"rev-save-{comment.id}", type="primary"):
                try:
                    decide_comment(session, journal, user, job.id, comment.id, action=action, paragraph_id=target,
                                   proposed_text=proposed, reason=reason, author_input_confirmed=author_confirmed,
                                   numeric_approval_reason=numeric_reason, storage=storage)
                    st.success("Decision saved and audited. No manuscript was patched yet.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    st.error(str(exc))


def _artifacts(session: Session, journal: Journal, user: User, job: RevisionJob, storage) -> None:
    st.subheader("Integrity check and generation")
    counts = {status: sum(item.status == status for item in job.comments) for status in sorted({item.status for item in job.comments})}
    st.write({"Reviewer comments": sum(item.source_type == "REVIEWER" for item in job.comments),
              "Editorial polish proposals": sum(item.source_type == "EDITORIAL_POLISHING" for item in job.comments), "Decisions": counts,
              "Unreviewed": sum(item.status in {"UNREVIEWED", "PROPOSED", "EDITOR_CONFIRMATION_REQUIRED"} for item in job.comments),
              "Author input required": counts.get("AUTHOR_INPUT_REQUIRED", 0)})
    st.download_button("Download draft Response to Reviewers (pending items marked)",
        draft_response_document(session, journal, user, job.id), file_name="Draft_Response_to_Reviewers.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    latest_check = session.scalar(select(RevisionIntegrityCheck).where(RevisionIntegrityCheck.job_id == job.id).order_by(RevisionIntegrityCheck.created_at.desc()))
    if latest_check:
        st.success("Latest integrity check passed.") if latest_check.passed else st.error("Latest integrity check failed. Finalization is blocked.")
        with st.expander("Full integrity results"):
            st.json(json.loads(latest_check.result_json))
    if st.button("Generate Revised Manuscript, Clean Manuscript, Response, and Revision Log", type="primary",
                 disabled=job.status not in {"READY_TO_GENERATE", "GENERATED"}):
        try:
            generate_revision_artifacts(session, journal, user, job.id, storage=storage)
            st.success("Artifacts generated from approved revisions only. Inspect them before completion.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            st.error(str(exc))
    if job.artifacts:
        st.write("**Authorized artifact downloads**")
        for artifact in sorted(job.artifacts, key=lambda item: (item.version, item.kind), reverse=True):
            try:
                filename, content = authorized_artifact_bytes(session, journal, user, artifact.id, storage=storage)
                st.download_button(f"Version {artifact.version} · {filename}", content, file_name=filename,
                                   mime="application/pdf" if filename.endswith(".pdf") else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                   key=f"rev-download-{artifact.id}")
            except Exception as exc:
                st.error(str(exc))
    if job.status == "GENERATED" and st.button("Mark revision complete after document inspection"):
        try:
            complete_revision_job(session, journal, user, job.id)
            st.success("Revision job completed.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            st.error(str(exc))


def quick_revision_page(session: Session, journal: Journal, user: User) -> None:
    st.title("Reviewer Revision")
    st.info("This workflow addresses reviewer comments. For journal template formatting, use REVISION · Quick Template Revision. Only editor-approved text changes are patched into the uploaded DOCX.")
    if not _ready(session, journal, user):
        return
    _settings_caption()
    _create_job(session, journal, user)
    job = _job_selector(session, journal)
    if job:
        _job_detail(session, journal, user, job)


def revision_jobs_page(session: Session, journal: Journal, user: User) -> None:
    st.title("Revision Jobs")
    if not _ready(session, journal, user):
        return
    jobs = _jobs(session, journal)
    rows = []
    for job in jobs:
        resolved = sum(item.status in {"ACCEPTED", "EDITED", "REJECTED", "ALREADY_ADDRESSED", "RESOLVED"} for item in job.comments)
        rows.append({"Job ID": str(job.id), "Submission ID": job.submission_identifier or "Standalone",
                     "Article Title": job.article_title, "Journal": journal.abbreviation, "Round": job.revision_round,
                     "Reviewers": len({item.reviewer_label for item in job.review_files}), "Comments": len(job.comments),
                     "Resolved": resolved, "Pending author": sum(item.status == "AUTHOR_INPUT_REQUIRED" for item in job.comments),
                     "Progress": f"{resolved / len(job.comments):.0%}" if job.comments else "0%",
                     "Status": job.status, "Last updated": str(job.updated_at)})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    for job in jobs:
        if st.button(f"Open / Continue · Round {job.revision_round} · {job.article_title[:60]}", key=f"open-revision-{job.id}"):
            st.session_state.revision_active_job = str(job.id)
            st.session_state.revision_go_to_quick = True
            st.rerun()


def revision_history_page(session: Session, journal: Journal, user: User) -> None:
    st.title("Revision History")
    if not _ready(session, journal, user):
        return
    job = _job_selector(session, journal)
    if not job:
        return
    events = revision_history(session, journal, user, job.id)
    st.dataframe(pd.DataFrame([{"Time": event.created_at, "Action": event.action,
                                "User ID": str(event.user_id) if event.user_id else "System",
                                "Object": event.object_type, "Details": event.new_value or ""} for event in events]),
                 use_container_width=True, hide_index=True)
    st.write("**Immutable artifact versions**")
    st.dataframe(pd.DataFrame([{"Version": artifact.version, "Kind": artifact.kind,
                                "SHA-256": artifact.sha256, "Created": artifact.created_at} for artifact in job.artifacts]),
                 use_container_width=True, hide_index=True)
