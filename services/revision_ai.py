"""Opt-in, provider-isolated AI proposal service for unpublished research."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from config import settings


class RevisionAIUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AIProposal:
    paragraph_id: str
    original_text: str
    proposed_text: str
    reason: str
    confidence: int
    requires_author_input: bool


class RevisionAIService(Protocol):
    def generate_revision_proposal(self, *, reviewer_comment: str, paragraph_id: str,
                                   original_text: str, mode: str) -> AIProposal: ...


class DisabledRevisionAIService:
    def generate_revision_proposal(self, **kwargs) -> AIProposal:
        raise RevisionAIUnavailable("Revision AI service is currently unavailable. The editor may enter a proposal manually.")


class CompatibleChatRevisionAIService:
    """Adapter for a configured private OpenAI-compatible chat-completions endpoint."""

    def __init__(self, endpoint: str, model: str, api_key: str):
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise RevisionAIUnavailable("Revision AI endpoint must use HTTPS outside localhost.")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key

    def generate_revision_proposal(self, *, reviewer_comment: str, paragraph_id: str,
                                   original_text: str, mode: str) -> AIProposal:
        mode_instruction = {
            "CONSERVATIVE": "Make only changes directly required by the reviewer comment; do not polish unrelated wording.",
            "REVIEWER_DRIVEN": "Address the reviewer comment and improve grammar only in this affected paragraph.",
            "FULL_ACADEMIC_POLISHING": "Improve academic language in this paragraph while preserving scientific meaning and every protected value.",
        }.get(mode, "Address the comment conservatively.")
        system = (
            "You are an academic copy editor. Return only one JSON object with keys "
            "paragraph_id, original_text, proposed_text, reason, confidence (0-100), "
            "requires_author_input (boolean). Revise ONLY the supplied paragraph to address "
            "the supplied reviewer comment. Do not invent facts, experiments, references, "
            "citations, data, measurements, equations, figures, or tables. Preserve every "
            "number, citation marker, scientific claim, and uncertainty. If the request "
            "requires scientific data or a new artifact, return the original text unchanged "
            "and requires_author_input=true. " + mode_instruction
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"mode": mode, "reviewer_comment": reviewer_comment,
                                                        "paragraph_id": paragraph_id, "original_text": original_text}, ensure_ascii=False)},
            ],
        }
        request = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = json.loads(response.read(1_000_000))
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if parsed.get("paragraph_id") != paragraph_id or parsed.get("original_text") != original_text:
                raise ValueError("AI response target did not match the supplied manuscript paragraph.")
            proposed = parsed["proposed_text"]
            if not isinstance(proposed, str) or not proposed.strip() or len(proposed) > 30000:
                raise ValueError("AI proposal text is empty or too long.")
            return AIProposal(paragraph_id, original_text, proposed, str(parsed.get("reason", ""))[:2000],
                              max(0, min(100, int(parsed.get("confidence", 0)))), bool(parsed.get("requires_author_input", False)))
        except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RevisionAIUnavailable("Revision AI service is currently unavailable or returned invalid structured output.") from exc


def configured_ai_service() -> RevisionAIService:
    if not settings.revision_ai_enabled:
        return DisabledRevisionAIService()
    if settings.revision_ai_provider != "openai_compatible":
        raise RevisionAIUnavailable("Configured revision AI provider is not supported by this deployment.")
    if not settings.revision_ai_endpoint or not settings.revision_ai_model or not settings.revision_ai_api_key:
        raise RevisionAIUnavailable("Revision AI service is currently unavailable. Configure endpoint, model, and API key in environment or Streamlit Secrets.")
    return CompatibleChatRevisionAIService(settings.revision_ai_endpoint, settings.revision_ai_model, settings.revision_ai_api_key)
