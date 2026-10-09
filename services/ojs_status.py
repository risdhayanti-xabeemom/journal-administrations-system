"""Pure helpers that turn OJS export values into JAS status values.

This module deliberately has no database, pandas, or SQLAlchemy imports so the
mapping rules can be tested on their own. Status values are returned as enum
*names* (strings); ``services.ojs_import`` converts them to the JAS enums.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime

# Normalized OJS "Status" value -> (EditorialStatus name, PublicationStatus name or None).
# None for publication means "leave the JAS default (NOT_READY)".
# Everything after acceptance in OJS (copyediting, production, scheduled) is an
# accepted manuscript; only "Published" is also published.
OJS_STATUS_MAP: dict[str, tuple[str, str | None]] = {
    "submitted": ("SUBMITTED", None),
    "submission": ("SUBMITTED", None),
    "under_review": ("UNDER_REVIEW", None),
    "in_review": ("UNDER_REVIEW", None),
    "review": ("UNDER_REVIEW", None),
    "accepted": ("ACCEPTED", None),
    "copyediting": ("ACCEPTED", None),
    "production": ("ACCEPTED", None),
    "scheduled": ("ACCEPTED", None),
    "published": ("ACCEPTED", "PUBLISHED"),
    "rejected": ("REJECTED", None),
    "declined": ("REJECTED", None),
    "withdrawn": ("WITHDRAWN", None),
}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S")


def normalize_token(value: object) -> str:
    """Lower-case a label and collapse every run of non-alphanumerics to one underscore."""
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def parse_date(value: str) -> date | None:
    if not value:
        return None
    for format_string in _DATE_FORMATS:
        try:
            return datetime.strptime(value, format_string).date()
        except ValueError:
            pass
    return None


def map_ojs_status(value: str) -> tuple[str | None, str | None]:
    """Return (editorial name, publication name) for an OJS status, or (None, None) if unknown."""
    if not value:
        return None, None
    return OJS_STATUS_MAP.get(normalize_token(value), (None, None))


_DECISION_HEADER = re.compile(r"^editor_decision_(\d+)_editor_(\d+)$")
_DECISION_DATE_HEADER = re.compile(r"^date_decided_(\d+)_editor_(\d+)$")


def find_decision_columns(headers: Sequence[object]) -> list[tuple[int, int]]:
    """Pair the OJS "Editor Decision N (Editor M)" and "Date decided N (Editor M)" columns.

    Returns (decision column index, date column index) for every pair present.
    """
    decisions: dict[tuple[str, str], int] = {}
    dates: dict[tuple[str, str], int] = {}
    for index, header in enumerate(headers):
        token = normalize_token(header)
        match = _DECISION_HEADER.match(token)
        if match:
            decisions[match.groups()] = index
            continue
        match = _DECISION_DATE_HEADER.match(token)
        if match:
            dates[match.groups()] = index
    return [(decisions[key], dates[key]) for key in sorted(decisions, key=lambda k: (int(k[1]), int(k[0]))) if key in dates]


# A recommendation from a section editor is not a decision, so it never counts.
_ACCEPT_DECISION = "accept_submission"
_PRODUCTION_DECISION = "send_to_production"


def accepted_date_from_decisions(decisions: Iterable[tuple[str, str]]) -> date | None:
    """Earliest "Accept Submission" decision date, else earliest "Send To Production".

    ``decisions`` is an iterable of (decision label, date text) pairs from one article row.
    """
    accepted: list[date] = []
    production: list[date] = []
    for label, text in decisions:
        parsed = parse_date((text or "").strip())
        if parsed is None:
            continue
        token = normalize_token(label or "")
        if token == _ACCEPT_DECISION:
            accepted.append(parsed)
        elif token == _PRODUCTION_DECISION:
            production.append(parsed)
    if accepted:
        return min(accepted)
    if production:
        return min(production)
    return None


# Forward-only moves a status sync may apply to an existing JAS record.
_EDITORIAL_FORWARD: dict[str, set[str]] = {
    "SUBMITTED": {"UNDER_REVIEW", "ACCEPTED", "REJECTED", "WITHDRAWN"},
    "UNDER_REVIEW": {"ACCEPTED", "REJECTED", "WITHDRAWN"},
}


def editorial_move_allowed(current: str, target: str) -> bool:
    """True when OJS may move a JAS record from ``current`` to ``target``.

    Accepted, rejected, and withdrawn records are never changed by a sync.
    """
    return target in _EDITORIAL_FORWARD.get(current, set())


# --- OJS review report helpers -------------------------------------------------------------

# A review row whose title shares fewer than this fraction of its words with the JAS
# title almost certainly belongs to a different journal's submission with the same OJS ID.
TITLE_OVERLAP_MINIMUM = 0.5


def title_overlap(first: str, second: str) -> float:
    """Fraction of the shorter title's words that also appear in the other title (0 to 1).

    Titles change between OJS revisions (a leading "The", a subtitle), so exact
    equality is too strict; the overlap coefficient tolerates that.
    """
    words_a = set(re.findall(r"[a-z0-9]+", (first or "").lower()))
    words_b = set(re.findall(r"[a-z0-9]+", (second or "").lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def _is_yes(value: str) -> bool:
    return normalize_token(value or "") in {"yes", "y", "true", "1"}


def review_state(declined: str, cancelled: str, date_confirmed: str, date_completed: str) -> str:
    """Single label for where one reviewer assignment stands."""
    if _is_yes(cancelled):
        return "CANCELLED"
    if _is_yes(declined):
        return "DECLINED"
    if (date_completed or "").strip():
        return "COMPLETED"
    if (date_confirmed or "").strip():
        return "IN_PROGRESS"
    return "PENDING"


def reviewer_display_name(given: str, family: str, full: str, username: str) -> str:
    """Given + family name, else a single full-name column, else the OJS username."""
    name = " ".join(part.strip() for part in (given, family) if part and part.strip())
    return name or (full or "").strip() or (username or "").strip()
