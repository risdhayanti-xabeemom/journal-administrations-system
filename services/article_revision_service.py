"""Authenticated, journal-isolated article-template formatting lifecycle."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from models import ArticleRevisionArtifact, ArticleRevisionJob, AuditLog, Journal, Role, Submission, User
from services.article_formatting import (
    ArticleFormattingError, Finding, apply_safe_fixes, audit_article, compliance_report_xlsx,
    compliance_score, formatting_integrity,
)
from services.article_metadata import (
    elkolind_master_warnings, graft_article_master_header_footer, graft_integrity,
    resolve_elkolind_metadata,
)
from services.article_template_service import (
    active_article_template, article_template_config, authorized_article_template_bytes,
)
from services.core import AuthorizationError, assert_journal_access, log_audit
from services.revision_storage import RevisionStorage, get_revision_storage, safe_filename, validate_revision_upload


class ArticleRevisionRuleError(ValueError):
    pass


MODES = ("TEMPLATE_ONLY", "TEMPLATE_LANGUAGE_POLISH", "TEMPLATE_REVIEWER")
EDITABLE_STATUSES = {"UPLOADED", "ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT", "INTEGRITY_FAILED"}
MASTER_HEADER_FOOTER_TARGET = "ARTICLE_MASTER_HEADER_FOOTER"
MASTER_HEADER_FOOTER_FIX_ID = "document:article_master_header_footer:apply"
REVIEW_RESOLUTION_STATUSES = {
    "RESOLVED_APPLY_FIX", "RESOLVED_KEEP", "RESOLVED_FALSE_POSITIVE",
}
REVIEW_ACTION_STATUSES = {
    "APPLY_SUGGESTED_FIX": "RESOLVED_APPLY_FIX",
    "KEEP_AS_IS": "RESOLVED_KEEP",
    "MARK_FALSE_POSITIVE": "RESOLVED_FALSE_POSITIVE",
    "MANUAL_REVIEW_COMPLETED": "RESOLVED_KEEP",
}
BLOCKING_FINDING_STATUSES = {"MANUAL_ACTION_REQUIRED", "MISSING_REQUIRED_SECTION"}
FAST_RESULT_STATUSES = {
    "COMPLIANT", "COMPLIANT_PROTECTED", "AUTO_FIXED", "WARNING", "WARNING_PRESERVED",
    "REVIEW_REQUIRED", "BLOCKING",
}
_PATCHABLE_REVIEW_ATTRIBUTES = {
    "font", "size", "bold", "alignment", "caption_text", "space_before", "space_after",
    "first_line_indent", "line_spacing", "page_width", "page_height", "top_margin",
    "bottom_margin", "left_margin", "right_margin", "orientation", "columns",
}


def _compliance_findings(manuscript: bytes, template: bytes, config: dict[str, object],
                         *, elkolind: bool, master_applied: bool = False) -> list[Finding]:
    """Audit manuscript formatting separately from the authoritative Article Master."""
    rules = config.get("rules", {})
    findings = audit_article(manuscript, config["profile"], rules)
    if not elkolind or rules.get("header_footer_mode") != "MASTER":
        return findings

    # Header/footer presence in an author's manuscript is not a prerequisite. The
    # active master is validated independently and is grafted only after approval.
    findings = [item for item in findings if not (
        item.category == "Document" and item.check in {"Header", "Footer", "Page numbering"}
    )]
    warnings = elkolind_master_warnings(template)
    if warnings:
        findings.append(Finding(
            id="document:article_master_header_footer:validation",
            category="Document",
            check="ELKOLIND Article Master header/footer",
            expected="Valid active master with dynamic metadata placeholders and native PAGE field",
            detected="; ".join(warnings),
            status="MANUAL_ACTION_REQUIRED",
            target=MASTER_HEADER_FOOTER_TARGET,
        ))
        return findings

    findings.extend((
        Finding(
            id="document:article_master_header_footer:validation",
            category="Document",
            check="ELKOLIND Article Master header/footer",
            expected="Valid active master with dynamic metadata placeholders and native PAGE field",
            detected="Validated active Article Master",
            status="COMPLIANT",
            target=MASTER_HEADER_FOOTER_TARGET,
        ),
        Finding(
            id=MASTER_HEADER_FOOTER_FIX_ID,
            category="Document",
            check="Apply active Article Master header/footer",
            expected="Official ELKOLIND header/footer populated from article metadata",
            detected=("Active Article Master applied" if master_applied else
                      "Incoming manuscript page furniture will be replaced during generation"),
            status="COMPLIANT" if master_applied else "SAFE_FIX_AVAILABLE",
            target=MASTER_HEADER_FOOTER_TARGET,
            attribute="header_footer",
            value=True,
        ),
    ))
    return findings


def _validated_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    values = {key: str(value or "").strip() for key, value in (metadata or {}).items()}
    if len(values) > 30 or any(len(key) > 50 or len(value) > 255 for key, value in values.items()):
        raise ArticleRevisionRuleError("Article metadata fields are too large.")
    return values


def article_revision_tables_ready(session: Session) -> bool:
    inspector = inspect(session.bind)
    if not {"article_revision_jobs", "article_revision_artifacts"} <= set(inspector.get_table_names()):
        return False
    return "metadata_json" in {column["name"] for column in inspector.get_columns("article_revision_jobs")}


def _job(session: Session, journal: Journal, user: User, job_id: uuid.UUID) -> ArticleRevisionJob:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    job = session.scalar(select(ArticleRevisionJob).where(ArticleRevisionJob.id == job_id,
        ArticleRevisionJob.journal_id == journal.id))
    if job is None:
        raise AuthorizationError("Template revision job is unavailable for the active journal.")
    return job


def article_revision_jobs(session: Session, journal: Journal, user: User) -> list[ArticleRevisionJob]:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    return list(session.scalars(select(ArticleRevisionJob).where(ArticleRevisionJob.journal_id == journal.id)
                                .order_by(ArticleRevisionJob.updated_at.desc())))


def create_article_revision_job(session: Session, journal: Journal, user: User, *, submission: Submission | None,
                                title: str, submission_identifier: str | None, mode: str,
                                filename: str, content: bytes, metadata: dict[str, str] | None = None,
                                storage: RevisionStorage | None = None) -> ArticleRevisionJob:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    if submission and submission.journal_id != journal.id:
        raise AuthorizationError("Submission belongs to another journal.")
    template = active_article_template(session, journal.id)
    if template is None:
        raise ArticleRevisionRuleError("Upload and activate this journal's Article Template before creating a job.")
    if mode not in MODES or not title.strip():
        raise ArticleRevisionRuleError("Choose a valid mode and article title.")
    validate_revision_upload(filename, content, manuscript=True)
    from services.revision_analysis import analyze_manuscript
    analyze_manuscript(content)
    store = storage or get_revision_storage()
    article_metadata = _validated_metadata(metadata)
    if submission:
        defaults = {
            "volume": submission.planned_volume, "issue": submission.planned_issue,
            "publication_month": submission.planned_publication_month,
            "publication_year": submission.planned_year, "doi_full": submission.doi,
            "first_author": next((author.name for author in sorted(submission.authors, key=lambda item: item.position)), None),
            "received_date": submission.date_submitted.isoformat() if submission.date_submitted else None,
            "accepted_date": submission.date_accepted.isoformat() if submission.date_accepted else None,
        }
        for key, value in defaults.items():
            article_metadata.setdefault(key, str(value or "").strip())
    job = ArticleRevisionJob(journal_id=journal.id, submission_id=submission.id if submission else None,
        template_id=template.id, article_title=title.strip(), submission_identifier=submission_identifier or None,
        mode=mode, status="UPLOADED", original_filename=safe_filename(filename),
        original_storage_key="pending", original_sha256=hashlib.sha256(content).hexdigest(), created_by=user.id)
    job.metadata_json = json.dumps(article_metadata, ensure_ascii=False)
    session.add(job)
    session.flush()
    key, digest = store.put(job.id, "article_original", content, ".docx")
    if digest != job.original_sha256:
        raise ArticleRevisionRuleError("Private manuscript storage checksum mismatch.")
    job.original_storage_key = key
    log_audit(session, action="ARTICLE_REVISION_JOB_CREATED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id, new={"submission_id": str(job.submission_id) if job.submission_id else None,
            "template_id": str(template.id), "template_version": template.version, "mode": mode,
            "metadata_fields": sorted(key for key, value in article_metadata.items() if value)})
    log_audit(session, action="ARTICLE_MANUSCRIPT_UPLOADED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id, new={"filename": job.original_filename, "sha256": digest})
    session.commit()
    return job


def update_article_job_metadata(session: Session, journal: Journal, user: User, job_id: uuid.UUID,
                                *, metadata: dict[str, str], article_title: str) -> ArticleRevisionJob:
    job = _job(session, journal, user, job_id)
    if job.status not in EDITABLE_STATUSES or not article_title.strip():
        raise ArticleRevisionRuleError("Metadata cannot be edited after artifact generation; provide a valid title.")
    values = _validated_metadata(metadata)
    prior = json.loads(job.metadata_json or "{}")
    previous_title = job.article_title
    job.metadata_json = json.dumps(values, ensure_ascii=False)
    job.article_title = article_title.strip()
    job.findings_json = job.approved_fixes_json = job.integrity_json = None
    job.compliance_score = None
    job.status = "UPLOADED"
    log_audit(session, action="ARTICLE_METADATA_UPDATED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id,
        previous={"title": previous_title, "populated_fields": sorted(key for key, value in prior.items() if value)},
        new={"title": job.article_title, "populated_fields": sorted(key for key, value in values.items() if value)})
    session.commit()
    return job


def select_active_article_template_for_job(session: Session, journal: Journal, user: User,
                                           job_id: uuid.UUID) -> ArticleRevisionJob:
    job = _job(session, journal, user, job_id)
    if job.status not in EDITABLE_STATUSES:
        raise ArticleRevisionRuleError("An issued artifact cannot silently change article template version.")
    template = active_article_template(session, journal.id)
    if template is None:
        raise ArticleRevisionRuleError("No active article master is configured for this journal.")
    if template.id == job.template_id:
        return job
    before = {"template_id": str(job.template_id), "version": job.template.version}
    job.template = template
    job.findings_json = job.approved_fixes_json = job.integrity_json = None
    job.compliance_score = None
    job.status = "UPLOADED"
    log_audit(session, action="ARTICLE_TEMPLATE_SELECTED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id, previous=before,
        new={"template_id": str(template.id), "version": template.version})
    session.commit()
    return job


def _read_original(job: ArticleRevisionJob, store: RevisionStorage) -> bytes:
    original = store.read(job.original_storage_key)
    if hashlib.sha256(original).hexdigest() != job.original_sha256:
        raise ArticleRevisionRuleError("Original manuscript hash changed; processing stopped.")
    return original


def audit_article_revision_job(session: Session, journal: Journal, user: User, job_id: uuid.UUID,
                               *, storage: RevisionStorage | None = None) -> list[Finding]:
    job = _job(session, journal, user, job_id)
    if job.status not in {"UPLOADED", "ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT"}:
        raise ArticleRevisionRuleError("This job cannot be re-analyzed after formatting.")
    store = storage or get_revision_storage()
    original = _read_original(job, store)
    _, template_bytes = authorized_article_template_bytes(session, journal, user, job.template_id, storage=store)
    config = article_template_config(job.template)
    findings = _compliance_findings(original, template_bytes, config,
        elkolind=journal.abbreviation.upper() == "ELKOLIND")
    job.findings_json = json.dumps([asdict(item) for item in findings], ensure_ascii=False)
    job.approved_fixes_json = None
    job.compliance_score = compliance_score(findings)
    job.status = "ANALYZED"
    log_audit(session, action="ARTICLE_COMPLIANCE_ANALYZED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id, new={"template_version": job.template.version,
            "findings": len(findings), "safe_fixes": sum(item.status == "SAFE_FIX_AVAILABLE" for item in findings),
            "compliance_score": job.compliance_score})
    session.commit()
    return findings


def job_findings(job: ArticleRevisionJob) -> list[Finding]:
    return [Finding(**item) for item in json.loads(job.findings_json or "[]")]


def _decision_state(job: ArticleRevisionJob) -> dict[str, object]:
    raw = json.loads(job.approved_fixes_json or "null")
    if isinstance(raw, list):
        # Backward compatibility for jobs created before human-review decisions.
        return {"safe_fix_ids": raw, "safe_fixes_confirmed": True, "review_decisions": {},
                "fast_result": None}
    if not isinstance(raw, dict):
        return {"safe_fix_ids": [], "safe_fixes_confirmed": False, "review_decisions": {},
                "fast_result": None}
    safe_ids = raw.get("safe_fix_ids", [])
    decisions = raw.get("review_decisions", {})
    return {
        "safe_fix_ids": [str(value) for value in safe_ids] if isinstance(safe_ids, list) else [],
        "safe_fixes_confirmed": bool(raw.get("safe_fixes_confirmed")),
        "review_decisions": decisions if isinstance(decisions, dict) else {},
        "fast_result": raw.get("fast_result") if isinstance(raw.get("fast_result"), dict) else None,
    }


def _store_decision_state(job: ArticleRevisionJob, state: dict[str, object]) -> None:
    job.approved_fixes_json = json.dumps(state, ensure_ascii=False, sort_keys=True)


def job_review_decisions(job: ArticleRevisionJob) -> dict[str, dict[str, str]]:
    decisions = _decision_state(job)["review_decisions"]
    return {str(key): dict(value) for key, value in decisions.items() if isinstance(value, dict)}


def job_effective_findings(job: ArticleRevisionJob) -> list[Finding]:
    decisions = job_review_decisions(job)
    result = []
    for finding in job_findings(job):
        decision = decisions.get(finding.id)
        status = decision.get("status") if decision else None
        result.append(replace(finding, status=status) if status in REVIEW_RESOLUTION_STATUSES else finding)
    return result


def finding_blocker_code(finding: Finding) -> str | None:
    """Return a true generation blocker; formatting and editorial warnings return None."""
    if finding.status not in BLOCKING_FINDING_STATUSES:
        return None
    category = finding.category.casefold()
    check = finding.check.casefold()
    target = (finding.target or "").casefold()
    if "article master header/footer" in check or "margin" in check or "article_master" in target:
        return "DOCUMENT_CORRUPTION"
    if category == "references" and "entries" in check:
        return "MISSING_REQUIRED_SCIENTIFIC_CONTENT"
    if category == "abstract" and "present" in check:
        return "MISSING_REQUIRED_SCIENTIFIC_CONTENT"
    if category == "structure" and "required section" in check:
        return "MISSING_REQUIRED_SCIENTIFIC_CONTENT"
    return None


def _fast_status_findings(findings: list[Finding], approved_ids: set[str]) -> list[Finding]:
    result = []
    for finding in findings:
        blocker = finding_blocker_code(finding)
        if blocker:
            result.append(replace(finding, status="BLOCKING", detected=f"{finding.detected} [{blocker}]"))
        elif finding.id in approved_ids and finding.status in {"COMPLIANT", "SAFE_FIX_AVAILABLE"}:
            result.append(replace(finding, status="AUTO_FIXED"))
        elif finding.status == "REVIEW_REQUIRED" and _fast_warning_only(finding):
            result.append(replace(finding, status="WARNING"))
        elif finding.status in {"MISSING_REQUIRED_SECTION", "MANUAL_ACTION_REQUIRED", "SAFE_FIX_AVAILABLE"}:
            result.append(replace(finding, status="WARNING"))
        else:
            result.append(finding)
    return result


def _fast_warning_only(finding: Finding) -> bool:
    """Downgrade advisory editorial checks, while preserving genuine ambiguity."""
    category = finding.category.casefold()
    check = finding.check.casefold()
    if "word count" in check or "keyword count" in check:
        return True
    if category == "references" and any(token in check for token in ("format", "style", "font")):
        return True
    if any(token in check for token in ("optional section", "object alignment")):
        return True
    return False


def job_fast_findings(job: ArticleRevisionJob) -> list[Finding]:
    state = _decision_state(job)
    return _fast_status_findings(job_effective_findings(job), set(state["safe_fix_ids"]))


def job_auto_format_result(job: ArticleRevisionJob) -> dict[str, object] | None:
    result = _decision_state(job).get("fast_result")
    return dict(result) if isinstance(result, dict) else None


def _integrity_blocking_codes(integrity: dict[str, object]) -> list[str]:
    checks = integrity.get("checks", {}) if isinstance(integrity, dict) else {}
    mapping = {
        "numbers_unchanged": "NUMERIC_INTEGRITY_FAILURE",
        "equations_unchanged": "EQUATION_LOSS",
        "figures_unchanged": "FIGURE_LOSS",
        "tables_unchanged": "TABLE_LOSS",
        "references_unchanged": "REFERENCE_LOSS",
        "text_unchanged": "SCIENTIFIC_DATA_CHANGE",
        "caption_notation_changes_authorized": "SCIENTIFIC_DATA_CHANGE",
        "headers_footers_and_other_parts_unchanged": "DOCUMENT_CORRUPTION",
        "section_break_count_unchanged": "DOCUMENT_CORRUPTION",
    }
    return sorted({code for key, code in mapping.items() if checks.get(key) is False})


def review_finding_can_apply_fix(finding: Finding) -> bool:
    return bool(
        finding.status == "REVIEW_REQUIRED"
        and finding.target
        and finding.target != "section"
        and finding.attribute in _PATCHABLE_REVIEW_ATTRIBUTES
        and finding.value is not None
    )


def job_resolution_summary(job: ArticleRevisionJob, findings: list[Finding] | None = None) -> dict[str, object]:
    records = findings if findings is not None else job_findings(job)
    state = _decision_state(job)
    decisions = state["review_decisions"]
    review_ids = {item.id for item in records if item.status == "REVIEW_REQUIRED"}
    resolved_ids = {
        finding_id for finding_id in review_ids
        if isinstance(decisions.get(finding_id), dict)
        and decisions[finding_id].get("status") in REVIEW_RESOLUTION_STATUSES
    }
    blockers = [item for item in records if item.status in BLOCKING_FINDING_STATUSES]
    unresolved = review_ids - resolved_ids
    safe_count = sum(item.status == "SAFE_FIX_AVAILABLE" for item in records)
    can_generate = bool(state["safe_fixes_confirmed"]) and not unresolved and not blockers
    return {
        "safe_fixes": safe_count,
        "safe_fixes_confirmed": bool(state["safe_fixes_confirmed"]),
        "human_review": len(review_ids),
        "resolved_human_review": len(resolved_ids),
        "unresolved_human_review": len(unresolved),
        "blocking_issues": len(blockers),
        "can_generate": can_generate,
    }


def accept_all_low_risk_review_findings(session: Session, journal: Journal, user: User,
                                        job_id: uuid.UUID) -> ArticleRevisionJob:
    job = _job(session, journal, user, job_id)
    low_risk = {item.id: "APPLY_SUGGESTED_FIX" for item in job_findings(job)
                if item.status == "REVIEW_REQUIRED" and review_finding_can_apply_fix(item)}
    if not low_risk:
        raise ArticleRevisionRuleError("No low-risk formatting suggestions are available for batch acceptance.")
    return resolve_article_review_findings(session, journal, user, job_id, low_risk)


def _update_job_readiness(job: ArticleRevisionJob) -> None:
    summary = job_resolution_summary(job)
    if summary["blocking_issues"] or summary["unresolved_human_review"]:
        job.status = "REVIEW_REQUIRED"
    elif summary["safe_fixes_confirmed"]:
        job.status = "READY_TO_FORMAT"
    else:
        job.status = "ANALYZED"


def resolve_article_review_findings(session: Session, journal: Journal, user: User, job_id: uuid.UUID,
                                    decisions: dict[str, str]) -> ArticleRevisionJob:
    job = _job(session, journal, user, job_id)
    if job.status not in {"ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT"}:
        raise ArticleRevisionRuleError("Run the compliance audit before resolving human-review findings.")
    review_findings = {item.id: item for item in job_findings(job) if item.status == "REVIEW_REQUIRED"}
    if not decisions or not set(decisions) <= set(review_findings):
        raise ArticleRevisionRuleError("Only current REVIEW_REQUIRED findings may be resolved.")
    state = _decision_state(job)
    stored = dict(state["review_decisions"])
    audit_decisions = []
    for finding_id, action in decisions.items():
        status = REVIEW_ACTION_STATUSES.get(action, action)
        if status not in REVIEW_RESOLUTION_STATUSES:
            raise ArticleRevisionRuleError("Choose a valid human-review resolution.")
        finding = review_findings[finding_id]
        if status == "RESOLVED_APPLY_FIX" and not review_finding_can_apply_fix(finding):
            raise ArticleRevisionRuleError(
                f"{finding.check} has no deterministic document patch. Choose Keep As Is, False Positive, "
                "or Manual Review Completed."
            )
        stored[finding_id] = {
            "status": status,
            "action": action,
            "decided_by": str(user.id),
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        audit_decisions.append({"finding_id": finding_id, "status": status, "action": action})
    state["review_decisions"] = stored
    _store_decision_state(job, state)
    _update_job_readiness(job)
    log_audit(session, action="ARTICLE_HUMAN_REVIEW_RESOLVED", object_type="article_revision_job",
        object_id=job.id, user_id=user.id, journal_id=journal.id,
        new={"decisions": audit_decisions, "template_version": job.template.version})
    session.commit()
    return job


def approve_article_fixes(session: Session, journal: Journal, user: User, job_id: uuid.UUID,
                          approved_ids: set[str]) -> ArticleRevisionJob:
    job = _job(session, journal, user, job_id)
    if job.status not in {"ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT"}:
        raise ArticleRevisionRuleError("Run the compliance audit before approving safe fixes.")
    safe = {item.id for item in job_findings(job) if item.status == "SAFE_FIX_AVAILABLE"}
    if not approved_ids <= safe:
        raise ArticleRevisionRuleError("Only confirmed safe formatting fixes may be approved.")
    state = _decision_state(job)
    state["safe_fix_ids"] = sorted(approved_ids)
    state["safe_fixes_confirmed"] = True
    _store_decision_state(job, state)
    _update_job_readiness(job)
    log_audit(session, action="ARTICLE_FIXES_APPROVED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id, new={"fix_ids": sorted(approved_ids),
            "template_version": job.template.version})
    session.commit()
    return job


def generate_formatted_manuscript(session: Session, journal: Journal, user: User, job_id: uuid.UUID,
                                  *, storage: RevisionStorage | None = None,
                                  fast_mode: bool = False) -> list[ArticleRevisionArtifact]:
    job = _job(session, journal, user, job_id)
    allowed_statuses = {"ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT"} if fast_mode else {"READY_TO_FORMAT"}
    if job.status not in allowed_statuses or job.approved_fixes_json is None:
        raise ArticleRevisionRuleError("Resolve human review and approve safe fixes before generation.")
    store = storage or get_revision_storage()
    original = _read_original(job, store)
    _, template_bytes = authorized_article_template_bytes(session, journal, user, job.template_id, storage=store)
    config = article_template_config(job.template)
    elkolind = journal.abbreviation.upper() == "ELKOLIND"
    current = _compliance_findings(original, template_bytes, config, elkolind=elkolind)
    saved = job_findings(job)
    if [asdict(item) for item in current] != [asdict(item) for item in saved]:
        raise ArticleRevisionRuleError("Template rules or manuscript changed since audit. Run the compliance check again.")
    state = _decision_state(job)
    summary = job_resolution_summary(job, saved)
    if not state["safe_fixes_confirmed"]:
        raise ArticleRevisionRuleError("Confirm the safe-fix selection before generation.")
    if summary["unresolved_human_review"] and not fast_mode:
        raise ArticleRevisionRuleError("Resolve every REVIEW_REQUIRED finding before generation.")
    true_blockers = [(item, finding_blocker_code(item)) for item in saved]
    true_blockers = [(item, code) for item, code in true_blockers if code]
    if true_blockers:
        raise ArticleRevisionRuleError(
            "Auto Format stopped for true blocking issue(s): " +
            ", ".join(sorted({code for _, code in true_blockers}))
        )
    if summary["blocking_issues"] and not fast_mode:
        raise ArticleRevisionRuleError("Resolve all manual-action and missing-section blockers before generation.")
    approved = set(state["safe_fix_ids"])
    review_apply_ids = {
        finding_id for finding_id, decision in state["review_decisions"].items()
        if isinstance(decision, dict) and decision.get("status") == "RESOLVED_APPLY_FIX"
    }
    master_mode = config.get("rules", {}).get("header_footer_mode") == "MASTER"
    if elkolind and master_mode:
        invalid_master = next((item for item in current
            if item.id == "document:article_master_header_footer:validation" and
            item.status == "MANUAL_ACTION_REQUIRED"), None)
        if invalid_master is not None:
            raise ArticleRevisionRuleError(
                "Active ELKOLIND Article Master failed validation: " + invalid_master.detected
            )
        if MASTER_HEADER_FOOTER_FIX_ID not in approved:
            raise ArticleRevisionRuleError(
                "Approve the safe Article Master header/footer fix before generation."
            )
    applicable = [replace(item, status="SAFE_FIX_AVAILABLE")
                  if item.id in review_apply_ids else item for item in current]
    body_approved = {item.id for item in applicable
                     if item.id in approved | review_apply_ids and item.target != MASTER_HEADER_FOOTER_TARGET}
    formatted_body = apply_safe_fixes(original, applicable, body_approved)
    integrity = formatting_integrity(original, formatted_body)
    formatted = formatted_body
    if integrity["passed"] and config.get("rules", {}).get("header_footer_mode") == "MASTER":
        rules = config["rules"]
        if rules.get("require_lr_margin_confirmation") and (
            rules.get("left_margin_mm") is None or rules.get("right_margin_mm") is None
        ):
            raise ArticleRevisionRuleError(
                "Administrator must confirm both ELKOLIND left/right margins in Article Template Rules before generation."
            )
        raw_metadata = json.loads(job.metadata_json or "{}")
        values = resolve_elkolind_metadata(raw_metadata, job.article_title) if elkolind else raw_metadata
        formatted, master_details = graft_article_master_header_footer(
            formatted_body, template_bytes, values, elkolind=elkolind
        )
        master_check = graft_integrity(formatted_body, formatted, values, elkolind=elkolind)
        integrity["checks"].update(master_check["checks"])
        integrity["master_header_footer"] = master_details
        integrity["passed"] = integrity["passed"] and master_check["passed"]
    log_audit(session, action="ARTICLE_FORMATTING_APPLIED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id, new={"approved_fix_count": len(approved), "template_version": job.template.version})
    log_audit(session, action="ARTICLE_INTEGRITY_CHECKED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id, new={"passed": integrity["passed"], "checks": integrity["checks"]})
    integrity["blocking_codes"] = _integrity_blocking_codes(integrity)
    job.integrity_json = json.dumps(integrity, ensure_ascii=False)
    if not integrity["passed"]:
        job.status = "INTEGRITY_FAILED"
        session.commit()
        raise ArticleFormattingError("Integrity check failed. No final manuscript or report was stored.")
    post_findings = _compliance_findings(
        formatted, template_bytes, config, elkolind=elkolind,
        master_applied=bool(master_mode),
    )
    job.findings_json = json.dumps([asdict(item) for item in post_findings], ensure_ascii=False)
    job.compliance_score = compliance_score(post_findings)
    report_findings = job_effective_findings(job)
    if fast_mode:
        report_findings = _fast_status_findings(report_findings, approved | review_apply_ids)
    report = compliance_report_xlsx(journal=journal.abbreviation, manuscript_name=job.original_filename,
        template_version=job.template.version, findings=report_findings,
        approved_ids=approved | review_apply_ids, integrity=integrity,
        review_decisions=job_review_decisions(job))
    version = (session.scalar(select(func.max(ArticleRevisionArtifact.version)).where(ArticleRevisionArtifact.job_id == job.id)) or 0) + 1
    artifacts = []
    for kind, filename, content, suffix in (
        ("FORMATTED_MANUSCRIPT", "Formatted_Manuscript.docx", formatted, ".docx"),
        ("COMPLIANCE_REPORT", "Template_Compliance_Report.xlsx", report, ".xlsx"),
    ):
        key, digest = store.put(job.id, kind.lower(), content, suffix)
        artifact = ArticleRevisionArtifact(job_id=job.id, kind=kind, version=version,
            filename=filename, storage_key=key, sha256=digest, created_by=user.id)
        session.add(artifact)
        artifacts.append(artifact)
    job.status = "FORMATTED"
    log_audit(session, action="ARTICLE_REVISION_ARTIFACTS_GENERATED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id, new={"version": version, "template_version": job.template.version,
            "artifact_count": len(artifacts), "integrity_passed": True})
    session.commit()
    return artifacts


def auto_format_and_generate(session: Session, journal: Journal, user: User, job_id: uuid.UUID,
                             *, storage: RevisionStorage | None = None) -> list[ArticleRevisionArtifact]:
    """One-click deterministic formatting with scientific/integrity blockers only."""
    job = _job(session, journal, user, job_id)
    if job.status not in EDITABLE_STATUSES:
        raise ArticleRevisionRuleError("This job can no longer be auto-formatted.")
    prior_decisions = job_review_decisions(job)
    findings = audit_article_revision_job(session, journal, user, job_id, storage=storage)
    job = _job(session, journal, user, job_id)
    current_review_ids = {item.id for item in findings if item.status == "REVIEW_REQUIRED"}
    retained_decisions = {
        finding_id: decision for finding_id, decision in prior_decisions.items()
        if finding_id in current_review_ids and decision.get("status") in REVIEW_RESOLUTION_STATUSES
    }
    if retained_decisions:
        state = _decision_state(job)
        state["review_decisions"] = retained_decisions
        _store_decision_state(job, state)
        session.commit()
    safe_ids = {item.id for item in findings if item.status == "SAFE_FIX_AVAILABLE"}
    approve_article_fixes(session, journal, user, job_id, safe_ids)
    job = _job(session, journal, user, job_id)
    blockers = [finding_blocker_code(item) for item in findings]
    blockers = sorted({code for code in blockers if code})
    state = _decision_state(job)
    review_apply_ids = {
        finding_id for finding_id, decision in state["review_decisions"].items()
        if isinstance(decision, dict) and decision.get("status") == "RESOLVED_APPLY_FIX"
    }
    fast_findings = _fast_status_findings(findings, safe_ids | review_apply_ids)
    warning_count = sum(item.status in {"WARNING", "WARNING_PRESERVED", "REVIEW_REQUIRED"}
                        for item in fast_findings)
    protected_targets = {
        item.target for item in fast_findings
        if item.status in {"COMPLIANT_PROTECTED", "WARNING_PRESERVED"}
        and item.protected_object_type and item.target
    }
    state["fast_result"] = {
        "auto_fixed": len(safe_ids | review_apply_ids),
        "already_compliant": sum(item.status == "COMPLIANT" for item in findings),
        "protected_preserved": len(protected_targets),
        "warnings": warning_count,
        "blocking_issues": len(blockers),
        "blocking_codes": blockers,
    }
    _store_decision_state(job, state)
    log_audit(session, action="ARTICLE_FAST_AUTO_FORMAT_STARTED", object_type="article_revision_job",
        object_id=job.id, user_id=user.id, journal_id=journal.id,
        new={"auto_fix_count": len(safe_ids), "warning_count": warning_count,
             "blocking_codes": blockers, "template_version": job.template.version})
    session.commit()
    if blockers:
        raise ArticleRevisionRuleError(
            "Auto Format stopped for true blocking issue(s): " + ", ".join(blockers)
        )
    try:
        artifacts = generate_formatted_manuscript(
            session, journal, user, job_id, storage=storage, fast_mode=True
        )
    except ArticleFormattingError:
        job = _job(session, journal, user, job_id)
        integrity = json.loads(job.integrity_json or "{}")
        state = _decision_state(job)
        result = dict(state.get("fast_result") or {})
        codes = integrity.get("blocking_codes") or ["DOCUMENT_CORRUPTION"]
        result["blocking_issues"] = len(codes)
        result["blocking_codes"] = codes
        state["fast_result"] = result
        _store_decision_state(job, state)
        session.commit()
        raise
    job = _job(session, journal, user, job_id)
    result = job_auto_format_result(job) or {}
    log_audit(session, action="ARTICLE_FAST_AUTO_FORMAT_COMPLETED", object_type="article_revision_job",
        object_id=job.id, user_id=user.id, journal_id=journal.id,
        new={**result, "artifact_count": len(artifacts), "template_version": job.template.version})
    session.commit()
    return artifacts


def complete_article_revision_job(session: Session, journal: Journal, user: User, job_id: uuid.UUID) -> ArticleRevisionJob:
    job = _job(session, journal, user, job_id)
    if job.status != "FORMATTED" or not json.loads(job.integrity_json or "{}").get("passed"):
        raise ArticleRevisionRuleError("Generate and inspect a manuscript with passing integrity checks first.")
    job.status = "COMPLETED"
    log_audit(session, action="ARTICLE_REVISION_COMPLETED", object_type="article_revision_job", object_id=job.id,
        user_id=user.id, journal_id=journal.id, new={"template_version": job.template.version})
    session.commit()
    return job


def authorized_article_artifact_bytes(session: Session, journal: Journal, user: User, artifact_id: uuid.UUID,
                                      *, storage: RevisionStorage | None = None) -> tuple[str, bytes]:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    artifact = session.scalar(select(ArticleRevisionArtifact).join(ArticleRevisionJob).where(
        ArticleRevisionArtifact.id == artifact_id, ArticleRevisionJob.journal_id == journal.id))
    if artifact is None:
        raise AuthorizationError("Article revision artifact is unavailable for the active journal.")
    content = (storage or get_revision_storage()).read(artifact.storage_key)
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise ArticleRevisionRuleError("Artifact checksum mismatch; download stopped.")
    return artifact.filename, content


def article_revision_history(session: Session, journal: Journal, user: User, job_id: uuid.UUID) -> list[AuditLog]:
    _job(session, journal, user, job_id)
    return list(session.scalars(select(AuditLog).where(AuditLog.journal_id == journal.id,
        AuditLog.object_type == "article_revision_job", AuditLog.object_id == str(job_id)).order_by(AuditLog.created_at)))
