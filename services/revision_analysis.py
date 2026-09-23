"""Read-only manuscript and reviewer analysis for controlled revisions."""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass
from zipfile import ZipFile

from lxml import etree
from pypdf import PdfReader


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS = {"w": W, "m": M}
NUMERIC_RE = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)?\s*(?:%|ms|s|Hz|kHz|V|A|W|°C|kg|g|mm|cm|m)?(?![\w])", re.I)
CITATION_RE = re.compile(r"\[(?:\d+)(?:\s*[-,]\s*\d+)*\]")
AUTHOR_YEAR_RE = re.compile(r"\b[A-Z][A-Za-z-]+(?:\s+et al\.)?\s*\(\d{4}[a-z]?\)|\([A-Z][A-Za-z-]+(?:\s+et al\.)?(?:\s*(?:&|and|,)\s*[A-Z][A-Za-z-]+)*,?\s+\d{4}[a-z]?\)")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s,;)>\]]+", re.I)
FIGURE_CAPTION_RE = re.compile(
    r"^\s*(?:gambar\.?|figure|fig\.?)\s+(\d+)\s*[:.\-]\s*(\S.*)\s*$",
    re.I,
)
TABLE_CAPTION_RE = re.compile(
    r"^\s*(?:tabel|table)\s+([IVXLCDM]+|\d+)\s*[:.\-]\s*(\S.*)\s*$",
    re.I,
)


@dataclass(frozen=True)
class ParagraphInfo:
    identifier: str
    text: str
    section: str
    section_id: str
    kind: str
    patchable: bool
    index: int


@dataclass(frozen=True)
class ManuscriptStructure:
    title: str
    authors: tuple[str, ...]
    affiliations: tuple[str, ...]
    abstract: str
    keywords: str
    paragraphs: tuple[ParagraphInfo, ...]
    table_count: int
    figure_count: int
    equation_count: int
    table_captions: tuple[str, ...]
    figure_captions: tuple[str, ...]
    references: tuple[str, ...]


@dataclass(frozen=True)
class ExtractedComment:
    reviewer: str
    number: int
    raw_text: str
    category: str
    severity: str
    suggested_section: str | None
    author_input_required: bool


@dataclass(frozen=True)
class LocationMatch:
    paragraph_id: str | None
    section: str | None
    original_text: str | None
    confidence: int


def _xml(content: bytes) -> etree._Element:
    return etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))


def _paragraph_text(element: etree._Element) -> str:
    parts = []
    for node in element.xpath(".//w:t|.//w:tab|.//w:br", namespaces=NS):
        if node.tag == f"{{{W}}}t":
            parts.append(node.text or "")
        elif node.tag == f"{{{W}}}tab":
            parts.append("\t")
        else:
            parts.append("\n")
    # Keep the exact character offsets used by OOXML run patching.
    return "".join(parts)


def _kind(text: str, style: str, in_table: bool) -> str:
    lower = text.lower().strip()
    if in_table:
        return "TABLE_CELL"
    if TABLE_CAPTION_RE.fullmatch(text):
        return "TABLE_CAPTION"
    if FIGURE_CAPTION_RE.fullmatch(text):
        return "FIGURE_CAPTION"
    if lower in {"abstract", "abstrak"}:
        return "ABSTRACT_HEADING"
    if lower.startswith(("keywords:", "kata kunci:")):
        return "KEYWORDS"
    if style.lower().startswith("heading") or re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Za-z]", text):
        return "SECTION_HEADING"
    return "PARAGRAPH"


def analyze_manuscript(content: bytes) -> ManuscriptStructure:
    with ZipFile(io.BytesIO(content)) as archive:
        root = _xml(archive.read("word/document.xml"))
    body = root.find(f"{{{W}}}body")
    if body is None:
        raise ValueError("Manuscript DOCX has no body.")
    paragraphs: list[ParagraphInfo] = []
    heading_numbers = [0, 0, 0, 0]
    current_section, current_section_id = "Front matter", "FRONT_MATTER"
    title = ""
    authors: list[str] = []
    affiliations: list[str] = []
    abstract_lines: list[str] = []
    keywords = ""
    in_abstract = False
    references: list[str] = []
    in_references = False
    table_captions: list[str] = []
    figure_captions: list[str] = []
    for index, para in enumerate(body.xpath(".//w:p", namespaces=NS), 1):
        text = _paragraph_text(para)
        style_values = para.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
        style = style_values[0] if style_values else ""
        in_table = any(ancestor.tag == f"{{{W}}}tbl" for ancestor in para.iterancestors())
        kind = _kind(text, style, in_table)
        heading_text = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", text.strip()).lower()
        if not title and text.strip() and (style.lower() == "title" or not paragraphs):
            title = text.strip()
        if kind == "SECTION_HEADING" or heading_text in {"references", "bibliography", "daftar pustaka"}:
            heading_level = 1
            matched = re.match(r"Heading(\d+)", style, re.I)
            if matched:
                heading_level = min(int(matched.group(1)), 4)
            else:
                number = re.match(r"^(\d+(?:\.\d+)*)", text)
                if number:
                    heading_level = min(number.group(1).count(".") + 1, 4)
            heading_numbers[heading_level - 1] += 1
            heading_numbers[heading_level:] = [0] * (4 - heading_level)
            current_section = text
            current_section_id = "SECTION_" + "_".join(str(x) for x in heading_numbers[:heading_level])
            in_abstract = False
            in_references = heading_text in {"references", "bibliography", "daftar pustaka"}
        elif kind == "ABSTRACT_HEADING":
            in_abstract = True
            in_references = False
            current_section, current_section_id = "Abstract", "ABSTRACT"
        elif kind == "KEYWORDS":
            keywords = text
            in_abstract = False
        elif text and in_abstract:
            abstract_lines.append(text)
        elif text and in_references:
            references.append(text)
        elif text and current_section_id == "FRONT_MATTER" and text != title:
            (affiliations if re.search(r"university|institute|department|faculty|laboratory|universitas", text, re.I) else authors).append(text)
        if kind == "TABLE_CAPTION":
            table_captions.append(text)
        elif kind == "FIGURE_CAPTION":
            figure_captions.append(text)
        unsafe = bool(para.xpath(".//m:oMath|.//m:oMathPara|.//w:drawing|.//w:pict|.//w:fldChar|.//w:instrText|.//w:footnoteReference|.//w:endnoteReference|.//w:hyperlink|.//w:tab|.//w:br|.//w:commentRangeStart|.//w:commentRangeEnd", namespaces=NS))
        patchable = bool(text.strip()) and kind in {"PARAGRAPH", "TABLE_CAPTION", "FIGURE_CAPTION"} and not in_table and not in_references and not unsafe
        paragraphs.append(ParagraphInfo(f"PARAGRAPH_{index:03d}", text, current_section, current_section_id, kind, patchable, index))
    figures = len(root.xpath(".//w:drawing|.//w:pict", namespaces=NS))
    equations = len(root.xpath(".//m:oMath|.//m:oMathPara", namespaces=NS))
    return ManuscriptStructure(title, tuple(authors), tuple(affiliations), "\n".join(abstract_lines), keywords,
                               tuple(paragraphs), len(root.xpath(".//w:tbl", namespaces=NS)), figures, equations,
                               tuple(table_captions), tuple(figure_captions), tuple(references))


def reviewer_text(filename: str, content: bytes) -> str:
    suffix = filename.rsplit(".", 1)[-1].lower()
    if suffix == "txt":
        text = content.decode("utf-8-sig")
        if len(text) > 1_000_000:
            raise ValueError("Reviewer text exceeds the one-million-character analysis limit.")
        return text
    if suffix == "pdf":
        text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
        if len(text) > 1_000_000:
            raise ValueError("Reviewer PDF text exceeds the one-million-character analysis limit.")
        return text
    if suffix == "docx":
        with ZipFile(io.BytesIO(content)) as archive:
            root = _xml(archive.read("word/document.xml"))
            lines = [_paragraph_text(para) for para in root.xpath(".//w:body//w:p", namespaces=NS)]
            if "word/comments.xml" in archive.namelist():
                comments = _xml(archive.read("word/comments.xml"))
                lines.extend(_paragraph_text(para) for para in comments.xpath(".//w:comment//w:p", namespaces=NS))
        text = "\n".join(lines)
        if len(text) > 1_000_000:
            raise ValueError("Reviewer DOCX text exceeds the one-million-character analysis limit.")
        return text
    raise ValueError("Unsupported reviewer document.")


def classify_comment(text: str, context_severity: str = "MINOR") -> tuple[str, str, str | None, bool]:
    lower = text.lower()
    categories = (
        ("DATA_CHANGE", r"new experiment|additional experiment|re[- ]?run|new data|measurements?|sample size|statistical|accuracy|performance metric|change.*\d|correct.*\d"),
        ("EQUATION", r"equation|formula|derivation|mathematical"),
        ("FIGURE", r"figure|fig\.|image|chart|resolution"),
        ("TABLE", r"table|tabel"),
        ("REFERENCE", r"reference|citation|cited|bibliography|doi"),
        ("ABSTRACT", r"abstract|abstrak"),
        ("NOVELTY", r"novelty|contribution|originality"),
        ("INTRODUCTION", r"introduction|background"),
        ("LITERATURE_REVIEW", r"literature|related work|prior work"),
        ("METHODOLOGY", r"method|procedure|algorithm"),
        ("EXPERIMENT", r"experiment|simulation"),
        ("RESULTS", r"results?|findings"),
        ("DISCUSSION", r"discussion|interpretation"),
        ("CONCLUSION", r"conclusion"),
        ("LANGUAGE", r"grammar|clarity|wording|language|typo|spelling"),
        ("FORMAT", r"format|layout|template|font|margin"),
    )
    category = next((name for name, pattern in categories if re.search(pattern, lower)), "OTHER")
    severity = "MAJOR" if re.search(r"major|critical|essential|fundamental", lower) else context_severity
    if re.search(r"typo|spelling|punctuation|minor", lower):
        severity = "EDITORIAL"
    section = {
        "ABSTRACT": "Abstract", "NOVELTY": "Introduction", "INTRODUCTION": "Introduction",
        "LITERATURE_REVIEW": "Literature Review", "METHODOLOGY": "Methodology",
        "EXPERIMENT": "Experiment", "RESULTS": "Results", "DISCUSSION": "Discussion",
        "CONCLUSION": "Conclusion", "REFERENCE": "References",
    }.get(category)
    author_input = category in {"DATA_CHANGE", "REFERENCE", "EQUATION"} or bool(re.search(r"new figure|replace.*figure|higher.resolution|new table|additional chart", lower))
    return category, severity, section, author_input


def extract_reviewer_comments(filename: str, content: bytes, reviewer: str) -> list[ExtractedComment]:
    text = reviewer_text(filename, content)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    records: list[ExtractedComment] = []
    severity = "MINOR"
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        raw = "\n".join(current).strip()
        if raw:
            category, identified_severity, section, author_input = classify_comment(raw, severity)
            records.append(ExtractedComment(reviewer, len(records) + 1, raw, category, identified_severity, section, author_input))
        current.clear()

    for line in lines:
        heading = re.fullmatch(r"(?:major|minor|general|specific)\s+comments?\s*:?", line, re.I)
        if heading:
            flush()
            severity = "MAJOR" if line.lower().startswith("major") else "MINOR"
            continue
        match = re.match(r"^(?:comment\s*)?(?:\d+[.)]|[-•])\s*(.+)$", line, re.I)
        if match:
            flush()
            current.append(match.group(1).strip())
        elif current:
            current.append(line)
        else:
            current.append(line)
    flush()
    return records


def map_comment(comment: ExtractedComment, structure: ManuscriptStructure) -> LocationMatch:
    words = {word for word in re.findall(r"[a-z]{4,}", comment.raw_text.lower()) if word not in {"please", "should", "manuscript", "reviewer", "article", "clarify", "explain", "provide", "authors"}}
    candidates: list[tuple[int, ParagraphInfo]] = []
    for paragraph in structure.paragraphs:
        if not paragraph.patchable:
            continue
        section_match = bool(comment.suggested_section and comment.suggested_section.lower() in paragraph.section.lower())
        paragraph_words = set(re.findall(r"[a-z]{4,}", paragraph.text.lower()))
        overlap = len(words & paragraph_words)
        score = (35 if section_match else 0) + min(50, overlap * 17)
        if comment.category == "NOVELTY" and "novel" in paragraph.text.lower():
            score += 20
        candidates.append((score, paragraph))
    if not candidates:
        return LocationMatch(None, comment.suggested_section, None, 0)
    score, target = max(candidates, key=lambda item: item[0])
    # A section-only guess is not enough to authorize a document patch.
    return LocationMatch(target.identifier if score >= 40 else None,
                         target.section if score >= 40 else comment.suggested_section,
                         target.text if score >= 40 else None, min(score, 100))


def numeric_tokens(text: str) -> Counter[str]:
    return Counter(re.sub(r"\s+", "", token.group(0)).lower() for token in NUMERIC_RE.finditer(text))


def citation_tokens(text: str) -> Counter[str]:
    return Counter(CITATION_RE.findall(text) + AUTHOR_YEAR_RE.findall(text) + DOI_RE.findall(text))
