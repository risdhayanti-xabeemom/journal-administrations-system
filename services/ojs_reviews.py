"""Preview-first import of the OJS review report: reviewer names and review outcome per submission.

The review report is a second OJS export (one row per reviewer assignment) that is
matched to submissions already imported from the articles report by OJS Submission ID.
Only the reviewer's name, OJS username, round, state, recommendation, and two dates are
kept. Reviewer e-mail addresses and review comments in the report are deliberately
ignored.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Journal, Role, Submission, SubmissionReviewer, User
from services.core import assert_journal_access, log_audit
from services.ojs_import import ImportTable, clean_text, detect_columns
from services.ojs_status import (
    TITLE_OVERLAP_MINIMUM,
    parse_date,
    review_state,
    reviewer_display_name,
    title_overlap,
)

REVIEW_ALIASES: dict[str, tuple[str, ...]] = {
    "ojs_submission_id": ("submission_id", "ojs_submission_id"),
    "submission_title": ("submission_title", "title"),
    "reviewer_username": ("reviewer", "reviewer_username", "username"),
    "given_name": ("given_name", "first_name"),
    "family_name": ("family_name", "last_name", "surname"),
    "reviewer_name": ("reviewer_name", "full_name"),
    "review_round": ("round", "review_round"),
    "date_assigned": ("date_assigned",),
    "date_confirmed": ("date_confirmed",),
    "date_completed": ("date_completed",),
    "declined": ("declined",),
    "cancelled": ("cancelled", "canceled"),
    "recommendation": ("recommendation",),
}


def detect_review_columns(headers: tuple[str, ...]) -> tuple[dict[str, int | None], dict[str, str]]:
    return detect_columns(headers, REVIEW_ALIASES)


def missing_review_columns(mapping: dict[str, int | None]) -> list[str]:
    """Human-readable names of required columns that were not found."""
    missing = []
    if mapping.get("ojs_submission_id") is None:
        missing.append("Submission ID")
    if all(mapping.get(name) is None for name in ("reviewer_username", "given_name", "family_name", "reviewer_name")):
        missing.append("Reviewer (username or given/family name)")
    return missing


@dataclass
class ReviewPreviewRow:
    source_row: int
    values: dict[str, str]
    status: str
    action: str
    issues: list[str] = field(default_factory=list)
    submission_id: uuid.UUID | None = None
    existing_id: uuid.UUID | None = None
    reviewer_name: str = ""
    ojs_reviewer: str = ""
    review_round: int = 1
    review_state: str = "PENDING"
    recommendation: str | None = None
    date_assigned: date | None = None
    date_completed: date | None = None


@dataclass
class ReviewImportResult:
    total_rows: int
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    not_found: int = 0
    title_mismatch: int = 0
    duplicates: int = 0
    invalid: int = 0
    errors: list[dict[str, str | int]] = field(default_factory=list)


def _round_number(text: str, issues: list[str]) -> int:
    if not text:
        return 1
    try:
        number = int(float(text))
        if number < 1 or float(text) != number:
            raise ValueError
        return number
    except ValueError:
        issues.append(f"Unrecognized review round '{text}'; using round 1.")
        return 1


def build_review_preview(
    session: Session,
    journal: Journal,
    table: ImportTable,
    mapping: dict[str, int | None],
    *,
    check_titles: bool = True,
) -> list[ReviewPreviewRow]:
    missing = missing_review_columns(mapping)
    if missing:
        raise ValueError("The review report is missing required column(s): " + ", ".join(missing) + ".")
    if any(index is not None and (index < 0 or index >= len(table.headers)) for index in mapping.values()):
        raise ValueError("A mapped source column is outside the uploaded file.")
    submissions = {
        ojs_id: (submission_id, title)
        for ojs_id, submission_id, title in session.execute(
            select(Submission.ojs_submission_id, Submission.id, Submission.manuscript_title)
            .where(Submission.journal_id == journal.id, Submission.ojs_submission_id.is_not(None))
        ).all()
    }
    existing = {
        (item.submission_id, item.review_round, item.ojs_reviewer): item
        for item in session.scalars(
            select(SubmissionReviewer)
            .join(Submission, Submission.id == SubmissionReviewer.submission_id)
            .where(Submission.journal_id == journal.id)
        )
    }
    seen: set[tuple[uuid.UUID, int, str]] = set()
    output: list[ReviewPreviewRow] = []
    for source in table.rows:
        values = {
            name: clean_text(source.values[index]) if index is not None and index < len(source.values) else ""
            for name, index in mapping.items()
        }
        values = {name: values.get(name, "") for name in REVIEW_ALIASES}
        issues: list[str] = []
        status, action = "READY", "ADD"
        username = values["reviewer_username"]
        name = reviewer_display_name(values["given_name"], values["family_name"], values["reviewer_name"], username)
        ojs_reviewer = username or name
        ojs_id = values["ojs_submission_id"]
        round_number = _round_number(values["review_round"], issues)
        recommendation = values["recommendation"] or None
        assigned = parse_date(values["date_assigned"])
        completed = parse_date(values["date_completed"])
        state = review_state(values["declined"], values["cancelled"], values["date_confirmed"], values["date_completed"])
        submission_id = None
        existing_id = None
        if source.parse_error:
            status, action = "INVALID_ROW", "SKIP"
            issues.append(source.parse_error)
        elif not ojs_id or not ojs_reviewer:
            status, action = "MISSING_REQUIRED_FIELD", "SKIP"
            issues.append("Missing Submission ID or reviewer.")
        elif len(ojs_id) > 128 or len(ojs_reviewer) > 128 or len(name) > 255 or (recommendation and len(recommendation) > 64):
            status, action = "INVALID_ROW", "SKIP"
            issues.append("Submission ID, reviewer, name, or recommendation exceeds the database field length.")
        else:
            found = submissions.get(ojs_id)
            if found is None:
                status, action = "SUBMISSION_NOT_FOUND", "SKIP"
                issues.append("No submission with this OJS ID in the active journal. Import the articles report first.")
            elif check_titles and values["submission_title"] and title_overlap(values["submission_title"], found[1]) < TITLE_OVERLAP_MINIMUM:
                status, action = "TITLE_MISMATCH", "SKIP"
                issues.append("Title differs from the JAS submission with this OJS ID; the report may belong to another journal.")
            else:
                submission_id = found[0]
                key = (submission_id, round_number, ojs_reviewer)
                if key in seen:
                    status, action = "DUPLICATE", "SKIP"
                    issues.append("Repeated reviewer assignment (same submission, round, and reviewer) within the uploaded file.")
                else:
                    seen.add(key)
                    current = existing.get(key)
                    if current is not None:
                        existing_id = current.id
                        same = (
                            current.reviewer_name == name and current.review_state == state
                            and (current.recommendation or None) == recommendation
                            and current.date_assigned == assigned and current.date_completed == completed
                        )
                        action = "UNCHANGED" if same else "UPDATE"
        if values["date_assigned"] and assigned is None:
            issues.append(f"Unrecognized date_assigned '{values['date_assigned']}'.")
        if values["date_completed"] and completed is None:
            issues.append(f"Unrecognized date_completed '{values['date_completed']}'.")
        if action != "SKIP" and not (values["given_name"] or values["family_name"] or values["reviewer_name"]):
            issues.append("No display name in the report; the OJS username is used as the name.")
        output.append(ReviewPreviewRow(
            source.number, values, status, action, issues, submission_id, existing_id, name, ojs_reviewer,
            round_number, state, recommendation, assigned, completed,
        ))
    return output


def review_preview_table(rows: list[ReviewPreviewRow]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Source row": row.source_row,
            "OJS Submission ID": row.values["ojs_submission_id"],
            "Reviewer": row.reviewer_name,
            "Round": row.review_round,
            "State": row.review_state,
            "Recommendation": row.recommendation or "",
            "Status": row.status,
            "Action": row.action,
            "Review notes": "; ".join(row.issues),
        }
        for row in rows
    ])


def confirm_review_import(
    session: Session,
    journal: Journal,
    user: User,
    table: ImportTable,
    mapping: dict[str, int | None],
    filename: str,
    *,
    check_titles: bool = True,
) -> ReviewImportResult:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    rows = build_review_preview(session, journal, table, mapping, check_titles=check_titles)
    result = ReviewImportResult(total_rows=len(rows))
    for row in rows:
        report = {"source_row": row.source_row, "ojs_submission_id": row.values["ojs_submission_id"], "status": row.status}
        if row.action == "UNCHANGED":
            result.unchanged += 1
            continue
        if row.action == "SKIP":
            if row.status == "SUBMISSION_NOT_FOUND":
                result.not_found += 1
            elif row.status == "TITLE_MISMATCH":
                result.title_mismatch += 1
            elif row.status == "DUPLICATE":
                result.duplicates += 1
            else:
                result.invalid += 1
            result.errors.append({**report, "reason": "; ".join(row.issues)})
            continue
        try:
            with session.begin_nested():
                if row.action == "UPDATE":
                    item = session.get(SubmissionReviewer, row.existing_id)
                    if item is None:
                        raise LookupError("reviewer assignment disappeared")
                else:
                    item = SubmissionReviewer(submission_id=row.submission_id, ojs_reviewer=row.ojs_reviewer, review_round=row.review_round)
                    session.add(item)
                item.reviewer_name = row.reviewer_name
                item.review_state = row.review_state
                item.recommendation = row.recommendation
                item.date_assigned = row.date_assigned
                item.date_completed = row.date_completed
                session.flush()
            if row.action == "UPDATE":
                result.updated += 1
            else:
                result.added += 1
            if row.issues:
                result.errors.append({**report, "status": "NOTE", "reason": "; ".join(row.issues)})
        except Exception as exc:
            result.invalid += 1
            result.errors.append({**report, "status": "INVALID_ROW", "reason": f"Database rejected row: {type(exc).__name__}"})
    safe_filename = filename.replace("\\", "/").split("/")[-1][:255]
    log_audit(session, action="SUBMISSION_REVIEWERS_IMPORT_BATCH", object_type="submission_review_import", object_id=uuid.uuid4(),
              user_id=user.id, journal_id=journal.id,
              new={"filename": safe_filename, "total_rows": result.total_rows, "added_rows": result.added,
                   "updated_rows": result.updated, "unchanged_rows": result.unchanged,
                   "not_found_rows": result.not_found, "title_mismatch_rows": result.title_mismatch,
                   "duplicate_rows": result.duplicates, "invalid_rows": result.invalid})
    session.commit()
    return result
