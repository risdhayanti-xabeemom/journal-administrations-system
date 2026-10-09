"""Pure-logic tests for OJS status mapping and review helpers (no database needed)."""

from __future__ import annotations

from datetime import date

import pytest

from services.ojs_status import (
    TITLE_OVERLAP_MINIMUM,
    accepted_date_from_decisions,
    editorial_move_allowed,
    find_decision_columns,
    map_ojs_status,
    parse_date,
    review_state,
    reviewer_display_name,
    title_overlap,
)


@pytest.mark.parametrize(
    "ojs_value, expected",
    [
        ("Submission", ("SUBMITTED", None)),
        ("Review", ("UNDER_REVIEW", None)),
        ("Copyediting", ("ACCEPTED", None)),
        ("Production", ("ACCEPTED", None)),
        ("Scheduled", ("ACCEPTED", None)),
        ("Published", ("ACCEPTED", "PUBLISHED")),
        ("Declined", ("REJECTED", None)),
        ("  under review ", ("UNDER_REVIEW", None)),
        ("In-Review", ("UNDER_REVIEW", None)),
        ("Something new", (None, None)),
        ("", (None, None)),
    ],
)
def test_ojs_status_mapping(ojs_value, expected):
    assert map_ojs_status(ojs_value) == expected


def test_decision_columns_are_paired_by_number_and_editor():
    headers = (
        "Submission ID", "Title",
        "Editor Decision 1  (Editor 1)", "Date decided 1  (Editor 1)",
        "Editor Decision 2  (Editor 1)", "Date decided 2  (Editor 1)",
        "Editor Decision 1  (Editor 2)", "Date decided 1  (Editor 2)",
        "Given Name (Editor 1)",
    )
    assert find_decision_columns(headers) == [(2, 3), (4, 5), (6, 7)]
    assert find_decision_columns(("id", "title", "Editor Decision 1  (Editor 1)")) == []


def test_accepted_date_uses_earliest_accept_then_production_and_ignores_recommendations():
    assert accepted_date_from_decisions([
        ("Send to Review", "2024-01-01 10:00:00"),
        ("Accept Submission", "2024-03-05 08:00:00"),
        ("Accept Submission", "2024-02-01 08:00:00"),
        ("Send To Production", "2024-01-15 08:00:00"),
    ]) == date(2024, 2, 1)
    assert accepted_date_from_decisions([("Send To Production", "2024-04-02 09:00:00")]) == date(2024, 4, 2)
    assert accepted_date_from_decisions([("Recommendation: Accept Submission", "2024-01-01 00:00:00")]) is None
    assert accepted_date_from_decisions([("Accept Submission", "not a date"), ("", "")]) is None
    assert accepted_date_from_decisions([]) is None


def test_parse_date_formats():
    assert parse_date("2024-12-02 18:45:05") == date(2024, 12, 2)
    assert parse_date("02/12/2024") == date(2024, 12, 2)
    assert parse_date("") is None and parse_date("soon") is None


def test_status_sync_moves_forward_only():
    assert editorial_move_allowed("SUBMITTED", "ACCEPTED")
    assert editorial_move_allowed("SUBMITTED", "UNDER_REVIEW")
    assert editorial_move_allowed("UNDER_REVIEW", "REJECTED")
    assert not editorial_move_allowed("UNDER_REVIEW", "SUBMITTED")
    assert not editorial_move_allowed("ACCEPTED", "UNDER_REVIEW")
    assert not editorial_move_allowed("REJECTED", "ACCEPTED")
    assert not editorial_move_allowed("WITHDRAWN", "ACCEPTED")


def test_review_state_priority():
    assert review_state("No", "No", "", "") == "PENDING"
    assert review_state("No", "No", "2024-01-01", "") == "IN_PROGRESS"
    assert review_state("No", "No", "2024-01-01", "2024-02-01") == "COMPLETED"
    assert review_state("Yes", "No", "2024-01-01", "") == "DECLINED"
    assert review_state("No", "Yes", "2024-01-01", "2024-02-01") == "CANCELLED"


def test_reviewer_display_name_fallbacks():
    assert reviewer_display_name("Indah", "Agustien", "", "hari_kurnia") == "Indah Agustien"
    assert reviewer_display_name("Wahyu", "", "", "wtriwahono") == "Wahyu"
    assert reviewer_display_name("", "", "Dr. Full Name", "user1") == "Dr. Full Name"
    assert reviewer_display_name("", "", "", "user1") == "user1"


def test_title_overlap_tolerates_revisions_but_catches_other_papers():
    assert title_overlap("Acquisition of PLC data to the database", "The Acquisition of PLC data to the database in the Siemens") >= TITLE_OVERLAP_MINIMUM
    assert title_overlap("Same Title", "same   title") == 1.0
    assert title_overlap("Sensor suhu pada inkubator", "Completely unrelated paper about gardening") < TITLE_OVERLAP_MINIMUM
    assert title_overlap("", "Anything") == 0.0
