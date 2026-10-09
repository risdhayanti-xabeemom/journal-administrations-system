from __future__ import annotations

import io
import uuid
from datetime import date

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from models import AuditLog, Author, Base, EditorialStatus, Journal, LoADocument, PublicationStatus, Role, Submission, User
from services.ojs_import import (
    INCOMPLETE_MARKER,
    ImportFileError,
    build_preview,
    clean_text,
    confirm_import,
    detect_columns,
    error_report_csv,
    read_import_file,
)


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


def csv_table(text: str):
    return read_import_file("ojs.csv", text.encode("utf-8-sig"))


def required_mapping(table):
    return detect_columns(table.headers)[0]


def test_exact_jas_column_names(records):
    session, journal, _, user = records
    table = csv_table("ojs_submission_id,manuscript_title,authors,corresponding_author,email\n1550,Exact title,Ada; Bima,Ada,ada@example.test\n")
    mapping = required_mapping(table)
    assert all(mapping[field] is not None for field in ("ojs_submission_id", "manuscript_title", "authors", "corresponding_author", "email"))
    result = confirm_import(session, journal, user, table, mapping, "ojs.csv")
    assert result.imported == 1
    item = session.scalar(select(Submission).where(Submission.journal_id == journal.id))
    assert item.manuscript_title == "Exact title"
    assert [author.name for author in item.authors] == ["Ada", "Bima"]


def test_typical_ojs_aliases_case_spaces_hyphens_and_ambiguity():
    headers = (" SUBMISSION-ID ", "ARTICLE TITLE", "Contributors", "PRIMARY-CONTACT", "Contact Email")
    mapping, warnings = detect_columns(headers)
    assert [mapping[field] for field in ("ojs_submission_id", "manuscript_title", "authors", "corresponding_author", "email")] == [0, 1, 2, 3, 4]
    assert not warnings
    ambiguous, warnings = detect_columns(("id", "title", "article-title", "author"))
    assert ambiguous["manuscript_title"] is None
    assert ambiguous["authors"] is None
    assert ambiguous["corresponding_author"] is None
    assert "manuscript_title" in warnings and "authors" in warnings
    manual = {**ambiguous, "manuscript_title": 1, "authors": 3, "corresponding_author": 3}
    assert manual["authors"] == manual["corresponding_author"]


def test_ojs_alias_file_import_and_author_order(records):
    session, journal, _, user = records
    table = csv_table("Submission-ID;Article Title;Contributors;Primary Contact;Contact Email\n1551;OJS title;Zara;Zara;zara@example.test\n")
    mapping = required_mapping(table)
    result = confirm_import(session, journal, user, table, mapping, "C:\\private\\ojs.csv")
    assert result.imported == 1
    item = session.scalar(select(Submission).where(Submission.ojs_submission_id == "1551"))
    assert item.email == "zara@example.test"
    assert [author.name for author in item.authors] == ["Zara"]
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "SUBMISSION_IMPORT_BATCH"))
    assert "C:\\private" not in audit.new_value
    assert '"filename": "ojs.csv"' in audit.new_value


def test_missing_optional_fields_are_imported_and_flagged(records):
    session, journal, _, user = records
    table = csv_table("submission_id,title\n1552,A sparse but valid title\n")
    preview = build_preview(session, journal, table, required_mapping(table))
    assert preview[0].status == "INCOMPLETE_METADATA"
    result = confirm_import(session, journal, user, table, required_mapping(table), "ojs.csv")
    assert (result.imported, result.incomplete, result.invalid) == (1, 1, 0)
    item = session.scalar(select(Submission).where(Submission.ojs_submission_id == "1552"))
    assert item.notes.startswith(INCOMPLETE_MARKER)
    assert item.corresponding_author == "" and item.email == ""


def test_missing_required_and_malformed_rows_do_not_abort_file(records):
    session, journal, _, user = records
    table = csv_table("id,title\n1553,A\n1554,\n1555,Valid title,unexpected-cell\n1556,Another valid title\n")
    preview = build_preview(session, journal, table, required_mapping(table))
    assert [row.status for row in preview] == ["INCOMPLETE_METADATA", "MISSING_REQUIRED_FIELD", "INVALID_ROW", "INCOMPLETE_METADATA"]
    result = confirm_import(session, journal, user, table, required_mapping(table), "ojs.csv")
    assert (result.imported, result.invalid) == (2, 2)
    assert session.scalar(select(func.count(Submission.id))) == 2
    report = error_report_csv(result).decode("utf-8-sig")
    assert "MISSING_REQUIRED_FIELD" in report and "INVALID_ROW" in report


def test_broken_csv_quote_is_reported_and_later_rows_recovered(records):
    session, journal, _, user = records
    table = csv_table('id,title\n1601,Good first\n1602,"broken quote\n1603,Good later\n')
    preview = build_preview(session, journal, table, required_mapping(table))
    assert [row.status for row in preview] == ["INCOMPLETE_METADATA", "INVALID_ROW", "INCOMPLETE_METADATA"]
    result = confirm_import(session, journal, user, table, required_mapping(table), "ojs.csv")
    assert (result.imported, result.invalid) == (2, 1)


def test_duplicate_check_is_journal_scoped_and_skip_is_default(records):
    session, journal, other, user = records
    session.add(Submission(journal_id=journal.id, ojs_submission_id="1557", manuscript_title="Original", corresponding_author="", email=""))
    session.commit()
    table = csv_table("id,title\n1557,Replacement\n")
    preview = build_preview(session, journal, table, required_mapping(table))
    assert preview[0].status == "DUPLICATE" and preview[0].action == "SKIP"
    result = confirm_import(session, journal, user, table, required_mapping(table), "ojs.csv")
    assert (result.imported, result.skipped_duplicates) == (0, 1)
    assert session.scalar(select(Submission).where(Submission.journal_id == journal.id)).manuscript_title == "Original"
    other_result = confirm_import(session, other, user, table, required_mapping(table), "ojs.csv")
    assert other_result.imported == 1


def test_explicit_metadata_update_never_overwrites_accepted_or_publication(records):
    session, journal, _, user = records
    protected = Submission(journal_id=journal.id, ojs_submission_id="1558", manuscript_title="Accepted title", corresponding_author="Ada", email="ada@example.test", editorial_status=EditorialStatus.ACCEPTED, publication_status=PublicationStatus.PUBLISHED)
    editable = Submission(journal_id=journal.id, ojs_submission_id="1559", manuscript_title="Old draft", corresponding_author="", email="")
    session.add_all([protected, editable])
    session.commit()
    session.add(Author(submission_id=editable.id, name="Old author", position=1, is_corresponding=False))
    session.commit()
    table = csv_table("id,title,contact_email,author_names\n1558,Do not replace,change@example.test,Protected author\n1559,New draft,new@example.test,First; Second\n")
    result = confirm_import(session, journal, user, table, required_mapping(table), "ojs.csv", update_existing=True)
    assert (result.updated, result.skipped_duplicates) == (1, 1)
    assert protected.manuscript_title == "Accepted title" and protected.publication_status == PublicationStatus.PUBLISHED
    assert editable.manuscript_title == "New draft" and editable.email == "new@example.test"
    assert [author.name for author in editable.authors] == ["First", "Second"]


def test_html_entities_decode_without_rendering_html(records):
    session, journal, _, user = records
    table = csv_table("id,title,author,email\n1560,&lt;b&gt;Safe &amp; Sound&lt;/b&gt;,\"&lt;i&gt;Ada&lt;/i&gt;\",ada@example.test\n")
    mapping = required_mapping(table)
    mapping["authors"] = 2
    mapping["corresponding_author"] = 2
    preview = build_preview(session, journal, table, mapping)
    assert preview[0].values["manuscript_title"] == "Safe & Sound"
    assert preview[0].values["authors"] == "Ada"
    confirm_import(session, journal, user, table, mapping, "ojs.csv")
    item = session.scalar(select(Submission).where(Submission.ojs_submission_id == "1560"))
    assert item.manuscript_title == "Safe & Sound"
    assert clean_text("&lt;script&gt;alert(1)&lt;/script&gt;&lt;b&gt;Safe&lt;/b&gt;") == "Safe"


def test_utf8_bom_common_delimiters_and_xlsx(records):
    session, journal, _, user = records
    table = read_import_file("ojs.csv", "id\ttitle\n1561\tTab title\n".encode("utf-8-sig"))
    assert table.headers == ("id", "title")
    assert confirm_import(session, journal, user, table, required_mapping(table), "ojs.csv").imported == 1

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Article ID", "Localized Title", "Primary Contact", "Email Address"])
    sheet.append([1562, "Excel title", "Bima", "bima@example.test"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    excel = read_import_file("ojs.xlsx", buffer.getvalue())
    assert confirm_import(session, journal, user, excel, required_mapping(excel), "ojs.xlsx").imported == 1
    assert session.scalar(select(Submission).where(Submission.ojs_submission_id == "1562")).manuscript_title == "Excel title"


def test_invalid_csv_encoding_has_useful_error():
    with pytest.raises(ImportFileError, match="UTF-8"):
        read_import_file("ojs.csv", b"id,title\n1,\xe9\n")


def test_uncertain_long_author_text_is_retained_without_truncation(records):
    session, journal, _, user = records
    raw = "Author " + "X" * 260
    table = csv_table(f"id,title,authors\n1566,Long author,{raw}\n")
    result = confirm_import(session, journal, user, table, required_mapping(table), "ojs.csv")
    assert result.imported == 1 and result.incomplete == 1
    item = session.scalar(select(Submission).where(Submission.ojs_submission_id == "1566"))
    assert raw in item.notes
    assert item.authors == []


def test_error_report_escapes_spreadsheet_formulas(records):
    session, journal, _, user = records
    table = csv_table("id,title\n=HYPERLINK(1),\n")
    result = confirm_import(session, journal, user, table, required_mapping(table), "ojs.csv")
    assert "'=HYPERLINK" in error_report_csv(result).decode("utf-8-sig")


def test_partial_valid_invalid_duplicate_file_and_audit(records):
    session, journal, _, user = records
    session.add(Submission(journal_id=journal.id, ojs_submission_id="1564", manuscript_title="Already there", corresponding_author="", email=""))
    session.commit()
    table = csv_table("id,title,authors,primary_contact,contact_email\n1563,Valid,Alpha; Beta,Alpha,alpha@example.test\n1564,Duplicate,Alpha,Alpha,alpha@example.test\n,Missing ID,Alpha,Alpha,alpha@example.test\n1565,A,,,\n")
    result = confirm_import(session, journal, user, table, required_mapping(table), "ojs.csv")
    assert (result.total_rows, result.imported, result.skipped_duplicates, result.invalid, result.incomplete) == (4, 2, 1, 1, 2)
    assert [author.name for author in session.scalar(select(Submission).where(Submission.ojs_submission_id == "1563")).authors] == ["Alpha", "Beta"]
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "SUBMISSION_IMPORT_BATCH"))
    assert '"total_rows": 4' in audit.new_value and '"invalid_rows": 1' in audit.new_value


def test_ojs_publication_stages_import_with_status_publication_and_decision_date(records):
    session, journal, _, user = records
    table = csv_table(
        "Submission ID,Title,Status,Date submitted,Editor Decision 1  (Editor 1),Date decided 1  (Editor 1),Editor Decision 2  (Editor 1),Date decided 2  (Editor 1)\n"
        "2001,Published paper,Published,2024-01-05 10:00:00,Send to Review,2024-01-10 10:00:00,Accept Submission,2024-02-01 09:30:00\n"
        "2002,In production,Production,2024-01-06 10:00:00,Accept Submission,2024-03-01 09:30:00,,\n"
        "2003,Still in review,Review,2024-01-07 10:00:00,Send to Review,2024-01-12 10:00:00,,\n"
        "2004,Declined paper,Declined,2024-01-08 10:00:00,Decline Submission,2024-01-20 10:00:00,,\n"
    )
    mapping = required_mapping(table)
    preview = build_preview(session, journal, table, mapping)
    assert not any("Unrecognized editorial status" in issue for row in preview for issue in row.issues)
    result = confirm_import(session, journal, user, table, mapping, "ojs.csv")
    assert result.imported == 4
    items = {item.ojs_submission_id: item for item in session.scalars(select(Submission))}
    assert (items["2001"].editorial_status, items["2001"].publication_status) == (EditorialStatus.ACCEPTED, PublicationStatus.PUBLISHED)
    assert items["2001"].date_accepted == date(2024, 2, 1)
    assert (items["2002"].editorial_status, items["2002"].publication_status) == (EditorialStatus.ACCEPTED, PublicationStatus.NOT_READY)
    assert items["2002"].date_accepted == date(2024, 3, 1)
    assert (items["2003"].editorial_status, items["2003"].date_accepted) == (EditorialStatus.UNDER_REVIEW, None)
    assert (items["2004"].editorial_status, items["2004"].date_accepted) == (EditorialStatus.REJECTED, None)


def test_status_sync_completes_existing_records_forward_only_and_respects_documents(records):
    session, journal, _, user = records

    def seed(ojs_id, **extra):
        item = Submission(journal_id=journal.id, ojs_submission_id=ojs_id, manuscript_title=f"Paper {ojs_id}", corresponding_author="", email="", **extra)
        session.add(item)
        return item

    plain, locked = seed("3001"), seed("3002")
    rejected, in_review = seed("3003", editorial_status=EditorialStatus.REJECTED), seed("3004")
    session.commit()
    session.add(LoADocument(journal_id=journal.id, submission_id=locked.id, verification_id=uuid.uuid4(), document_number="LOCK/3002", issue_date=date(2026, 1, 1)))
    session.commit()
    table = csv_table(
        "Submission ID,Title,Status,Editor Decision 1  (Editor 1),Date decided 1  (Editor 1)\n"
        "3001,Paper 3001,Published,Accept Submission,2024-02-01 09:00:00\n"
        "3002,Paper 3002,Published,Accept Submission,2024-02-01 09:00:00\n"
        "3003,Paper 3003,Published,Accept Submission,2024-02-01 09:00:00\n"
        "3004,Paper 3004,Review,,\n"
    )
    mapping = required_mapping(table)
    assert [row.action for row in build_preview(session, journal, table, mapping)] == ["SKIP"] * 4

    preview = build_preview(session, journal, table, mapping, sync_status=True)
    assert [row.action for row in preview] == ["COMPLETE", "SKIP", "SKIP", "COMPLETE"]
    assert "SUBMITTED → ACCEPTED" in preview[0].info[0]

    result = confirm_import(session, journal, user, table, mapping, "ojs.csv", sync_status=True)
    assert (result.updated, result.status_updated, result.skipped_duplicates, result.incomplete) == (2, 2, 2, 0)
    assert (plain.editorial_status, plain.publication_status, plain.date_accepted) == (EditorialStatus.ACCEPTED, PublicationStatus.PUBLISHED, date(2024, 2, 1))
    assert (locked.editorial_status, locked.publication_status) == (EditorialStatus.SUBMITTED, PublicationStatus.NOT_READY)
    assert rejected.editorial_status == EditorialStatus.REJECTED
    assert in_review.editorial_status == EditorialStatus.UNDER_REVIEW
    assert session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "SUBMISSION_IMPORT_COMPLETED")) == 2

    again = confirm_import(session, journal, user, table, mapping, "ojs.csv", sync_status=True)
    assert (again.updated, again.status_updated) == (0, 0)


AUTHOR_HEADER = (
    "Submission ID,Title,Given Name (Author 1),Family Name (Author 1),Email (Author 1),Affiliation (Author 1),"
    "Given Name (Author 2),Family Name (Author 2),Email (Author 2),Affiliation (Author 2)\n"
)


def test_ojs_author_columns_fill_authors_corresponding_author_and_email(records):
    session, journal, _, user = records
    table = csv_table(
        AUTHOR_HEADER
        + "4001,Two authors,Ada,Lovelace,ADA@example.test,Polinema,Bima,,bima@example.test,\n"
        + "4002,One author,Citra,Dewi,citra@example.test,,,,,\n"
    )
    mapping = required_mapping(table)
    preview = build_preview(session, journal, table, mapping)
    assert not any(issue in ("Missing authors.", "Missing corresponding_author.", "Missing email.") for row in preview for issue in row.issues)
    assert confirm_import(session, journal, user, table, mapping, "ojs.csv").imported == 2
    two = session.scalar(select(Submission).where(Submission.ojs_submission_id == "4001"))
    assert (two.corresponding_author, two.email, two.affiliation) == ("Ada Lovelace", "ada@example.test", "Polinema")
    assert [(a.name, a.is_corresponding, a.email) for a in two.authors] == [
        ("Ada Lovelace", True, "ada@example.test"), ("Bima", False, "bima@example.test"),
    ]
    one = session.scalar(select(Submission).where(Submission.ojs_submission_id == "4002"))
    assert (one.corresponding_author, one.affiliation, [a.name for a in one.authors]) == ("Citra Dewi", None, ["Citra Dewi"])


def test_fill_blanks_completes_empty_author_fields_on_accepted_records_without_overwriting(records):
    session, journal, _, user = records
    empty = Submission(journal_id=journal.id, ojs_submission_id="4101", manuscript_title="Accepted paper", corresponding_author="", email="",
                       editorial_status=EditorialStatus.ACCEPTED, publication_status=PublicationStatus.PUBLISHED)
    filled = Submission(journal_id=journal.id, ojs_submission_id="4102", manuscript_title="Other paper", corresponding_author="Existing Person",
                        email="existing@example.test", affiliation="Old Univ")
    session.add_all([empty, filled])
    session.commit()
    session.add(Author(submission_id=filled.id, name="Existing Person", position=1, is_corresponding=True))
    session.commit()
    table = csv_table(
        AUTHOR_HEADER
        + "4101,Accepted paper,Ada,Lovelace,ada@example.test,Polinema,,,,\n"
        + "4102,Other paper,Citra,Dewi,citra@example.test,New Univ,,,,\n"
    )
    mapping = required_mapping(table)
    assert [row.action for row in build_preview(session, journal, table, mapping)] == ["SKIP", "SKIP"]
    preview = build_preview(session, journal, table, mapping, fill_blanks=True)
    assert [row.action for row in preview] == ["COMPLETE", "SKIP"]
    result = confirm_import(session, journal, user, table, mapping, "ojs.csv", fill_blanks=True)
    assert (result.updated, result.filled, result.status_updated, result.skipped_duplicates) == (1, 1, 0, 1)
    assert (empty.corresponding_author, empty.email, empty.affiliation) == ("Ada Lovelace", "ada@example.test", "Polinema")
    assert [a.name for a in empty.authors] == ["Ada Lovelace"]
    assert (empty.editorial_status, empty.publication_status) == (EditorialStatus.ACCEPTED, PublicationStatus.PUBLISHED)
    assert (filled.corresponding_author, filled.email, filled.affiliation) == ("Existing Person", "existing@example.test", "Old Univ")
    assert [a.name for a in filled.authors] == ["Existing Person"]
    again = confirm_import(session, journal, user, table, mapping, "ojs.csv", fill_blanks=True)
    assert (again.updated, again.filled) == (0, 0)
