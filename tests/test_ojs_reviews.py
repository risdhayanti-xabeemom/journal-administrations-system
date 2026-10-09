from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from models import AuditLog, Base, Journal, Role, Submission, SubmissionReviewer, User
from services.ojs_import import error_report_csv, read_import_file
from services.ojs_reviews import (
    build_review_preview,
    confirm_review_import,
    detect_review_columns,
    missing_review_columns,
)

HEADER = (
    'Stage,Round,"Submission Title","Submission ID",Reviewer,"Given Name","Family Name",Email,'
    '"Date Assigned","Date Confirmed","Date Completed",Declined,Cancelled,Recommendation,"Comments On Submission"\n'
)
TITLE = "Sensor suhu pada inkubator"


@pytest.fixture()
def records():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        journal = Journal(name="First", abbreviation="FIRST")
        other = Journal(name="Second", abbreviation="SECOND")
        user = User(email="importer@example.test", display_name="Importer", password_hash="not-used", role=Role.SUPER_ADMIN)
        session.add_all([journal, other, user])
        session.commit()
        yield session, journal, other, user


def submission(session, journal, ojs_id, title):
    item = Submission(journal_id=journal.id, ojs_submission_id=ojs_id, manuscript_title=title, corresponding_author="", email="")
    session.add(item)
    session.commit()
    return item


def review_table(*rows: str):
    return read_import_file("reviews.csv", (HEADER + "\n".join(rows) + "\n").encode("utf-8-sig"))


def mapping_for(table):
    return detect_review_columns(table.headers)[0]


ASSIGNED = f"Review,1,{TITLE},1001,anindya11,Raden Arief,Setiawan,a@example.test,2024-12-02 18:45:05,2024-12-05 01:04:23,,No,No,,"
COMPLETED = f"Review,1,{TITLE},1001,hari_kurnia,Indah Agustien,Siradjuddin,h@example.test,2024-12-02 18:45:05,2024-12-03 01:00:00,2024-12-10 09:00:00,No,No,Revisions Required,"
ROUND_TWO = "Review,2,Kontrol motor DC,1002,hari_kurnia,Indah Agustien,,h@example.test,2025-01-02 10:00:00,,,No,No,,"
UNKNOWN = "Review,1,Unknown paper,9999,someone,Some,One,s@example.test,2025-01-02 10:00:00,,,No,No,,"


def test_review_report_columns_are_detected_and_other_files_are_rejected():
    table = review_table(ASSIGNED)
    mapping, warnings = detect_review_columns(table.headers)
    assert not warnings and not missing_review_columns(mapping)
    assert table.headers[mapping["reviewer_username"]] == "Reviewer"
    assert table.headers[mapping["given_name"]] == "Given Name"
    assert table.headers[mapping["review_round"]] == "Round"
    other = read_import_file("articles.csv", b"id,title\n1,A paper\n")
    assert missing_review_columns(detect_review_columns(other.headers)[0])


def test_reviewers_are_matched_by_ojs_id_stored_without_email_and_import_is_idempotent(records):
    session, journal, _, user = records
    submission(session, journal, "1001", TITLE)
    submission(session, journal, "1002", "Kontrol motor DC")
    table = review_table(ASSIGNED, COMPLETED, ROUND_TWO, UNKNOWN)
    mapping = mapping_for(table)
    preview = build_review_preview(session, journal, table, mapping)
    assert [(row.status, row.action) for row in preview] == [
        ("READY", "ADD"), ("READY", "ADD"), ("READY", "ADD"), ("SUBMISSION_NOT_FOUND", "SKIP"),
    ]
    assert session.scalar(select(func.count(SubmissionReviewer.id))) == 0

    result = confirm_review_import(session, journal, user, table, mapping, "C:\\private\\reviews.csv")
    assert (result.added, result.unchanged, result.not_found, result.invalid) == (3, 0, 1, 0)
    stored = {
        (item.submission.ojs_submission_id, item.review_round, item.ojs_reviewer): item
        for item in list(session.scalars(select(SubmissionReviewer)))
    }
    done = stored[("1001", 1, "hari_kurnia")]
    assert done.reviewer_name == "Indah Agustien Siradjuddin"
    assert (done.review_state, done.recommendation, done.date_completed) == ("COMPLETED", "Revisions Required", date(2024, 12, 10))
    assert stored[("1001", 1, "anindya11")].review_state == "IN_PROGRESS"
    assert stored[("1002", 2, "hari_kurnia")].reviewer_name == "Indah Agustien"
    assert stored[("1002", 2, "hari_kurnia")].review_state == "PENDING"
    assert not hasattr(SubmissionReviewer, "email")

    again = confirm_review_import(session, journal, user, table, mapping, "reviews.csv")
    assert (again.added, again.updated, again.unchanged) == (0, 0, 3)
    assert session.scalar(select(func.count(SubmissionReviewer.id))) == 3

    done.review_state = "PENDING"
    done.reviewer_name = "Old name"
    session.commit()
    fixed = confirm_review_import(session, journal, user, table, mapping, "reviews.csv")
    assert (fixed.updated, fixed.unchanged) == (1, 2)
    assert done.reviewer_name == "Indah Agustien Siradjuddin" and done.review_state == "COMPLETED"

    audit = session.scalars(select(AuditLog).where(AuditLog.action == "SUBMISSION_REVIEWERS_IMPORT_BATCH")).first()
    assert "C:\\private" not in audit.new_value
    assert "Siradjuddin" not in audit.new_value and "example.test" not in audit.new_value
    assert "SUBMISSION_NOT_FOUND" in error_report_csv(result).decode("utf-8-sig")


def test_title_guard_blocks_other_journal_reports_and_can_be_switched_off(records):
    session, journal, _, user = records
    submission(session, journal, "1001", TITLE)
    wrong = "Review,1,Completely unrelated paper about gardening,1001,anindya11,Raden Arief,Setiawan,a@example.test,2024-12-02 18:45:05,,,No,No,,"
    table = review_table(wrong)
    mapping = mapping_for(table)
    assert [(row.status, row.action) for row in build_review_preview(session, journal, table, mapping)] == [("TITLE_MISMATCH", "SKIP")]
    assert confirm_review_import(session, journal, user, table, mapping, "reviews.csv").title_mismatch == 1
    assert session.scalar(select(func.count(SubmissionReviewer.id))) == 0
    allowed = confirm_review_import(session, journal, user, table, mapping, "reviews.csv", check_titles=False)
    assert allowed.added == 1


def test_revised_titles_still_match_and_repeated_rows_are_reported(records):
    session, journal, _, user = records
    submission(session, journal, "1001", "The " + TITLE + " berbasis mikrokontroler")
    table = review_table(ASSIGNED, ASSIGNED)
    mapping = mapping_for(table)
    preview = build_review_preview(session, journal, table, mapping)
    assert [(row.status, row.action) for row in preview] == [("READY", "ADD"), ("DUPLICATE", "SKIP")]
    result = confirm_review_import(session, journal, user, table, mapping, "reviews.csv")
    assert (result.added, result.duplicates) == (1, 1)


def test_review_import_is_scoped_to_the_active_journal(records):
    session, journal, other, user = records
    mine = submission(session, journal, "1001", TITLE)
    theirs = submission(session, other, "1001", TITLE)
    table = review_table(ASSIGNED)
    result = confirm_review_import(session, journal, user, table, mapping_for(table), "reviews.csv")
    assert result.added == 1
    assert session.scalar(select(func.count(SubmissionReviewer.id)).where(SubmissionReviewer.submission_id == mine.id)) == 1
    assert session.scalar(select(func.count(SubmissionReviewer.id)).where(SubmissionReviewer.submission_id == theirs.id)) == 0


def test_username_is_used_when_the_report_has_no_display_name(records):
    session, journal, _, user = records
    submission(session, journal, "1001", TITLE)
    table = review_table(f"Review,1,{TITLE},1001,solo_user,,,x@example.test,2024-12-02 18:45:05,,,No,No,,")
    result = confirm_review_import(session, journal, user, table, mapping_for(table), "reviews.csv")
    assert result.added == 1
    assert session.scalar(select(SubmissionReviewer.reviewer_name)) == "solo_user"
    assert "OJS username is used" in error_report_csv(result).decode("utf-8-sig")


def test_review_preview_requires_the_review_columns(records):
    session, journal, _, _ = records
    table = read_import_file("articles.csv", b"id,title\n1,A paper\n")
    with pytest.raises(ValueError, match="missing required column"):
        build_review_preview(session, journal, table, detect_review_columns(table.headers)[0])
