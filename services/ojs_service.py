from __future__ import annotations

from typing import Any, Protocol


class OJSAdapter(Protocol):
    """Contract for a verified, site-specific OJS integration."""

    def get_submission(self, submission_id: str) -> dict[str, Any]: ...

    def get_submission_metadata(self, submission_id: str) -> dict[str, Any]: ...

    def get_authors(self, submission_id: str) -> list[dict[str, Any]]: ...

    def get_editorial_status(self, submission_id: str) -> str: ...


class UnconfiguredOJSAdapter:
    """Explicitly unavailable until an administrator supplies a verified endpoint."""

    message = "OJS integration requires a verified endpoint and credentials. Manual entry/import remains available."

    def _unavailable(self, *_args, **_kwargs):
        raise NotImplementedError(self.message)

    get_submission = _unavailable
    get_submission_metadata = _unavailable
    get_authors = _unavailable
    get_editorial_status = _unavailable

