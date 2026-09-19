from __future__ import annotations

import io

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from models import AuditLog, Author, Base, EditorialStatus, Journal, PublicationStatus, Role, Submission, User
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
