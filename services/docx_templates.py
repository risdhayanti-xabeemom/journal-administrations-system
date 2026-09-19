from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from lxml import etree
import qrcode

from config import BASE_DIR, settings


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": WORD_NS}
PLACEHOLDER_RE = re.compile(r"\{\{[a-z][a-z0-9_]*\}\}")

REQUIRED_FIELDS = {
    "ELKOLIND": {
        "{{loa_number}}", "{{recipient_name}}", "{{ojs_submission_id}}", "{{authors}}",
        "{{article_title}}", "{{volume}}", "{{issue}}", "{{publication_month}}",
        "{{publication_year}}", "{{loa_date}}",
    },
    "JASENS": {
        "{{loa_number}}", "{{recipient_name}}", "{{recipient_affiliation}}",
        "{{article_title}}", "{{authors}}", "{{volume}}", "{{issue}}",
        "{{publication_month}}", "{{publication_year}}",
    },
}

INDONESIAN_MONTHS = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)


class TemplateError(ValueError):
    pass


class PDFConverterUnavailable(RuntimeError):
    def __init__(self, message: str, *, docx_path: str | Path | None = None):
        super().__init__(message)
        self.docx_path = Path(docx_path) if docx_path else None


class PDFConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedLoA:
    docx_path: Path
    pdf_path: Path | None
    page_count: int | None
    document_hash: str

    @property
    def overflow(self) -> bool:
        return bool(self.page_count and self.page_count > 1)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_docx_bytes(content: bytes) -> None:
    if not content.startswith(b"PK"):
        raise TemplateError("The uploaded file is not a valid DOCX package.")
    try:
        with ZipFile(__import__("io").BytesIO(content)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise TemplateError("The uploaded file is not a Word DOCX document.")
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise TemplateError("Macro-enabled documents are not accepted as LoA templates.")
    except BadZipFile as exc:
        raise TemplateError("The uploaded DOCX package is damaged.") from exc


def _story_xml_parts(archive: ZipFile) -> list[str]:
    names = archive.namelist()
    return [
        name for name in names
        if name == "word/document.xml"
        or re.fullmatch(r"word/(header|footer)\d+\.xml", name)
        or re.fullmatch(r"word/(footnotes|endnotes|comments)\.xml", name)
    ]


def _replace_text_in_root(root: etree._Element, replacements: dict[str, str]) -> int:
    count = 0
    paragraphs = root.xpath(".//w:p", namespaces=NS)
    for paragraph in paragraphs:
        for old, new in replacements.items():
            if not old:
                continue
            while True:
                nodes = paragraph.xpath(".//w:t", namespaces=NS)
                values = [node.text or "" for node in nodes]
                combined = "".join(values)
                start = combined.find(old)
                if start < 0:
                    break
                end = start + len(old)
                cursor = 0
                overlaps: list[tuple[etree._Element, int, int]] = []
                for node, value in zip(nodes, values):
                    node_start, node_end = cursor, cursor + len(value)
                    if node_end > start and node_start < end:
                        overlaps.append((node, node_start, node_end))
                    cursor = node_end
                if not overlaps:
                    break
                first, first_start, _ = overlaps[0]
                last, last_start, _ = overlaps[-1]
                first_value = first.text or ""
                last_value = last.text or ""
                prefix = first_value[: max(0, start - first_start)]
                suffix = last_value[max(0, end - last_start):]
                first.text = prefix + new + (suffix if first is last else "")
                for node, _, _ in overlaps[1:-1]:
                    node.text = ""
                if last is not first:
                    last.text = suffix
                count += 1
    return count


def replace_docx_text(
    source: str | Path,
    destination: str | Path,
    replacements: dict[str, object],
    *,
    require_all: bool = True,
) -> dict[str, int]:
    """Replace paragraph text without rebuilding the Word package or its visual objects."""
    source, destination = Path(source), Path(destination)
    clean = {key: "" if value is None else str(value) for key, value in replacements.items()}
    counts = {key: 0 for key in clean}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with ZipFile(source, "r") as source_zip, ZipFile(temporary, "w", ZIP_DEFLATED) as output_zip:
        story_parts = set(_story_xml_parts(source_zip))
        for info in source_zip.infolist():
            payload = source_zip.read(info.filename)
            if info.filename in story_parts:
                parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
                root = etree.fromstring(payload, parser)
                part_matches = 0
                for key, value in clean.items():
                    matched = _replace_text_in_root(root, {key: value})
                    counts[key] += matched
                    part_matches += matched
                if part_matches:
                    payload = etree.tostring(
                        root,
                        xml_declaration=True,
                        encoding="UTF-8",
                        standalone=True,
                    )
            output_zip.writestr(info, payload)
    if require_all:
        missing = [key for key, matches in counts.items() if matches == 0]
        if missing:
            temporary.unlink(missing_ok=True)
            raise TemplateError(f"Template fields not found: {', '.join(missing)}")
    os.replace(temporary, destination)
    return counts


def extract_placeholders(path: str | Path) -> set[str]:
    found: set[str] = set()
    with ZipFile(path) as archive:
        for name in _story_xml_parts(archive):
            root = etree.fromstring(archive.read(name))
            for paragraph in root.xpath(".//w:p", namespaces=NS):
                text = "".join(node.text or "" for node in paragraph.xpath(".//w:t", namespaces=NS))
                found.update(PLACEHOLDER_RE.findall(text))
    return found


def _qr_drawing_xml(relationship_id: str, size_emu: int, placement: str, x_emu: int = 0, y_emu: int = 0) -> etree._Element:
    namespaces = {
        "w": WORD_NS,
        "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    if placement == "template-placeholder":
        frame = f"""
        <wp:inline distT="0" distB="0" distL="0" distR="0">
          <wp:extent cx="{size_emu}" cy="{size_emu}"/>
          <wp:docPr id="9901" name="LoA verification QR"/>
          <wp:cNvGraphicFramePr/>
          <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="verification-qr.png"/><pic:cNvPicPr/></pic:nvPicPr>
            <pic:blipFill><a:blip r:embed="{relationship_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
            <pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{size_emu}" cy="{size_emu}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
            </pic:pic>
          </a:graphicData></a:graphic>
        </wp:inline>"""
    else:
        frame = f"""
        <wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="251700000" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">
          <wp:simplePos x="0" y="0"/>
          <wp:positionH relativeFrom="page"><wp:posOffset>{x_emu}</wp:posOffset></wp:positionH>
          <wp:positionV relativeFrom="page"><wp:posOffset>{y_emu}</wp:posOffset></wp:positionV>
          <wp:extent cx="{size_emu}" cy="{size_emu}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>
          <wp:docPr id="9901" name="LoA verification QR"/><wp:cNvGraphicFramePr/>
          <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="verification-qr.png"/><pic:cNvPicPr/></pic:nvPicPr>
            <pic:blipFill><a:blip r:embed="{relationship_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
            <pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{size_emu}" cy="{size_emu}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
            </pic:pic>
          </a:graphicData></a:graphic>
        </wp:anchor>"""
    return etree.fromstring(f'<w:drawing xmlns:w="{namespaces["w"]}" xmlns:wp="{namespaces["wp"]}" xmlns:a="{namespaces["a"]}" xmlns:pic="{namespaces["pic"]}" xmlns:r="{namespaces["r"]}">{frame}</w:drawing>')


def inject_verification_qr(docx_path: str | Path, url: str, *, size_mm: int, placement: str) -> None:
    path = Path(docx_path)
    if size_mm < 10 or size_mm > 40:
        raise TemplateError("LoA QR size must be between 10 and 40 mm.")
    image = qrcode.make(url)
    image_buffer = io.BytesIO()
    image.save(image_buffer, format="PNG")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.qr.tmp")
    with ZipFile(path, "r") as source_zip:
        document_root = etree.fromstring(source_zip.read("word/document.xml"))
        relationships_name = "word/_rels/document.xml.rels"
        relationships_root = etree.fromstring(source_zip.read(relationships_name))
        relationship_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        ids = [node.get("Id", "") for node in relationships_root]
        numeric = [int(value[3:]) for value in ids if value.startswith("rId") and value[3:].isdigit()]
        relationship_id = f"rId{max(numeric, default=0) + 1}"
        etree.SubElement(relationships_root, f"{{{relationship_ns}}}Relationship", {
            "Id": relationship_id,
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
            "Target": "media/verification-qr.png",
        })
        size_emu = int(size_mm * 36000)
        page_width, page_height = 11908800, 16835760
        pg_size = document_root.find(".//w:sectPr/w:pgSz", namespaces=NS)
        if pg_size is not None:
            page_width = int(pg_size.get(f"{{{WORD_NS}}}w", "18760")) * 635
            page_height = int(pg_size.get(f"{{{WORD_NS}}}h", "26520")) * 635
        inset = int(10 * 36000)
        positions = {
            "top-left": (inset, inset),
            "top-right": (page_width - size_emu - inset, inset),
            "bottom-left": (inset, page_height - size_emu - inset),
            "bottom-right": (page_width - size_emu - inset, page_height - size_emu - inset),
        }
        if placement == "template-placeholder":
            target_run = None
            for paragraph in document_root.xpath(".//w:p", namespaces=NS):
                text_nodes = paragraph.xpath(".//w:t", namespaces=NS)
                if "{{verification_qr}}" in "".join(node.text or "" for node in text_nodes):
                    _replace_text_in_root(paragraph, {"{{verification_qr}}": ""})
                    target_run = paragraph.find(".//w:r", namespaces=NS)
                    break
            if target_run is None:
                raise TemplateError("QR placement is 'template-placeholder', but {{verification_qr}} is not present.")
            target_run.append(_qr_drawing_xml(relationship_id, size_emu, placement))
        else:
            if placement not in positions:
                raise TemplateError(f"Unsupported LoA QR placement: {placement}")
            x_emu, y_emu = positions[placement]
            paragraph = etree.Element(f"{{{WORD_NS}}}p")
            run = etree.SubElement(paragraph, f"{{{WORD_NS}}}r")
            run.append(_qr_drawing_xml(relationship_id, size_emu, placement, x_emu, y_emu))
            body = document_root.find(".//w:body", namespaces=NS)
            section = body.find("w:sectPr", namespaces=NS)
            body.insert(body.index(section) if section is not None else len(body), paragraph)
        content_types_root = etree.fromstring(source_zip.read("[Content_Types].xml"))
        ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
        if not content_types_root.xpath("./ct:Default[@Extension='png']", namespaces={"ct": ct_ns}):
            etree.SubElement(content_types_root, f"{{{ct_ns}}}Default", {"Extension": "png", "ContentType": "image/png"})
        changed = {
            "word/document.xml": etree.tostring(document_root, xml_declaration=True, encoding="UTF-8", standalone=True),
            relationships_name: etree.tostring(relationships_root, xml_declaration=True, encoding="UTF-8", standalone=True),
            "[Content_Types].xml": etree.tostring(content_types_root, xml_declaration=True, encoding="UTF-8", standalone=True),
        }
        with ZipFile(temporary, "w", ZIP_DEFLATED) as output_zip:
            for info in source_zip.infolist():
                output_zip.writestr(info, changed.get(info.filename, source_zip.read(info.filename)))
            output_zip.writestr("word/media/verification-qr.png", image_buffer.getvalue())
    os.replace(temporary, path)


def pin_anchor_vertical_to_page(docx_path: str | Path, anchor_index: int, y_emu: int) -> None:
    """Pin an existing floating object to its audited page position without recreating it."""
    path = Path(docx_path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.anchor.tmp")
    with ZipFile(path, "r") as source_zip, ZipFile(temporary, "w", ZIP_DEFLATED) as output_zip:
        root = etree.fromstring(source_zip.read("word/document.xml"))
        wp_ns = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
        anchors = root.xpath(".//wp:anchor", namespaces={"wp": wp_ns})
        try:
            anchor = anchors[anchor_index]
        except IndexError as exc:
            raise TemplateError(f"Floating object {anchor_index} was not found.") from exc
        vertical = anchor.find(f"{{{wp_ns}}}positionV")
        if vertical is None:
            raise TemplateError(f"Floating object {anchor_index} has no vertical position.")
        vertical.set("relativeFrom", "page")
        position = vertical.find(f"{{{wp_ns}}}posOffset")
        if position is None:
            for child in list(vertical):
                vertical.remove(child)
            position = etree.SubElement(vertical, f"{{{wp_ns}}}posOffset")
        position.text = str(y_emu)
        document_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        for info in source_zip.infolist():
            output_zip.writestr(info, document_xml if info.filename == "word/document.xml" else source_zip.read(info.filename))
    os.replace(temporary, path)


def validate_required_fields(path: str | Path, journal_abbreviation: str) -> set[str]:
    placeholders = extract_placeholders(path)
    required = REQUIRED_FIELDS.get(journal_abbreviation.upper(), set())
    missing = sorted(required - placeholders)
    if missing:
        raise TemplateError(f"Missing required placeholders: {', '.join(missing)}")
    return placeholders


def indonesian_date(value: date) -> str:
    return f"{value.day} {INDONESIAN_MONTHS[value.month - 1]} {value.year}"


def _page_count(pdf_path: Path) -> int:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PDFConversionError("pypdf is required to inspect generated PDF pages.") from exc
    return len(PdfReader(str(pdf_path)).pages)


def _convert_with_libreoffice(input_docx: Path, output_pdf: Path) -> None:
    executable = settings.libreoffice_path or shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        raise PDFConverterUnavailable(
            "LibreOffice is not configured. Set LIBREOFFICE_PATH or choose the Microsoft Word converter on Windows."
        )
    with tempfile.TemporaryDirectory(prefix="jas-lo-") as temp_dir:
        profile = Path(temp_dir, "profile").as_uri()
        command = [
            str(executable), f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf",
            "--outdir", temp_dir, str(input_docx),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        produced = Path(temp_dir, f"{input_docx.stem}.pdf")
        if completed.returncode or not produced.is_file():
            detail = (completed.stderr or completed.stdout or "unknown conversion error").strip()
            raise PDFConversionError(f"LibreOffice could not convert the DOCX: {detail}")
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(produced, output_pdf)


def _convert_with_word(input_docx: Path, output_pdf: Path) -> None:
    if os.name != "nt":
        raise PDFConverterUnavailable("The Microsoft Word converter is available only on Windows.")
    script = BASE_DIR / "scripts" / "convert_docx_to_pdf.ps1"
    completed = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-InputDocx", str(input_docx.resolve()), "-OutputPdf", str(output_pdf.resolve()),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode or not output_pdf.is_file():
        detail = (completed.stderr or completed.stdout or "Microsoft Word is unavailable").strip()
        raise PDFConversionError(f"Microsoft Word could not convert the DOCX: {detail}")


def convert_docx_to_pdf(input_docx: str | Path, output_pdf: str | Path) -> int:
    input_docx, output_pdf = Path(input_docx), Path(output_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    converter = settings.docx_pdf_converter
    errors: list[str] = []
    candidates = [converter] if converter != "auto" else (["word", "libreoffice"] if os.name == "nt" else ["libreoffice"])
    for candidate in candidates:
        try:
            if candidate == "word":
                _convert_with_word(input_docx, output_pdf)
            elif candidate == "libreoffice":
                _convert_with_libreoffice(input_docx, output_pdf)
            elif candidate == "none":
                raise PDFConverterUnavailable("DOCX-to-PDF conversion is disabled by DOCX_PDF_CONVERTER=none.")
            else:
                raise PDFConverterUnavailable(f"Unknown DOCX_PDF_CONVERTER value: {candidate}")
            return _page_count(output_pdf)
        except (PDFConverterUnavailable, PDFConversionError) as exc:
            errors.append(str(exc))
    raise PDFConverterUnavailable(" No fallback converter succeeded. ".join(errors))


def generate_from_template(
    template_path: str | Path,
    output_docx: str | Path,
    values: dict[str, object],
    *,
    output_pdf: str | Path | None = None,
    qr_url: str | None = None,
    qr_size_mm: int = 20,
    qr_placement: str = "bottom-right",
) -> GeneratedLoA:
    replacements = {
        f"{{{{{key}}}}}": value
        for key, value in values.items()
        if not (key == "verification_qr" and qr_url and qr_placement == "template-placeholder")
    }
    replace_docx_text(template_path, output_docx, replacements, require_all=False)
    if qr_url:
        inject_verification_qr(output_docx, qr_url, size_mm=qr_size_mm, placement=qr_placement)
    remaining = extract_placeholders(output_docx)
    unresolved = sorted(item for item in remaining if item not in {"{{verification_qr}}"})
    if unresolved:
        raise TemplateError(f"Unresolved template placeholders: {', '.join(unresolved)}")
    pdf_path = Path(output_pdf) if output_pdf else None
    try:
        page_count = convert_docx_to_pdf(output_docx, pdf_path) if pdf_path else None
    except (PDFConverterUnavailable, PDFConversionError) as exc:
        raise PDFConverterUnavailable(
            f"{exc} The generated DOCX was retained at {Path(output_docx)}.",
            docx_path=output_docx,
        ) from exc
    return GeneratedLoA(Path(output_docx), pdf_path, page_count, sha256_file(output_docx))


def escaped_filename(value: str) -> str:
    value = html.unescape(value)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.") or "document"
