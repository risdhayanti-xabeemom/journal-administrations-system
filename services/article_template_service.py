"""Journal-scoped, versioned article DOCX masters in private revision storage."""

from __future__ import annotations

import hashlib
import io
import json
import math
import uuid
from pathlib import Path
from zipfile import ZipFile

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import DocumentTemplate, Journal, Role, TemplateStatus, User
from services.article_formatting import extract_style_profile
from services.core import AuthorizationError, assert_journal_access, log_audit
from services.revision_storage import RevisionStorage, get_revision_storage, safe_filename, validate_revision_upload


class ArticleTemplateError(ValueError):
    pass


RULE_FIELDS = {
    "title_max_words", "abstract_min_words", "abstract_max_words", "keywords_min", "keywords_max",
    "figure_caption_prefix", "table_caption_prefix", "reference_style", "required_sections",
    "body_font", "body_font_size", "style_overrides", "paper", "layout", "default_font",
    "top_margin_mm", "bottom_margin_mm", "left_margin_mm", "right_margin_mm",
    "title_font", "title_size_pt", "title_alignment", "title_case", "abstract_size_pt", "body_size_pt",
    "author_format", "affiliation_format", "abstract_format", "keyword_format", "heading_levels",
    "body_format", "table_format", "table_caption_format", "figure_format", "figure_caption_format",
    "equation_format", "reference_format", "header_footer_mode", "require_lr_margin_confirmation",
    "formatting_profile",
}

FORMAT_OBJECT_FIELDS = {
    "author_format": {"source", "font", "size_pt", "alignment", "preserve_order"},
    "affiliation_format": {"source", "font", "size_pt", "alignment"},
    "abstract_format": {"source", "font", "size_pt", "alignment"},
    "keyword_format": {"source", "font", "size_pt", "alignment", "min", "max"},
    "heading_levels": {"source", "level1", "level2"},
    "body_format": {"source", "font", "size_pt", "alignment"},
    "table_format": {"numbering", "preserve_cells", "font", "size_pt"},
    "table_caption_format": {"position", "size_pt", "alignment", "case", "font"},
    "figure_format": {"numbering", "preserve_images", "alignment"},
    "figure_caption_format": {"position", "size_pt", "single_line_alignment", "multi_line_alignment", "font"},
    "equation_format": {"numbering", "number_alignment", "preserve_omml"},
    "reference_format": {"style", "numbering", "size_pt", "preserve_entries", "font"},
}

# Semantic defaults are not DOCX text placeholders. Left/right margins were decided by the
# administrator as 20 mm, so no further confirmation step is required.
ELKOLIND_INITIAL_RULES: dict[str, object] = {
    "paper": "A4", "top_margin_mm": 19, "bottom_margin_mm": 43,
    "left_margin_mm": 20, "right_margin_mm": 20,
    "layout": "single_column", "default_font": "Gadugi", "title_font": "Gadugi",
    "title_size_pt": 24, "title_alignment": "center", "title_case": "sentence_case",
    "title_max_words": 15, "abstract_size_pt": 9, "abstract_min_words": 100,
    "abstract_max_words": 200, "keywords_min": 3, "keywords_max": 5,
    "body_size_pt": 10, "header_footer_mode": "MASTER", "formatting_profile": "ELKOLIND",
    "require_lr_margin_confirmation": False,
    "figure_caption_prefix": "Gambar", "table_caption_prefix": "TABEL",
    "author_format": {"source": "master", "font": "Gadugi", "size_pt": 10,
        "alignment": "center", "preserve_order": True},
    "affiliation_format": {"source": "master", "font": "Gadugi", "size_pt": 9,
        "alignment": "center"},
    "abstract_format": {"source": "master", "font": "Gadugi", "size_pt": 9,
        "alignment": "justify"},
    "keyword_format": {"source": "master", "font": "Gadugi", "size_pt": 9,
        "min": 3, "max": 5},
    "heading_levels": {"source": "master"},
    "body_format": {"source": "master", "font": "Gadugi", "size_pt": 10,
        "alignment": "justify"},
    "figure_format": {"numbering": "arabic", "preserve_images": True},
    "figure_caption_format": {"position": "below", "font": "Gadugi", "size_pt": 8,
        "single_line_alignment": "center", "multi_line_alignment": "justify"},
    "table_format": {"numbering": "upper_roman", "preserve_cells": True},
    "table_caption_format": {"position": "above", "font": "Gadugi", "alignment": "center",
        "size_pt": 8, "case": "template"},
    "equation_format": {"numbering": "arabic_parentheses", "number_alignment": "right",
        "preserve_omml": True},
    "reference_format": {"style": "IEEE", "numbering": "bracketed_arabic",
        "font": "Gadugi", "size_pt": 8, "preserve_entries": True},
    "style_overrides": {
        "title": {"font": "Gadugi", "size_pt": 24, "alignment": 1},
        "heading1": {"font": "Gadugi", "size_pt": 10, "alignment": 0, "bold": True},
        "heading2": {"font": "Gadugi", "size_pt": 10, "alignment": 0, "bold": True},
    },
}


def _merge_elkolind_rules(overrides: dict[str, object] | None = None) -> dict[str, object]:
    supplied = overrides or {}
    merged: dict[str, object] = {}
    for key in set(ELKOLIND_INITIAL_RULES) | set(supplied):
        default = ELKOLIND_INITIAL_RULES.get(key)
        override = supplied.get(key)
        if isinstance(default, dict) and isinstance(override, dict):
            if key == "style_overrides":
                merged[key] = {
                    role: {**(default.get(role, {}) if isinstance(default.get(role), dict) else {}),
                           **(override.get(role, {}) if isinstance(override.get(role), dict) else {})}
                    for role in set(default) | set(override)
                }
            else:
                merged[key] = {**default, **override}
        elif key in supplied:
            merged[key] = override
        else:
            merged[key] = default
    return merged


def validate_article_rules(raw: dict[str, object]) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) - RULE_FIELDS:
        raise ArticleTemplateError("Article rules must be a JSON object using supported rule names only.")
    if len(json.dumps(raw, ensure_ascii=False)) > 16000:
        raise ArticleTemplateError("Article rules are too large.")
    rules: dict[str, object] = {}
    for key, value in raw.items():
        if value is None or value == "":
            continue
        if key in {"title_max_words", "abstract_min_words", "abstract_max_words", "keywords_min", "keywords_max"}:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
                raise ArticleTemplateError(f"{key} must be a non-negative integer.")
        elif key in {"body_font_size", "title_size_pt", "abstract_size_pt", "body_size_pt"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 6 <= value <= 36:
                raise ArticleTemplateError(f"{key} must be between 6 and 36 points.")
        elif key in {"top_margin_mm", "bottom_margin_mm", "left_margin_mm", "right_margin_mm"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ArticleTemplateError(f"{key} must be between 0 and 100 mm.")
        elif key in FORMAT_OBJECT_FIELDS:
            if not isinstance(value, dict) or set(value) - FORMAT_OBJECT_FIELDS[key]:
                raise ArticleTemplateError(f"{key} has unsupported fields.")
            for name, setting in value.items():
                if isinstance(setting, (dict, list)) or setting is None or (isinstance(setting, str) and len(setting) > 100):
                    raise ArticleTemplateError(f"Invalid {key}.{name} value.")
                if name == "size_pt" and (isinstance(setting, bool) or not isinstance(setting, (int, float)) or not 6 <= setting <= 36):
                    raise ArticleTemplateError(f"{key}.size_pt must be 6–36 points.")
                if name.endswith("alignment") and setting not in {"left", "center", "right", "justify"}:
                    raise ArticleTemplateError(f"Invalid {key}.{name} alignment.")
                if name.startswith("preserve_") and not isinstance(setting, bool):
                    raise ArticleTemplateError(f"{key}.{name} must be true or false.")
        elif key == "require_lr_margin_confirmation":
            if not isinstance(value, bool):
                raise ArticleTemplateError("Margin confirmation must be true or false.")
        elif key in {"paper", "layout", "title_alignment", "title_case", "header_footer_mode", "formatting_profile"}:
            choices = {"paper": {"A4", "LETTER"}, "layout": {"single_column", "two_columns"},
                "title_alignment": {"left", "center", "right", "justify"},
                "title_case": {"sentence_case", "title_case", "upper", "as_is"},
                "header_footer_mode": {"MASTER", "PRESERVE_MANUSCRIPT"},
                "formatting_profile": {"ELKOLIND"}}[key]
            if value not in choices:
                raise ArticleTemplateError(f"Unsupported {key} value.")
        elif key == "required_sections":
            if not isinstance(value, list) or len(value) > 30 or any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in value):
                raise ArticleTemplateError("required_sections must be a list of at most 30 section names.")
            value = [item.strip() for item in value]
        elif key == "style_overrides":
            if not isinstance(value, dict) or set(value) - {"title", "body", "heading1", "heading2", "caption"}:
                raise ArticleTemplateError("style_overrides must use supported article roles.")
            allowed = {"font", "size_pt", "alignment", "bold", "space_before_pt", "space_after_pt",
                       "first_line_indent_pt", "line_spacing_multiplier"}
            for role, spec in value.items():
                if not isinstance(spec, dict) or set(spec) - allowed:
                    raise ArticleTemplateError(f"Invalid style overrides for {role}.")
                for name, setting in spec.items():
                    if name == "font":
                        if not isinstance(setting, str) or not setting.strip() or len(setting) > 100:
                            raise ArticleTemplateError("Override font must be a short name.")
                    elif name == "bold":
                        if not isinstance(setting, bool):
                            raise ArticleTemplateError("Override bold must be true or false.")
                    elif isinstance(setting, bool) or not isinstance(setting, (int, float)):
                        raise ArticleTemplateError(f"{name} must be numeric.")
                    elif name == "alignment" and setting not in {0, 1, 2, 3}:
                        raise ArticleTemplateError("Alignment must be 0=left, 1=center, 2=right, or 3=justify.")
                    elif name == "size_pt" and not 6 <= setting <= 36:
                        raise ArticleTemplateError("Override font size must be 6–36 pt.")
                    elif name == "line_spacing_multiplier" and not 0.8 <= setting <= 3:
                        raise ArticleTemplateError("Line-spacing multiplier must be 0.8–3.")
                    elif name.endswith("_pt") and not -72 <= setting <= 144:
                        raise ArticleTemplateError("Override spacing/indent must be within safe limits.")
        elif not isinstance(value, str) or len(value) > 150:
            raise ArticleTemplateError(f"{key} must be a short string.")
        rules[key] = value
    if rules.get("abstract_min_words", 0) > rules.get("abstract_max_words", 10000):
        raise ArticleTemplateError("Abstract minimum exceeds maximum.")
    if rules.get("keywords_min", 0) > rules.get("keywords_max", 10000):
        raise ArticleTemplateError("Keyword minimum exceeds maximum.")
    return rules


def active_article_template(session: Session, journal_id: uuid.UUID) -> DocumentTemplate | None:
    return session.scalar(select(DocumentTemplate).where(DocumentTemplate.journal_id == journal_id,
        DocumentTemplate.template_type == "ARTICLE_TEMPLATE", DocumentTemplate.status == TemplateStatus.ACTIVE))


def article_template_versions(session: Session, journal_id: uuid.UUID) -> list[DocumentTemplate]:
    return list(session.scalars(select(DocumentTemplate).where(DocumentTemplate.journal_id == journal_id,
        DocumentTemplate.template_type == "ARTICLE_TEMPLATE").order_by(DocumentTemplate.version.desc())))


def article_template_config(template: DocumentTemplate) -> dict[str, object]:
    data = json.loads(template.field_mapping or "{}")
    if not isinstance(data, dict) or not isinstance(data.get("profile"), dict):
        raise ArticleTemplateError("Article template style profile is missing; upload a new version.")
    if template.journal.abbreviation.upper() == "ELKOLIND":
        data["rules"] = _merge_elkolind_rules(data.get("rules", {}))
        data["rules"]["header_footer_mode"] = "MASTER"
        # Older template versions saved before the 20 mm decision have no margins; fill them in.
        for edge in ("left_margin_mm", "right_margin_mm"):
            if data["rules"].get(edge) is None:
                data["rules"][edge] = 20
        data["rules"]["require_lr_margin_confirmation"] = False
    return data


def upload_article_template(session: Session, journal: Journal, user: User, *, filename: str, content: bytes,
                            rules: dict[str, object] | None = None, activate: bool = True,
                            storage: RevisionStorage | None = None) -> DocumentTemplate:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    validate_revision_upload(filename, content, manuscript=True)
    initial = ELKOLIND_INITIAL_RULES if journal.abbreviation.upper() == "ELKOLIND" else {}
    submitted = _merge_elkolind_rules(rules) if initial else (rules or {})
    if initial and submitted.get("header_footer_mode") != "MASTER":
        raise ArticleTemplateError("ELKOLIND requires the official master header/footer.")
    validated_rules = validate_article_rules(submitted)
    profile = extract_style_profile(content)
    with ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        profile["has_header"] = any(name.startswith("word/header") and name.endswith(".xml") for name in names)
        profile["has_footer"] = any(name.startswith("word/footer") and name.endswith(".xml") for name in names)
    version = (session.scalar(select(func.max(DocumentTemplate.version)).where(
        DocumentTemplate.journal_id == journal.id, DocumentTemplate.template_type == "ARTICLE_TEMPLATE")) or 0) + 1
    current = active_article_template(session, journal.id)
    if activate and current:
        current.status, current.active_key = TemplateStatus.INACTIVE, None
        session.flush()
    template = DocumentTemplate(id=uuid.uuid4(), journal_id=journal.id, template_type="ARTICLE_TEMPLATE",
        original_filename=safe_filename(filename), storage_path="pending", version=version,
        status=TemplateStatus.ACTIVE if activate else TemplateStatus.INACTIVE,
        active_key=f"{journal.id}:ARTICLE_TEMPLATE" if activate else None, uploaded_by=user.id,
        checksum=hashlib.sha256(content).hexdigest(),
        field_mapping=json.dumps({"profile": profile, "rules": validated_rules}, ensure_ascii=False))
    session.add(template)
    session.flush()
    key, digest = (storage or get_revision_storage()).put(template.id, "article_template", content, ".docx")
    if digest != template.checksum:
        raise ArticleTemplateError("Private storage checksum did not match uploaded template.")
    template.storage_path = key
    log_audit(session, action="ARTICLE_TEMPLATE_UPLOADED", object_type="document_template", object_id=template.id,
        user_id=user.id, journal_id=journal.id, new={"version": version, "sha256": digest, "active": activate})
    session.commit()
    return template


def activate_article_template(session: Session, journal: Journal, user: User, template_id: uuid.UUID) -> DocumentTemplate:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    template = session.scalar(select(DocumentTemplate).where(DocumentTemplate.id == template_id,
        DocumentTemplate.journal_id == journal.id, DocumentTemplate.template_type == "ARTICLE_TEMPLATE"))
    if template is None:
        raise AuthorizationError("Article template is not available for the active journal.")
    current = active_article_template(session, journal.id)
    if current and current.id == template.id:
        return template
    if current:
        current.status, current.active_key = TemplateStatus.INACTIVE, None
        session.flush()
    template.status, template.active_key = TemplateStatus.ACTIVE, f"{journal.id}:ARTICLE_TEMPLATE"
    log_audit(session, action="ARTICLE_TEMPLATE_ACTIVATED", object_type="document_template", object_id=template.id,
        user_id=user.id, journal_id=journal.id, previous={"version": current.version} if current else None,
        new={"version": template.version})
    session.commit()
    return template


def update_article_rules(session: Session, journal: Journal, user: User, template_id: uuid.UUID,
                         rules: dict[str, object], *, storage: RevisionStorage | None = None) -> DocumentTemplate:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    template = session.scalar(select(DocumentTemplate).where(DocumentTemplate.id == template_id,
        DocumentTemplate.journal_id == journal.id, DocumentTemplate.template_type == "ARTICLE_TEMPLATE"))
    if template is None:
        raise AuthorizationError("Article template is not available for the active journal.")
    before = article_template_config(template).get("rules", {})
    validated = validate_article_rules(rules)
    _, content = authorized_article_template_bytes(session, journal, user, template.id, storage=storage)
    successor = upload_article_template(session, journal, user, filename=template.original_filename,
        content=content, rules=validated, activate=template.status == TemplateStatus.ACTIVE, storage=storage)
    log_audit(session, action="ARTICLE_TEMPLATE_RULES_CHANGED", object_type="document_template", object_id=successor.id,
        user_id=user.id, journal_id=journal.id,
        previous={"template_id": str(template.id), "version": template.version, "rules": before},
        new={"template_id": str(successor.id), "version": successor.version, "rules": validated})
    session.commit()
    return successor


def authorized_article_template_bytes(session: Session, journal: Journal, user: User, template_id: uuid.UUID,
                                      *, storage: RevisionStorage | None = None) -> tuple[DocumentTemplate, bytes]:
    assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    template = session.scalar(select(DocumentTemplate).where(DocumentTemplate.id == template_id,
        DocumentTemplate.journal_id == journal.id, DocumentTemplate.template_type == "ARTICLE_TEMPLATE"))
    if template is None:
        raise AuthorizationError("Article template is not available for the active journal.")
    content = (storage or get_revision_storage()).read(template.storage_path)
    if hashlib.sha256(content).hexdigest() != template.checksum:
        raise ArticleTemplateError("Article template checksum mismatch; operation stopped.")
    return template, content
