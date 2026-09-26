"""Authenticated Streamlit views for article-template formatting."""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import ArticleRevisionJob, Journal, Role, Submission, User
from services.article_metadata import elkolind_master_warnings, short_title_4w
from services.article_formatting import compliance_score
from services.article_revision_service import (
    accept_all_low_risk_review_findings, approve_article_fixes, article_revision_history,
    article_revision_jobs, article_revision_tables_ready, auto_format_and_generate,
    audit_article_revision_job, authorized_article_artifact_bytes, complete_article_revision_job,
    create_article_revision_job, generate_formatted_manuscript, job_effective_findings, job_findings,
    job_auto_format_result, job_fast_findings, job_resolution_summary, job_review_decisions,
    resolve_article_review_findings,
    review_finding_can_apply_fix,
    select_active_article_template_for_job, update_article_job_metadata,
)
from services.article_template_service import (
    ELKOLIND_INITIAL_RULES,
    activate_article_template, active_article_template, article_template_config, article_template_versions,
    authorized_article_template_bytes, update_article_rules, upload_article_template,
)
from services.core import assert_journal_access
from services.docx_templates import convert_docx_to_pdf


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _ready(session: Session, journal: Journal, user: User) -> bool:
    try:
        assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
        if not article_revision_tables_ready(session):
            st.error("Article revision tables are not installed. Back up the database and run scripts/migrate_article_revisions.py. Other JAS modules remain available.")
            return False
        return True
    except Exception as exc:
        st.error(str(exc))
        return False


def article_templates_panel(session: Session, journal: Journal, user: User, show_pdf) -> None:
    st.subheader("Master Article Template")
    try:
        assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    except Exception as exc:
        st.error(str(exc))
        return
    current = active_article_template(session, journal.id)
    a, b, c = st.columns(3)
    a.metric("Journal", journal.abbreviation)
    b.metric("Article template", "ACTIVE" if current else "NOT CONFIGURED")
    c.metric("Version", f"v{current.version}" if current else "—")
    st.caption("Upload the official article DOCX for this journal. Each upload creates a retained version in private storage; it does not affect the LoA template.")
    if current:
        st.write(f"**Current:** {current.original_filename} · Uploaded {current.uploaded_at} · By {current.uploader.display_name if current.uploader else 'Unknown'}")
        try:
            _, content = authorized_article_template_bytes(session, journal, user, current.id)
            if journal.abbreviation.upper() == "ELKOLIND":
                warnings = elkolind_master_warnings(content)
                if warnings:
                    for warning in warnings:
                        st.warning(warning)
                else:
                    st.success("ELKOLIND header/footer variants and metadata placeholders are configured. Inspect the visual preview before use.")
            actions = st.columns(2)
            actions[0].download_button("Download Current Article Template", content, file_name=current.original_filename,
                                       mime=DOCX_MIME, key=f"article-template-download-{current.id}")
            if actions[1].button("Preview Article Template", key=f"article-template-preview-{current.id}"):
                with tempfile.TemporaryDirectory(prefix="jas-article-preview-") as folder:
                    source = Path(folder) / "Article_Template.docx"
                    target = Path(folder) / "Article_Template.pdf"
                    source.write_bytes(content)
                    try:
                        convert_docx_to_pdf(source, target)
                        show_pdf(target, height=700)
                    except Exception as exc:
                        st.warning(f"Visual preview requires a configured DOCX-to-PDF converter: {exc}")
        except Exception as exc:
            st.error(str(exc))
        config = article_template_config(current)
        with st.expander("Template Rules", expanded=False):
            st.caption("Optional semantic checks are separate from the DOCX visual style profile. Empty rules do not invent requirements.")
            raw = st.text_area("Journal article rules (JSON)", value=json.dumps(config.get("rules", {}), ensure_ascii=False, indent=2),
                               height=210, key=f"article-rules-{current.id}")
            st.code(json.dumps({"required_sections": ["Introduction", "Conclusion", "References"],
                "abstract_max_words": 250, "keywords_min": 3, "keywords_max": 6,
                "figure_caption_prefix": "Figure", "table_caption_prefix": "Table"}, indent=2), language="json")
            if st.button("Save Rules as New Template Version", key=f"article-rules-save-{current.id}"):
                try:
                    update_article_rules(session, journal, user, current.id, json.loads(raw))
                    st.success("Article rules saved as a new retained template version and audited.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    st.error(str(exc))
        with st.expander("Extracted style profile", expanded=False):
            st.json(config["profile"])
    with st.expander("Upload Article Template" if not current else "Replace Article Template", expanded=not bool(current)):
        file = st.file_uploader("Official Article Template DOCX", type=["docx"], key=f"article-template-upload-{journal.id}")
        default_rules = ELKOLIND_INITIAL_RULES if journal.abbreviation.upper() == "ELKOLIND" else {}
        rules_text = st.text_area("Initial template rules (JSON)", value=json.dumps(default_rules, indent=2),
                                  key=f"article-template-new-rules-{journal.id}")
        if st.button("Upload and activate Article Template", type="primary", key=f"article-template-submit-{journal.id}"):
            if not file:
                st.error("Choose an official article DOCX first.")
            else:
                try:
                    created = upload_article_template(session, journal, user, filename=file.name,
                        content=file.getvalue(), rules=json.loads(rules_text))
                    st.success(f"Article template v{created.version} activated.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    st.error(str(exc))
    versions = article_template_versions(session, journal.id)
    if versions:
        with st.expander("Article template version history / Activate Version"):
            for template in versions:
                cols = st.columns([3, 1, 1, 1])
                cols[0].write(f"v{template.version} · {template.original_filename} · {template.uploaded_at}")
                cols[1].write(template.status.value)
                try:
                    _, content = authorized_article_template_bytes(session, journal, user, template.id)
                    cols[2].download_button("DOCX", content, file_name=template.original_filename,
                        mime=DOCX_MIME, key=f"article-template-version-dl-{template.id}")
                except Exception as exc:
                    cols[2].error(str(exc))
                if template.status.value != "ACTIVE" and cols[3].button("Activate", key=f"article-template-activate-{template.id}"):
                    try:
                        activate_article_template(session, journal, user, template.id)
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        st.error(str(exc))


def _job_selector(session: Session, journal: Journal, user: User) -> ArticleRevisionJob | None:
    jobs = article_revision_jobs(session, journal, user)
    if not jobs:
        return None
    ids = [str(job.id) for job in jobs]
    current = st.session_state.get("article_revision_job")
    selected = st.selectbox("Template revision job", ids, index=ids.index(current) if current in ids else 0,
        format_func=lambda value: next(f"{job.article_title[:70]} · v{job.template.version} · {job.status}" for job in jobs if str(job.id) == value))
    st.session_state.article_revision_job = selected
    return next(job for job in jobs if str(job.id) == selected)


def _finding_rows(findings) -> list[dict[str, str]]:
    return [{"Category": item.category, "Check": item.check, "Expected": item.expected,
        "Detected": item.detected, "Status": item.status, "Location": item.target or "Document"}
        | ({"Protected object": item.protected_object_type,
            "Preservation reason": item.preservation_reason}
           if item.protected_object_type or item.preservation_reason else {})
        for item in findings]


def _render_fast_result(session: Session, journal: Journal, user: User,
                        job: ArticleRevisionJob) -> None:
    findings = job_fast_findings(job)
    stored = job_auto_format_result(job)
    result = stored or {
        "auto_fixed": sum(item.status == "AUTO_FIXED" for item in findings),
        "already_compliant": sum(item.status == "COMPLIANT" for item in findings),
        "protected_preserved": len({item.target for item in findings
            if item.status in {"COMPLIANT_PROTECTED", "WARNING_PRESERVED"}
            and item.protected_object_type and item.target}),
        "warnings": sum(item.status in {"WARNING", "WARNING_PRESERVED", "REVIEW_REQUIRED"}
                        for item in findings),
        "blocking_issues": sum(item.status == "BLOCKING" for item in findings),
        "blocking_codes": [],
    }
    st.write("### AUTO FORMAT RESULT")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Auto-fixed", result.get("auto_fixed", 0))
    m2.metric("Already compliant", result.get("already_compliant", 0))
    m3.metric("Protected elements preserved", result.get("protected_preserved", 0))
    m4.metric("Warnings", result.get("warnings", 0))
    m5.metric("Blocking issues", result.get("blocking_issues", 0))
    if job.status in {"FORMATTED", "COMPLETED"} and not result.get("blocking_issues"):
        st.success(
            f"Formatting completed. Auto fixes applied: {result.get('auto_fixed', 0)} · "
            f"Protected elements preserved: {result.get('protected_preserved', 0)} · "
            f"Warnings: {result.get('warnings', 0)} · Blocking issues: 0"
        )
    blocking_codes = result.get("blocking_codes") or []
    if blocking_codes:
        st.error("Generation stopped for: " + ", ".join(blocking_codes))

    review_items = [item for item in job_findings(job) if item.status == "REVIEW_REQUIRED"]
    low_risk = [item for item in review_items if review_finding_can_apply_fix(item)]
    if review_items and job.status in {"ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT"}:
        if st.button("Accept All Low-Risk Suggestions", disabled=not bool(low_risk),
                     key=f"article-fast-low-risk-{job.id}"):
            try:
                accept_all_low_risk_review_findings(session, journal, user, job.id)
                st.success("Low-risk formatting suggestions were recorded. Scientific-content changes were excluded.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                st.error(str(exc))
        if not low_risk:
            st.caption("Remaining review items are ambiguous and were not batch-accepted.")

    exceptions = [item for item in findings
                  if item.status in {"WARNING", "WARNING_PRESERVED", "REVIEW_REQUIRED", "BLOCKING"}]
    if exceptions:
        with st.expander("View Warnings", expanded=bool(blocking_codes)):
            st.dataframe(pd.DataFrame(_finding_rows(exceptions)), use_container_width=True, hide_index=True)
    with st.expander("Show Details", expanded=False):
        st.caption("Formatting findings are collapsed by default so editors can focus on exceptions.")
        st.dataframe(pd.DataFrame(_finding_rows(findings)), use_container_width=True, hide_index=True)


def quick_template_revision_page(session: Session, journal: Journal, user: User) -> None:
    st.title("Quick Template Revision")
    if not _ready(session, journal, user):
        return
    template = active_article_template(session, journal.id)
    st.write(f"**Target journal:** {journal.abbreviation}")
    st.write(f"**Active Article Template:** {'v' + str(template.version) + ' · ' + template.original_filename if template else 'NOT CONFIGURED'}")
    st.caption("Select the active journal in the JAS sidebar. Template compliance scores measure formatting only, not scientific quality.")
    if not template:
        st.warning("Upload and activate this journal's official Article Template in Administration → Templates first.")
        return
    submissions = list(session.scalars(select(Submission).where(Submission.journal_id == journal.id)
                                       .order_by(Submission.created_at.desc()).limit(500)))
    with st.expander("Create template revision job", expanded=not bool(article_revision_jobs(session, journal, user))):
        with st.form("article-revision-create"):
            options = [None, *[item.id for item in submissions]]
            selected_id = st.selectbox("Submission (optional)", options,
                format_func=lambda value: "Standalone manuscript" if value is None else next(
                    f"{item.ojs_submission_id or 'Manual'} · {item.manuscript_title[:80]}" for item in submissions if item.id == value))
            submission = next((item for item in submissions if item.id == selected_id), None)
            title = st.text_input("Article title", value=submission.manuscript_title if submission else "")
            identifier = st.text_input("Submission ID", value=submission.ojs_submission_id or "" if submission else "")
            mode = st.selectbox("Revision mode", ["TEMPLATE_ONLY", "TEMPLATE_LANGUAGE_POLISH", "TEMPLATE_REVIEWER"],
                format_func=lambda item: {"TEMPLATE_ONLY": "Fast Auto Format (Template Only)", "TEMPLATE_LANGUAGE_POLISH": "Template + Language Polish",
                    "TEMPLATE_REVIEWER": "Template + Reviewer"}[item])
            manuscript = st.file_uploader("Original manuscript DOCX", type=["docx"], key="article-revision-manuscript")
            metadata: dict[str, str] = {}
            if journal.abbreviation.upper() == "ELKOLIND":
                st.write("**ELKOLIND article metadata**")
                st.caption("These values fill DOCX placeholders in the official master. Page numbers remain native Word PAGE fields. Short title is derived from the first four manuscript-title words.")
                defaults = {
                    "volume": submission.planned_volume if submission else "",
                    "issue": submission.planned_issue if submission else "",
                    "publication_month": submission.planned_publication_month if submission else "",
                    "publication_year": submission.planned_year if submission else "",
                    "doi_full": submission.doi if submission else "",
                    "first_author": next((author.name for author in sorted(submission.authors, key=lambda item: item.position)), "") if submission else "",
                    "received_date": submission.date_submitted.isoformat() if submission and submission.date_submitted else "",
                    "accepted_date": submission.date_accepted.isoformat() if submission and submission.date_accepted else "",
                }
                for key, label in (("volume", "Volume"), ("issue", "Issue"),
                                   ("publication_month", "Publication month"),
                                   ("publication_year", "Publication year"), ("doi_full", "Full DOI or URL"),
                                   ("doi_suffix", "DOI suffix (fallback)"), ("first_author", "First author"),
                                   ("received_date", "Received date (as printed)"),
                                   ("revised_date", "Revised date (as printed)"),
                                   ("accepted_date", "Accepted date (as printed)")):
                    metadata[key] = st.text_input(label, value=str(defaults.get(key) or ""),
                        key=f"article-metadata-{journal.id}-{key}")
            submitted = st.form_submit_button("Create formatting job", type="primary")
        if submitted:
            if not manuscript:
                st.error("Template-preserving revision requires the original DOCX manuscript.")
            else:
                try:
                    job = create_article_revision_job(session, journal, user, submission=submission, title=title,
                        submission_identifier=identifier, mode=mode, filename=manuscript.name,
                        content=manuscript.getvalue(), metadata=metadata)
                    st.session_state.article_revision_job = str(job.id)
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    st.error(str(exc))
    job = _job_selector(session, journal, user)
    if job is None:
        return
    st.subheader(job.article_title)
    st.caption(f"Status: {job.status} · Template v{job.template.version} · Mode: {job.mode}")
    workflow = st.radio(
        "Formatting workflow",
        ["FAST_AUTO_FORMAT", "GUIDED_REVIEW"],
        index=0,
        horizontal=True,
        format_func=lambda value: {
            "FAST_AUTO_FORMAT": "Fast Auto Format (default)",
            "GUIDED_REVIEW": "Guided Review",
        }[value],
        key=f"article-workflow-{job.id}",
    )
    fast_mode = workflow == "FAST_AUTO_FORMAT"
    if fast_mode:
        st.caption("One click applies deterministic formatting, checks document integrity, and generates the DOCX. Only real exceptions require attention.")
    editable = job.status in {"UPLOADED", "ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT", "INTEGRITY_FAILED"}
    if editable and template.id != job.template_id:
        st.warning(f"This job still uses article template v{job.template.version}; active version is v{template.version}.")
        if st.button("Use active article template and recheck", key=f"article-template-reselect-{job.id}"):
            try:
                select_active_article_template_for_job(session, journal, user, job.id)
                st.rerun()
            except Exception as exc:
                session.rollback()
                st.error(str(exc))
    if editable and journal.abbreviation.upper() == "ELKOLIND":
        with st.expander("Review or correct ELKOLIND metadata"):
            values = json.loads(job.metadata_json or "{}")
            with st.form(f"article-metadata-edit-{job.id}"):
                corrected_title = st.text_input("Manuscript title for footer", value=job.article_title,
                    key=f"article-title-edit-{job.id}")
                st.caption(f"Four-word footer title: {short_title_4w(corrected_title)}")
                revised_values = {}
                for key, label in (("volume", "Volume"), ("issue", "Issue"),
                                   ("publication_month", "Publication month"),
                                   ("publication_year", "Publication year"), ("doi_full", "Full DOI or URL"),
                                   ("doi_suffix", "DOI suffix (fallback)"), ("first_author", "First author"),
                                   ("received_date", "Received date (as printed)"),
                                   ("revised_date", "Revised date (as printed)"),
                                   ("accepted_date", "Accepted date (as printed)")):
                    revised_values[key] = st.text_input(label, value=values.get(key, ""),
                        key=f"article-metadata-edit-{job.id}-{key}")
                save_metadata = st.form_submit_button("Save metadata and rerun audit")
            if save_metadata:
                try:
                    update_article_job_metadata(session, journal, user, job.id,
                        metadata=revised_values, article_title=corrected_title)
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    st.error(str(exc))
    if job.mode != "TEMPLATE_ONLY":
        st.warning("This formatting workflow applies only deterministic template fixes. Language/reviewer text edits remain in the separate Reviewer Revision workflow and require editor approval; no wording is silently changed here.")
    if editable and fast_mode:
        primary, preserve, secondary = st.columns([2, 2, 1])
        auto_generate = primary.button("Auto Format & Generate", type="primary",
            key=f"article-fast-generate-{job.id}")
        preserve_generate = preserve.button("Generate While Preserving Protected Objects",
            key=f"article-fast-preserve-{job.id}",
            help="Protected bookmarks, fields, hyperlinks and content controls are retained unchanged. This is also the default Fast Auto Format behavior.")
        analyze_only = secondary.button("Analyze Only", key=f"article-fast-audit-{job.id}")
        if auto_generate or preserve_generate:
            try:
                auto_format_and_generate(session, journal, user, job.id)
                st.rerun()
            except Exception as exc:
                session.rollback()
                st.error(str(exc))
        if analyze_only:
            try:
                audit_article_revision_job(session, journal, user, job.id)
                st.rerun()
            except Exception as exc:
                session.rollback()
                st.error(str(exc))
    elif job.status in {"UPLOADED", "ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT"} and st.button(
        "Run Template Compliance Check", key=f"article-audit-{job.id}"
    ):
        try:
            audit_article_revision_job(session, journal, user, job.id)
            st.rerun()
        except Exception as exc:
            session.rollback()
            st.error(str(exc))
    raw_findings = job_findings(job)
    findings = job_effective_findings(job)
    if fast_mode and (raw_findings or job_auto_format_result(job)):
        _render_fast_result(session, journal, user, job)
    if raw_findings and not fast_mode:
        st.metric("Template compliance", f"{job.compliance_score}%")
        st.caption("Formatting conformity only; not an editorial or scientific quality score.")
        st.dataframe(pd.DataFrame([{"Category": item.category, "Check": item.check,
            "Expected": item.expected, "Detected": item.detected, "Status": item.status,
            "Location": item.target or "Document"} for item in findings]), use_container_width=True, hide_index=True)
        safe = [item for item in raw_findings if item.status == "SAFE_FIX_AVAILABLE"]
        if job.status in {"ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT"}:
            st.write("### Safe Fixes")
            chosen = {item.id for item in safe if st.checkbox(
                f"{item.check} · {item.target or 'Document'}: {item.detected} → {item.expected}",
                value=True, key=f"article-fix-{job.id}-{item.id}")}
            c1, c2 = st.columns(2)
            apply_all = c1.button("Apply All Safe Fixes", key=f"article-all-{job.id}")
            apply_selected = c2.button("Approve Selected Safe Fixes", key=f"article-selected-{job.id}")
            if apply_all or apply_selected:
                try:
                    approve_article_fixes(session, journal, user, job.id,
                        {item.id for item in safe} if apply_all else chosen)
                    st.success("Formatting decisions recorded. No output has been generated yet.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    st.error(str(exc))
        review_items = [item for item in raw_findings if item.status == "REVIEW_REQUIRED"]
        decisions = job_review_decisions(job)
        if review_items:
            st.write("### Human Review Required")
            st.caption("Resolve each item explicitly. A resolution records editorial judgment; it does not alter scientific content unless Apply Suggested Fix is selected.")
            for item in review_items:
                current = decisions.get(item.id, {})
                with st.expander(
                    f"{item.category} · {item.check} · {current.get('status', 'REVIEW_REQUIRED')}",
                    expanded=not bool(current),
                ):
                    st.write(f"**Category:** {item.category}")
                    st.write(f"**Rule:** {item.check}")
                    st.write(f"**Location:** {item.target or 'Document'}")
                    st.write(f"**Expected:** {item.expected}")
                    st.write(f"**Detected:** {item.detected}")
                    st.write(f"**Suggested correction:** {item.expected}")
                    if current:
                        st.success(f"Recorded decision: {current.get('status')}")
                    c1, c2, c3, c4 = st.columns(4)
                    apply_fix = c1.button("Apply Suggested Fix", key=f"review-apply-{job.id}-{item.id}",
                        disabled=not review_finding_can_apply_fix(item),
                        help=None if review_finding_can_apply_fix(item) else
                        "This finding has no deterministic document patch; complete it manually or keep it as reviewed.")
                    keep = c2.button("Keep As Is", key=f"review-keep-{job.id}-{item.id}")
                    false_positive = c3.button("Mark False Positive", key=f"review-false-{job.id}-{item.id}")
                    completed = c4.button("Manual Review Completed", key=f"review-complete-{job.id}-{item.id}")
                    action = ("APPLY_SUGGESTED_FIX" if apply_fix else "KEEP_AS_IS" if keep else
                              "MARK_FALSE_POSITIVE" if false_positive else
                              "MANUAL_REVIEW_COMPLETED" if completed else None)
                    if action:
                        try:
                            resolve_article_review_findings(session, journal, user, job.id, {item.id: action})
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            st.error(str(exc))
        summary = job_resolution_summary(job, raw_findings)
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Safe fixes", summary["safe_fixes"])
        m2.metric("Human review", summary["human_review"])
        m3.metric("Resolved human review", summary["resolved_human_review"])
        m4.metric("Unresolved human review", summary["unresolved_human_review"])
        m5.metric("Blocking issues", summary["blocking_issues"])
        if summary["unresolved_human_review"]:
            st.warning(f"{summary['unresolved_human_review']} human-review finding(s) remain unresolved.")
        if summary["blocking_issues"]:
            st.error(f"{summary['blocking_issues']} manual-action or missing-section blocker(s) prevent generation.")
        if not summary["safe_fixes_confirmed"]:
            st.info("Confirm the safe-fix selection before generation.")
        if job.status in {"ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT"}:
            generate = st.button("Generate Formatted Manuscript and Compliance Report", type="primary",
                key=f"article-generate-{job.id}", disabled=not summary["can_generate"])
            if generate:
                try:
                    generate_formatted_manuscript(session, journal, user, job.id)
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    st.error(str(exc))
    if job.integrity_json:
        integrity = json.loads(job.integrity_json)
        st.success("Integrity checks passed.") if integrity["passed"] else st.error("Integrity check failed; finalization is blocked.")
        with st.expander("Numerical, figure, table, equation, reference and package integrity"):
            st.json(integrity)
    for artifact in sorted(job.artifacts, key=lambda item: (item.version, item.kind), reverse=True):
        try:
            filename, content = authorized_article_artifact_bytes(session, journal, user, artifact.id)
            label = ("Download Formatted Manuscript" if artifact.kind == "FORMATTED_MANUSCRIPT" else
                     "Download Compliance Report" if artifact.kind == "COMPLIANCE_REPORT" else
                     f"Download {filename}")
            st.download_button(f"{label} · v{artifact.version}", content, file_name=filename,
                mime=XLSX_MIME if filename.endswith(".xlsx") else DOCX_MIME,
                key=f"article-artifact-{artifact.id}")
        except Exception as exc:
            st.error(str(exc))
    if job.status == "FORMATTED" and st.button("Mark formatting job complete after Word inspection", key=f"article-complete-{job.id}"):
        try:
            complete_article_revision_job(session, journal, user, job.id)
            st.rerun()
        except Exception as exc:
            session.rollback()
            st.error(str(exc))


def template_revision_jobs_page(session: Session, journal: Journal, user: User) -> None:
    st.subheader("Template formatting jobs")
    if not _ready(session, journal, user):
        return
    jobs = article_revision_jobs(session, journal, user)
    st.dataframe(pd.DataFrame([{"Submission ID": job.submission_identifier or "Standalone", "Article Title": job.article_title,
        "Journal": journal.abbreviation, "Template version": job.template.version, "Mode": job.mode,
        "Compliance": f"{job.compliance_score}%" if job.compliance_score is not None else "—",
        "Safe fixes": sum(item.status == "SAFE_FIX_AVAILABLE" for item in job_findings(job)),
        "Human review": job_resolution_summary(job)["human_review"],
        "Resolved review": job_resolution_summary(job)["resolved_human_review"],
        "Unresolved review": job_resolution_summary(job)["unresolved_human_review"],
        "Blocking issues": job_resolution_summary(job)["blocking_issues"],
        "Status": job.status, "Created": job.created_at, "Updated": job.updated_at} for job in jobs]),
        use_container_width=True, hide_index=True)
    for job in jobs:
        if st.button(f"Open · {job.article_title[:60]}", key=f"article-job-open-{job.id}"):
            st.session_state.article_revision_job = str(job.id)
            st.session_state.jas_navigation = "REVISION · Quick Template Revision"
            st.rerun()


def template_revision_history_page(session: Session, journal: Journal, user: User) -> None:
    st.subheader("Template formatting history")
    if not _ready(session, journal, user):
        return
    job = _job_selector(session, journal, user)
    if job is None:
        return
    events = article_revision_history(session, journal, user, job.id)
    st.dataframe(pd.DataFrame([{"Time": event.created_at, "Action": event.action,
        "User ID": str(event.user_id) if event.user_id else "System", "Details": event.new_value or ""}
        for event in events]), use_container_width=True, hide_index=True)
    st.write("**Immutable artifact versions**")
    st.dataframe(pd.DataFrame([{"Version": item.version, "Kind": item.kind,
        "SHA-256": item.sha256, "Created": item.created_at} for item in job.artifacts]),
        use_container_width=True, hide_index=True)


def revision_jobs_hub(session: Session, journal: Journal, user: User) -> None:
    template_tab, reviewer_tab = st.tabs(["Template formatting", "Reviewer revision"])
    with template_tab:
        template_revision_jobs_page(session, journal, user)
    with reviewer_tab:
        from revision_page import revision_jobs_page
        revision_jobs_page(session, journal, user)


def revision_history_hub(session: Session, journal: Journal, user: User) -> None:
    template_tab, reviewer_tab = st.tabs(["Template formatting", "Reviewer revision"])
    with template_tab:
        template_revision_history_page(session, journal, user)
    with reviewer_tab:
        from revision_page import revision_history_page
        revision_history_page(session, journal, user)
