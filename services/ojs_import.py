"""Preview-first OJS CSV/XLSX import without database schema changes."""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser

import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Author, EditorialStatus, Invoice, Journal, LoADocument, PublicationStatus, Submission, User
from services.core import assert_journal_access, log_audit
from models import Role


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "ojs_submission_id": ("ojs_submission_id", "submission_id", "id", "article_id"),
    "manuscript_title": ("manuscript_title", "title", "submission_title", "article_title", "localized_title"),
    "authors": ("authors", "author", "author_names", "contributors"),
    "corresponding_author": ("corresponding_author", "primary_contact", "contact_name", "corresponding_author_name", "author_name", "author"),
    "email": ("email", "author_email", "contact_email", "corresponding_email", "email_address"),
    "affiliation": ("affiliation", "author_affiliation", "primary_affiliation", "institution"),
    "editorial_status": ("editorial_status", "submission_status", "status", "current_status"),
    "date_submitted": ("date_submitted", "submission_date", "date_submission", "submitted_at"),
    "date_accepted": ("date_accepted", "acceptance_date", "accepted_at"),
    "planned_volume": ("planned_volume", "volume", "publication_volume"),
    "planned_issue": ("planned_issue", "issue", "number", "publication_issue"),
    "planned_publication_month": ("planned_publication_month", "publication_month", "month"),
    "planned_year": ("planned_year", "publication_year", "year"),
    "notes": ("notes", "comments"),
}
REQUIRED_FIELDS = ("ojs_submission_id", "manuscript_title")
RECOMMENDED_FIELDS = tuple(field for field in FIELD_ALIASES if field not in (*REQUIRED_FIELDS, "notes"))
INCOMPLETE_MARKER = "[INCOMPLETE_METADATA]"


class ImportFileError(ValueError):
    pass


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.ignored += 1
        elif tag.lower() in {"br", "div", "p", "li"} and not self.ignored:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.ignored:
            self.ignored -= 1
        elif tag.lower() in {"div", "p", "li"} and not self.ignored:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def clean_text(value: object) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = unescape(str(value))
    parser = _PlainText()
    parser.feed(text)
    text = "".join(parser.parts)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return re.sub(r"\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def normalize_column(value: object) -> str:
    return re.sub(r"_+", "_", re.sub(r"[\s-]+", "_", str(value).strip().lower())).strip("_")


@dataclass(frozen=True)
class SourceRow:
    number: int
    values: tuple[object, ...]
    parse_error: str = ""


@dataclass(frozen=True)
class ImportTable:
    headers: tuple[str, ...]
    rows: tuple[SourceRow, ...]


@dataclass
class PreviewRow:
    source_row: int
    values: dict[str, str]
    status: str
    action: str
    issues: list[str] = field(default_factory=list)
    existing_id: uuid.UUID | None = None
    author_names: list[str] = field(default_factory=list)
    parsed_dates: dict[str, date | None] = field(default_factory=dict)
    parsed_year: int | None = None
    editorial_status: EditorialStatus = EditorialStatus.SUBMITTED


@dataclass
class ImportResult:
    total_rows: int
    imported: int = 0
    skipped_duplicates: int = 0
    updated: int = 0
    incomplete: int = 0
    invalid: int = 0
    errors: list[dict[str, str | int]] = field(default_factory=list)


def read_import_file(filename: str, content: bytes) -> ImportTable:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix == "csv":
        try:
            source = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ImportFileError("CSV must be UTF-8 or UTF-8 with BOM. Re-export the OJS report as UTF-8 CSV.") from exc
        if not source.strip():
            raise ImportFileError("CSV file is empty.")
        try:
            dialect = csv.Sniffer().sniff(source[:8192], delimiters=",;\t|")
            delimiter = dialect.delimiter
        except csv.Error:
            first_line = source.splitlines()[0]
            delimiter = max(",;\t|", key=first_line.count)
        try:
            reader = csv.reader(io.StringIO(source, newline=""), delimiter=delimiter, strict=True)
            headers = tuple(next(reader))
            rows = []
            while True:
                source_row = reader.line_num + 1
                try:
                    cells = next(reader)
                except StopIteration:
                    break
                except csv.Error as exc:
                    rows.append(SourceRow(source_row, (), f"Malformed CSV quoting: {exc}"))
                    # A broken quote can make csv.reader consume the rest of the file.
                    # Recover later physical lines independently; any uncertain line
                    # still fails the normal column-count validation below.
                    for line_number, line in enumerate(source.splitlines()[source_row:], source_row + 1):
                        if not line.strip():
                            continue
                        try:
                            recovered = next(csv.reader([line], delimiter=delimiter, strict=True))
                            error = "" if len(recovered) == len(headers) else f"Expected {len(headers)} columns; found {len(recovered)}."
                            rows.append(SourceRow(line_number, tuple(recovered), error))
                        except csv.Error as recovery_error:
                            rows.append(SourceRow(line_number, (), f"Malformed CSV quoting: {recovery_error}"))
                    break
                if not any(str(cell).strip() for cell in cells):
                    continue
                error = "" if len(cells) == len(headers) else f"Expected {len(headers)} columns; found {len(cells)}."
                rows.append(SourceRow(source_row, tuple(cells), error))
        except (csv.Error, StopIteration) as exc:
            raise ImportFileError(f"Could not parse CSV. Check quoting and delimiter near the reported row: {exc}") from exc
    elif suffix == "xlsx":
        try:
            frame = pd.read_excel(io.BytesIO(content), header=None, dtype=object, keep_default_na=False, engine="openpyxl")
        except Exception as exc:
            raise ImportFileError(f"Could not read XLSX. Confirm it is a valid Excel workbook: {exc}") from exc
        if frame.empty:
            raise ImportFileError("Excel file is empty.")
        headers = tuple(str(value).strip() for value in frame.iloc[0].tolist())
        rows = tuple(
            SourceRow(index + 2, tuple(values))
            for index, values in enumerate(frame.iloc[1:].itertuples(index=False, name=None))
            if any(clean_text(value) for value in values)
        )
    else:
        raise ImportFileError("Supported file types are CSV and XLSX.")
    if not headers or not any(normalize_column(header) for header in headers):
        raise ImportFileError("The first row must contain column headings.")
    return ImportTable(headers, tuple(rows))


def detect_columns(headers: tuple[str, ...]) -> tuple[dict[str, int | None], dict[str, str]]:
    normalized = [normalize_column(header) for header in headers]
    candidates = {
        field: [index for index, name in enumerate(normalized) if name in aliases]
        for field, aliases in FIELD_ALIASES.items()
    }
    result: dict[str, int | None] = {field: None for field in FIELD_ALIASES}
    warnings: dict[str, str] = {}
    reserved: set[int] = set()
    for field in FIELD_ALIASES:
        exact = [index for index in candidates[field] if normalized[index] == field]
        if len(exact) == 1 and normalized.count(field) == 1:
            result[field] = exact[0]
            reserved.add(exact[0])
    changed = True
    while changed:
        changed = False
        unresolved = {field: [i for i in indexes if i not in reserved] for field, indexes in candidates.items() if result[field] is None}
        for field, indexes in unresolved.items():
            if len(indexes) == 1 and normalized.count(normalized[indexes[0]]) == 1:
                competing = [other for other, choices in unresolved.items() if other != field and indexes[0] in choices]
                if not competing:
                    result[field] = indexes[0]
                    reserved.add(indexes[0])
                    changed = True
                    break
    for field, indexes in candidates.items():
        if result[field] is None and indexes:
            warnings[field] = "Ambiguous matches: " + ", ".join(f"{headers[i]} (column {i + 1})" for i in indexes) + ". Select manually."
    return result, warnings


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    for format_string in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, format_string).date()
        except ValueError:
            pass
    return None


def _parse_status(value: str) -> EditorialStatus | None:
    aliases = {
        "submitted": EditorialStatus.SUBMITTED,
        "submission": EditorialStatus.SUBMITTED,
        "under_review": EditorialStatus.UNDER_REVIEW,
        "in_review": EditorialStatus.UNDER_REVIEW,
        "review": EditorialStatus.UNDER_REVIEW,
        "accepted": EditorialStatus.ACCEPTED,
        "rejected": EditorialStatus.REJECTED,
        "declined": EditorialStatus.REJECTED,
        "withdrawn": EditorialStatus.WITHDRAWN,
    }
    return aliases.get(normalize_column(value)) if value else None


def _authors(value: str) -> tuple[list[str], bool]:
    if not value:
        return [], False
    if ";" in value or "\n" in value:
        return [part.strip() for part in re.split(r"[;\n]+", value) if part.strip()], False
    # Commas are also used inside personal names. Preserve the raw field for review.
    return [value], "," in value


def metadata_flag(notes: str | None) -> bool:
    return bool(notes and notes.startswith(INCOMPLETE_MARKER))


def _notes(source: str, issues: list[str], raw_authors: str = "") -> str | None:
    existing = source.strip()
    if existing.startswith(INCOMPLETE_MARKER):
        existing = existing.split("\n", 1)[-1] if "\n" in existing else ""
    if not issues:
        return existing or None
    marker = INCOMPLETE_MARKER + " Review: " + "; ".join(issues)
    if raw_authors:
        marker += "\nRaw author field: " + raw_authors
    return marker + ("\n" + existing if existing else "")


def _protected(session: Session, submission: Submission) -> bool:
    return bool(
        submission.editorial_status == EditorialStatus.ACCEPTED
        or submission.date_accepted
        or submission.publication_status != PublicationStatus.NOT_READY
        or session.scalar(select(LoADocument.id).where(LoADocument.submission_id == submission.id))
        or session.scalar(select(Invoice.id).where(Invoice.submission_id == submission.id))
    )


def build_preview(
    session: Session,
    journal: Journal,
    table: ImportTable,
    mapping: dict[str, int | None],
    *,
    update_existing: bool = False,
) -> list[PreviewRow]:
    if any(mapping.get(field) is None for field in REQUIRED_FIELDS):
        raise ValueError("Map OJS Submission ID and Manuscript Title before previewing or importing.")
    if any(index is not None and (index < 0 or index >= len(table.headers)) for index in mapping.values()):
        raise ValueError("A mapped source column is outside the uploaded file.")
    seen: set[str] = set()
    output: list[PreviewRow] = []
    for source in table.rows:
        values = {
            field: clean_text(source.values[index]) if index is not None and index < len(source.values) else ""
            for field, index in mapping.items()
        }
        values = {field: values.get(field, "") for field in FIELD_ALIASES}
        ojs_id = values["ojs_submission_id"]
        issues: list[str] = []
        status = "READY"
        action = "IMPORT"
        existing = None
        if source.parse_error:
            status, action = "INVALID_ROW", "SKIP"
            issues.append(source.parse_error)
        elif not ojs_id or not values["manuscript_title"]:
            status, action = "MISSING_REQUIRED_FIELD", "SKIP"
            issues.append("Missing OJS Submission ID or Manuscript Title.")
        elif len(ojs_id) > 128 or len(values["corresponding_author"]) > 255 or len(values["email"]) > 255:
            status, action = "INVALID_ROW", "SKIP"
            issues.append("OJS ID, corresponding author, or email exceeds the database field length.")
        elif ojs_id in seen:
            status, action = "DUPLICATE", "SKIP"
            issues.append("Repeated OJS Submission ID within the uploaded file.")
        else:
            seen.add(ojs_id)
            existing = session.scalar(select(Submission).where(Submission.journal_id == journal.id, Submission.ojs_submission_id == ojs_id))
            if existing:
                status = "DUPLICATE"
                if update_existing and not _protected(session, existing):
                    action = "UPDATE_METADATA"
                else:
                    action = "SKIP"
                    issues.append("Existing submission is protected or duplicate policy is Skip existing.")
        for field in RECOMMENDED_FIELDS:
            if not values[field]:
                issues.append(f"Missing {field}.")
        email = values["email"]
        if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            issues.append("Invalid email format.")
        parsed_dates = {field: _parse_date(values[field]) for field in ("date_submitted", "date_accepted")}
        for field, parsed in parsed_dates.items():
            if values[field] and parsed is None:
                issues.append(f"Unrecognized {field} '{values[field]}'; use YYYY-MM-DD or DD/MM/YYYY.")
        year = None
        if values["planned_year"]:
            try:
                year = int(float(values["planned_year"]))
                if float(values["planned_year"]) != year:
                    raise ValueError
                if not 1900 <= year <= 2200:
                    raise ValueError
            except ValueError:
                issues.append("Invalid publication year.")
                year = None
        editorial = _parse_status(values["editorial_status"])
        if values["editorial_status"] and editorial is None:
            issues.append("Unrecognized editorial status; defaulting to SUBMITTED.")
        author_names, author_review = _authors(values["authors"])
        if author_review:
            issues.append("Author delimiter is uncertain; raw author text retained for review.")
        if any(len(name) > 255 for name in author_names):
            issues.append("Author name exceeds 255 characters; raw author text retained in notes.")
            author_names = []
        if status == "READY" and issues:
            status = "INCOMPLETE_METADATA"
        output.append(PreviewRow(source.number, values, status, action, issues, existing.id if existing else None, author_names, parsed_dates, year, editorial or EditorialStatus.SUBMITTED))
    return output


def preview_table(rows: list[PreviewRow]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Source row": row.source_row,
            "OJS Submission ID": row.values["ojs_submission_id"],
            "Manuscript Title": row.values["manuscript_title"],
            "Corresponding Author": row.values["corresponding_author"],
            "Email": row.values["email"],
            "Status": row.status,
            "Action": row.action,
            "Review notes": "; ".join(row.issues),
        }
        for row in rows
    ])


def _add_authors(session: Session, submission: Submission, row: PreviewRow) -> None:
    names = row.author_names or ([row.values["corresponding_author"]] if row.values["corresponding_author"] else [])
    for position, name in enumerate(names, 1):
        corresponding = name == row.values["corresponding_author"]
        submission.authors.append(Author(name=name, position=position, is_corresponding=corresponding,
                                         email=row.values["email"].lower() if corresponding else None,
                                         affiliation=row.values["affiliation"] or None))


def confirm_import(
    session: Session,
    journal: Journal,
    user: User,
    table: ImportTable,
    mapping: dict[str, int | None],
    filename: str,
    *,
    update_existing: bool = False,
) -> ImportResult:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    rows = build_preview(session, journal, table, mapping, update_existing=update_existing)
    result = ImportResult(total_rows=len(rows))
    for row in rows:
        if row.action == "SKIP":
            if row.status == "DUPLICATE":
                result.skipped_duplicates += 1
            else:
                result.invalid += 1
            result.errors.append({"source_row": row.source_row, "ojs_submission_id": row.values["ojs_submission_id"], "status": row.status, "reason": "; ".join(row.issues)})
            continue
        try:
            completed_action = row.action
            with session.begin_nested():
                if row.action == "UPDATE_METADATA":
                    item = session.get(Submission, row.existing_id)
                    if item is None or _protected(session, item):
                        result.skipped_duplicates += 1
                        result.errors.append({"source_row": row.source_row, "ojs_submission_id": row.values["ojs_submission_id"], "status": "DUPLICATE", "reason": "Existing record became protected or unavailable."})
                        continue
                    previous = {key: getattr(item, key) for key in ("manuscript_title", "corresponding_author", "email", "affiliation")}
                    for key in ("manuscript_title", "corresponding_author", "email", "affiliation"):
                        if row.values[key]:
                            setattr(item, key, row.values[key].lower() if key == "email" else row.values[key])
                    if row.parsed_dates["date_submitted"]:
                        item.date_submitted = row.parsed_dates["date_submitted"]
                    if row.author_names:
                        item.authors.clear()
                        session.flush()
                        _add_authors(session, item, row)
                    raw_authors = row.values["authors"] if row.values["authors"] and not row.author_names else ""
                    item.notes = _notes(row.values["notes"] or item.notes or "", row.issues, raw_authors)
                    log_audit(session, action="SUBMISSION_IMPORT_UPDATED", object_type="submission", object_id=item.id, user_id=user.id, journal_id=journal.id, previous=previous, new={key: getattr(item, key) for key in previous})
                else:
                    raw_authors = row.values["authors"] if row.values["authors"] and not row.author_names else ""
                    item = Submission(
                        journal_id=journal.id, ojs_submission_id=row.values["ojs_submission_id"],
                        manuscript_title=row.values["manuscript_title"], corresponding_author=row.values["corresponding_author"],
                        email=row.values["email"].lower(), affiliation=row.values["affiliation"] or None,
                        date_submitted=row.parsed_dates["date_submitted"], date_accepted=row.parsed_dates["date_accepted"],
                        editorial_status=row.editorial_status,
                        planned_volume=row.values["planned_volume"] or journal.default_volume,
                        planned_issue=row.values["planned_issue"] or journal.default_issue,
                        planned_publication_month=row.values["planned_publication_month"] or journal.default_publication_month,
                        planned_year=row.parsed_year or journal.default_publication_year,
                        notes=_notes(row.values["notes"], row.issues, raw_authors), created_by=user.id,
                    )
                    session.add(item)
                    session.flush()
                    _add_authors(session, item, row)
                    log_audit(session, action="SUBMISSION_IMPORTED", object_type="submission", object_id=item.id, user_id=user.id, journal_id=journal.id, new={"ojs_submission_id": item.ojs_submission_id, "metadata_status": "INCOMPLETE_METADATA" if row.issues else "READY"})
            if completed_action == "UPDATE_METADATA":
                result.updated += 1
            else:
                result.imported += 1
            if row.issues:
                result.incomplete += 1
                result.errors.append({"source_row": row.source_row, "ojs_submission_id": row.values["ojs_submission_id"], "status": "INCOMPLETE_METADATA", "reason": "; ".join(row.issues)})
        except Exception as exc:
            duplicate = isinstance(exc, IntegrityError) and bool(session.scalar(select(Submission.id).where(Submission.journal_id == journal.id, Submission.ojs_submission_id == row.values["ojs_submission_id"])))
            if duplicate:
                result.skipped_duplicates += 1
            else:
                result.invalid += 1
            result.errors.append({"source_row": row.source_row, "ojs_submission_id": row.values["ojs_submission_id"], "status": "DUPLICATE" if duplicate else "INVALID_ROW", "reason": "Concurrent duplicate." if duplicate else f"Database rejected row: {type(exc).__name__}"})
    safe_filename = filename.replace("\\", "/").split("/")[-1][:255]
    log_audit(session, action="SUBMISSION_IMPORT_BATCH", object_type="submission_import", object_id=uuid.uuid4(), user_id=user.id, journal_id=journal.id,
              new={"filename": safe_filename, "total_rows": result.total_rows, "imported_rows": result.imported,
                   "skipped_rows": result.skipped_duplicates + result.invalid, "skipped_duplicate_rows": result.skipped_duplicates,
                   "updated_rows": result.updated,
                   "invalid_rows": result.invalid, "incomplete_rows": result.incomplete})
    session.commit()
    return result


def error_report_csv(result: ImportResult) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["source_row", "ojs_submission_id", "status", "reason"])
    writer.writeheader()
    for row in result.errors:
        writer.writerow({key: ("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value) for key, value in row.items()})
    return buffer.getvalue().encode("utf-8-sig")
