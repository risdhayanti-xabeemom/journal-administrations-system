"""Formatting-preserving article metadata and official header/footer grafting.

Only the uploaded article master supplies header/footer artwork and typography.
Scientific manuscript paragraphs are never reconstructed from extracted text.
"""

from __future__ import annotations

import copy
import io
import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import unquote
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

from services.revision_analysis import NS, W


R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
W_TAG = f"{{{W}}}"
R_TAG = f"{{{R}}}"
REL_TAG = f"{{{PKG_REL}}}"
CT_TAG = f"{{{CT}}}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
HYPERLINK_TYPE = f"{R}/hyperlink"
ELKOLIND_FIELDS = {
    "volume", "issue", "publication_month", "publication_year", "doi_full", "doi_suffix",
    "first_author", "short_title_4w", "received_date", "revised_date", "accepted_date",
}
TOKEN_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class ArticleMetadataError(ValueError):
    pass


def short_title_4w(title: str) -> str:
    words = re.sub(r"\s+", " ", title).strip().split(" ")
    words = [word for word in words if word]
    return " ".join(words[:4]) + ("…" if len(words) > 4 else "")


def resolve_elkolind_metadata(raw: dict[str, object], article_title: str) -> dict[str, str]:
    values = {key: str(raw.get(key) or "").strip() for key in ELKOLIND_FIELDS}
    values["short_title_4w"] = short_title_4w(article_title)
    for key in ("volume", "issue"):
        if values[key] and not re.fullmatch(r"[A-Za-z0-9-]{1,16}", values[key]):
            raise ArticleMetadataError(f"Invalid ELKOLIND {key}.")
    if values["publication_year"] and not re.fullmatch(r"\d{4}", values["publication_year"]):
        raise ArticleMetadataError("Publication year must have four digits.")
    if values["doi_suffix"] and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", values["doi_suffix"]):
        raise ArticleMetadataError("Invalid ELKOLIND DOI suffix.")
    if not values["doi_full"] and values["doi_suffix"]:
        if not values["volume"] or not values["issue"]:
            raise ArticleMetadataError("A DOI suffix needs volume and issue to build the ELKOLIND DOI URL.")
        values["doi_full"] = (
            f"http://dx.doi.org/10.33795/elkolind.v{values['volume']}"
            f"i{values['issue']}.{values['doi_suffix']}"
        )
    if values["doi_full"]:
        parsed = urlsplit(values["doi_full"])
        if parsed.scheme and (parsed.scheme not in {"http", "https"} or
                              (parsed.hostname or "").lower() not in {"doi.org", "dx.doi.org"}):
            raise ArticleMetadataError("The full DOI URL must use doi.org or dx.doi.org.")
        if not parsed.scheme and not re.fullmatch(r"10\.\d{4,9}/\S+", values["doi_full"]):
            raise ArticleMetadataError("The full DOI must be a DOI identifier or doi.org URL.")
    return values


def _xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))


def _bytes(root: etree._Element) -> bytes:
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


def _rels_path(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def _part_target(owner: str, target: str) -> str:
    if target.startswith("/"):
        result = posixpath.normpath(unquote(target).lstrip("/"))
    else:
        result = posixpath.normpath(posixpath.join(posixpath.dirname(owner), unquote(target)))
    if result.startswith("../") or result == "..":
        raise ArticleMetadataError("Article master contains an unsafe package relationship.")
    return result


def _text(root: etree._Element) -> str:
    return re.sub(r"\s+", " ", " ".join(root.xpath(".//w:t/text()", namespaces=NS))).strip()


def _xml_visible_text(root: etree._Element) -> str:
    """Return every Word text node in XML order, including textboxes/shapes.

    Header/footer text in VML and DrawingML lives below ``w:txbxContent`` and is
    not reliably exposed by python-docx's paragraph collection. Joining the raw
    ``w:t`` nodes also reconstructs placeholders that Word split across runs.
    """
    return "".join(text or "" for text in root.xpath(".//w:t/text()", namespaces=NS))


def _compact_word_xml_text(root: etree._Element) -> str:
    return re.sub(r"\s+", "", _xml_visible_text(root))


def _xml_placeholders(root: etree._Element) -> set[str]:
    text = _xml_visible_text(root)
    return {match.group(1).strip() for match in PLACEHOLDER_RE.finditer(text)}


def _has_elkolind_doi_placeholder(root: etree._Element, placeholders: set[str]) -> bool:
    return "doi_full" in placeholders or "doi_suffix" in placeholders


def _word_on_off_enabled(node: etree._Element | None) -> bool:
    """Interpret an OOXML on/off property, including an explicit false value."""
    if node is None:
        return False
    value = (node.get(W_TAG + "val") or "true").strip().lower()
    return value not in {"0", "false", "off", "no"}


def _replace_tokens(root: etree._Element, values: dict[str, str]) -> int:
    count = 0
    replacements = {"{{short_title_4w}}…": values.get("short_title_4w", "")}
    replacements.update({f"{{{{{key}}}}}": value for key, value in values.items()})
    for paragraph in root.xpath(".//w:p", namespaces=NS):
        nodes = paragraph.xpath(".//w:t", namespaces=NS)
        for token, replacement in replacements.items():
            while True:
                combined = "".join(node.text or "" for node in nodes)
                start = combined.find(token)
                if start < 0:
                    break
                if not replacement:
                    raise ArticleMetadataError(f"Provide article metadata before replacing {token}.")
                end = start + len(token)
                offsets = []
                cursor = 0
                for index, node in enumerate(nodes):
                    length = len(node.text or "")
                    offsets.append((cursor, cursor + length, index))
                    cursor += length
                first = next((item for item in offsets if item[0] <= start < item[1]), None)
                last = next((item for item in offsets if item[0] < end <= item[1]), None)
                if first is None or last is None:
                    raise ArticleMetadataError("A DOCX placeholder spans unsupported Word objects.")
                first_node, last_node = nodes[first[2]], nodes[last[2]]
                prefix = (first_node.text or "")[:start - first[0]]
                suffix = (last_node.text or "")[end - last[0]:]
                if first[2] == last[2]:
                    first_node.text = prefix + replacement + suffix
                else:
                    first_node.text = prefix + replacement
                    for index in range(first[2] + 1, last[2]):
                        nodes[index].text = ""
                    last_node.text = suffix
                for node in (first_node, last_node):
                    if (node.text or "").startswith(" ") or (node.text or "").endswith(" "):
                        node.set(XML_SPACE, "preserve")
                count += 1
        unresolved = TOKEN_RE.findall("".join(node.text or "" for node in nodes))
        if unresolved:
            raise ArticleMetadataError(
                "Missing article metadata for DOCX placeholder(s): " + ", ".join(sorted(set(unresolved)))
            )
    return count


def _doi_href(display: str) -> str | None:
    if display.startswith(("https://", "http://")):
        return display
    if re.fullmatch(r"10\.\d{4,9}/\S+", display):
        return "https://doi.org/" + display
    return None


def _link_doi(root: etree._Element, part: str, files: dict[str, bytes], display: str) -> None:
    href = _doi_href(display)
    if not href:
        return
    match = next((node for node in root.xpath(".//w:t", namespaces=NS)
                  if display in (node.text or "")), None)
    if match is None:
        return
    rel_name = _rels_path(part)
    rels = _xml(files[rel_name]) if rel_name in files else etree.Element(REL_TAG + "Relationships", nsmap={None: PKG_REL})
    existing_ids = {item.get("Id") for item in rels}
    number = 1
    while f"rId{number}" in existing_ids:
        number += 1
    rel_id = f"rId{number}"
    ancestor = next((item for item in match.iterancestors() if item.tag == W_TAG + "hyperlink"), None)
    if ancestor is not None and ancestor.get(R_TAG + "id"):
        rel_id = ancestor.get(R_TAG + "id")
        relation = next((item for item in rels if item.get("Id") == rel_id), None)
        if relation is None or relation.get("Type") != HYPERLINK_TYPE:
            raise ArticleMetadataError("DOI hyperlink relationship is invalid.")
        relation.set("Target", href)
        relation.set("TargetMode", "External")
    else:
        run = match.getparent()
        if run is None or run.tag != W_TAG + "r" or len(run.xpath("./w:t", namespaces=NS)) != 1:
            return
        parent = run.getparent()
        if parent is None:
            return
        original = match.text or ""
        before, after = original.split(display, 1)
        link_run = copy.deepcopy(run)
        link_run.find(W_TAG + "t").text = display
        hyperlink = etree.Element(W_TAG + "hyperlink")
        hyperlink.set(R_TAG + "id", rel_id)
        hyperlink.append(link_run)
        position = parent.index(run)
        if before:
            match.text = before
            if before.endswith(" "):
                match.set(XML_SPACE, "preserve")
            parent.insert(position + 1, hyperlink)
        else:
            parent.remove(run)
            parent.insert(position, hyperlink)
        if after:
            trailing = copy.deepcopy(run)
            trailing.find(W_TAG + "t").text = after
            parent.insert(parent.index(hyperlink) + 1, trailing)
        relationship = etree.SubElement(rels, REL_TAG + "Relationship")
        relationship.set("Id", rel_id)
        relationship.set("Type", HYPERLINK_TYPE)
        relationship.set("Target", href)
        relationship.set("TargetMode", "External")
    files[rel_name] = _bytes(rels)


def _has_page_field(root: etree._Element) -> bool:
    instructions = root.xpath(".//w:instrText/text()|.//w:fldSimple/@w:instr", namespaces=NS)
    return bool(re.search(r"\bPAGE\b", " ".join(instructions), re.I))


def _append_page_field(root: etree._Element) -> None:
    paragraph = etree.SubElement(root, W_TAG + "p")
    properties = etree.SubElement(paragraph, W_TAG + "pPr")
    alignment = etree.SubElement(properties, W_TAG + "jc")
    alignment.set(W_TAG + "val", "right")
    for kind, text_value in (("begin", None), (None, " PAGE "), ("separate", None),
                             ("display", "1"), ("end", None)):
        run = etree.SubElement(paragraph, W_TAG + "r")
        if kind == "display":
            etree.SubElement(run, W_TAG + "t").text = text_value
        elif text_value is not None:
            element = etree.SubElement(run, W_TAG + "instrText")
            element.set(XML_SPACE, "preserve")
            element.text = text_value
        else:
            element = etree.SubElement(run, W_TAG + "fldChar")
            element.set(W_TAG + "fldCharType", kind)


def elkolind_master_warnings(content: bytes) -> list[str]:
    """Read-only conversion checklist; the uploaded official DOCX is never edited."""
    warnings: list[str] = []
    with ZipFile(io.BytesIO(content)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    section_nodes = _xml(files["word/document.xml"]).xpath(".//w:sectPr", namespaces=NS)
    if not section_nodes or "word/_rels/document.xml.rels" not in files:
        return ["The master has no usable Word section/header relationships."]
    rels = {item.get("Id"): item for item in _xml(files["word/_rels/document.xml.rels"])}
    master_section = section_nodes[-1]
    references: dict[tuple[str, str], str] = {}
    for node in master_section:
        if node.tag not in {W_TAG + "headerReference", W_TAG + "footerReference"}:
            continue
        kind = "header" if node.tag == W_TAG + "headerReference" else "footer"
        relation = rels.get(node.get(R_TAG + "id"))
        if relation is not None:
            references[(kind, node.get(W_TAG + "type", "default"))] = _part_target(
                "word/document.xml", relation.get("Target", "")
            )
    settings = _xml(files["word/settings.xml"]) if "word/settings.xml" in files else None
    active_header_variants = ["default"]
    even_and_odd = settings.find(W_TAG + "evenAndOddHeaders") if settings is not None else None
    if _word_on_off_enabled(even_and_odd):
        active_header_variants.append("even")
    if _word_on_off_enabled(master_section.find(W_TAG + "titlePg")):
        active_header_variants.append("first")

    required_parts = [("header", variant) for variant in active_header_variants]
    required_parts.extend((("footer", "default"), ("footer", "even")))
    for kind, variant in required_parts:
        if (kind, variant) not in references:
            warnings.append(f"Official {variant} {kind} is missing.")
    required_header_fields = {"volume", "issue", "publication_month", "publication_year"}
    for variant in active_header_variants:
        part = references.get(("header", variant))
        if part and part in files:
            root = _xml(files[part])
            placeholders = _xml_placeholders(root)
            if not required_header_fields <= placeholders or not _has_elkolind_doi_placeholder(root, placeholders):
                warnings.append(f"The {variant} header needs the ELKOLIND dynamic placeholders.")
    even_part = references.get(("footer", "even"))
    if even_part and even_part in files:
        placeholders = _xml_placeholders(_xml(files[even_part]))
        if not {"first_author", "short_title_4w"} <= placeholders:
            warnings.append("The even footer needs first-author and short-title placeholders.")
    footer_parts = [part for (kind, _), part in references.items() if kind == "footer" and part in files]
    page_parts = [part for part in set(references.values()) if part in files]
    if page_parts and not any(_has_page_field(_xml(files[part])) for part in page_parts):
        warnings.append("No native PAGE field was found; JAS will add one to each needed footer variant.")
    if footer_parts and not any(_xml(files[part]).xpath(".//w:drawing|.//w:pict", namespaces=NS)
                                for part in footer_parts):
        warnings.append("No footer image was detected; verify the official barcode in visual preview.")
    return warnings


def graft_article_master_header_footer(
    manuscript: bytes, master: bytes, values: dict[str, str], *, elkolind: bool = False
) -> tuple[bytes, dict[str, object]]:
    """Copy master page variants, styles and dependent media into a manuscript copy."""
    if elkolind:
        warnings = elkolind_master_warnings(master)
        if warnings:
            raise ArticleMetadataError(
                "Active ELKOLIND Article Master failed validation: " + "; ".join(warnings)
            )
    with ZipFile(io.BytesIO(manuscript)) as source, ZipFile(io.BytesIO(master)) as template:
        original_files = {name: source.read(name) for name in source.namelist()}
        master_files = {name: template.read(name) for name in template.namelist()}
        additions: dict[str, bytes] = {}
        document = _xml(original_files["word/document.xml"])
        master_document = _xml(master_files["word/document.xml"])
        template_sections = master_document.xpath(".//w:sectPr", namespaces=NS)
        target_sections = document.xpath(".//w:sectPr", namespaces=NS)
        if not template_sections or not target_sections:
            raise ArticleMetadataError("The article master and manuscript must contain Word sections.")
        source_rels_name = "word/_rels/document.xml.rels"
        if source_rels_name not in original_files or source_rels_name not in master_files:
            raise ArticleMetadataError("The article master or manuscript lacks document relationships.")
        source_rels = _xml(original_files[source_rels_name])
        master_rels = _xml(master_files[source_rels_name])
        master_by_id = {item.get("Id"): item for item in master_rels}
        original_ct = _xml(original_files["[Content_Types].xml"])
        master_ct = _xml(master_files["[Content_Types].xml"])
        master_overrides = {item.get("PartName"): item.get("ContentType") for item in master_ct
                            if item.tag == CT_TAG + "Override"}
        master_defaults = {item.get("Extension"): item.get("ContentType") for item in master_ct
                           if item.tag == CT_TAG + "Default"}
        copied: dict[str, str] = {}

        def clone_part(part: str) -> str:
            if part in copied:
                return copied[part]
            if part not in master_files:
                raise ArticleMetadataError(f"The article master references a missing part: {part}")
            folder, filename = posixpath.split(part)
            counter = len(copied) + 1
            new_part = posixpath.join(folder, f"jasArticle{counter}_{filename}")
            while new_part in original_files or new_part in additions:
                counter += 1
                new_part = posixpath.join(folder, f"jasArticle{counter}_{filename}")
            copied[part] = new_part
            additions[new_part] = master_files[part]
            content_type = master_overrides.get("/" + part)
            if content_type:
                item = etree.SubElement(original_ct, CT_TAG + "Override")
                item.set("PartName", "/" + new_part)
                item.set("ContentType", content_type)
            else:
                extension = PurePosixPath(part).suffix.lstrip(".")
                if extension and not any(item.tag == CT_TAG + "Default" and item.get("Extension") == extension
                                         for item in original_ct):
                    item = etree.SubElement(original_ct, CT_TAG + "Default")
                    item.set("Extension", extension)
                    item.set("ContentType", master_defaults.get(extension, "application/octet-stream"))
            old_rels_name = _rels_path(part)
            if old_rels_name in master_files:
                rels = _xml(master_files[old_rels_name])
                for relation in rels:
                    if relation.get("TargetMode") == "External":
                        continue
                    old_dependency = _part_target(part, relation.get("Target", ""))
                    new_dependency = clone_part(old_dependency)
                    relation.set("Target", posixpath.relpath(new_dependency, posixpath.dirname(new_part)))
                additions[_rels_path(new_part)] = _bytes(rels)
            return new_part

        master_section = template_sections[-1]
        references: dict[tuple[str, str], str] = {}
        reference_order: list[tuple[str, str]] = []
        for ref in master_section:
            if ref.tag not in {W_TAG + "headerReference", W_TAG + "footerReference"}:
                continue
            kind = "header" if ref.tag == W_TAG + "headerReference" else "footer"
            variant = ref.get(W_TAG + "type", "default")
            relation = master_by_id.get(ref.get(R_TAG + "id"))
            if relation is None:
                raise ArticleMetadataError("The article master has a broken header/footer relationship.")
            key = (kind, variant)
            references[key] = clone_part(_part_target("word/document.xml", relation.get("Target", "")))
            reference_order.append(key)
        if elkolind:
            if ("header", "default") not in references or ("footer", "default") not in references:
                raise ArticleMetadataError("ELKOLIND master needs official default header and footer.")
            if ("footer", "even") not in references:
                raise ArticleMetadataError(
                    "ELKOLIND master needs its distinct even-page footer with first-author and short-title placeholders."
                )
            if ("header", "even") not in references:
                references[("header", "even")] = references[("header", "default")]
                reference_order.append(("header", "even"))
            if ("header", "first") not in references:
                references[("header", "first")] = references[("header", "default")]
                reference_order.append(("header", "first"))
            if ("footer", "first") not in references:
                references[("footer", "first")] = references[("footer", "default")]
                reference_order.append(("footer", "first"))
        if not references:
            raise ArticleMetadataError("The article master has no header/footer to preserve.")

        # Clone master styles under private IDs: manuscript body style IDs are untouched.
        if "word/styles.xml" in original_files and "word/styles.xml" in master_files:
            styles = _xml(original_files["word/styles.xml"])
            master_styles = _xml(master_files["word/styles.xml"])
            available = {item.get(W_TAG + "styleId"): item for item in master_styles
                         if item.tag == W_TAG + "style"}
            mapped_styles: dict[str, str] = {}
            used = {item.get(W_TAG + "styleId") for item in styles if item.tag == W_TAG + "style"}

            def clone_style(style_id: str) -> str:
                if style_id in mapped_styles:
                    return mapped_styles[style_id]
                if style_id not in available:
                    return style_id
                base = re.sub(r"[^A-Za-z0-9]", "", style_id)[:40] or "Style"
                new_id = "JASArticle" + base
                number = 2
                while new_id in used:
                    new_id = "JASArticle" + base + str(number)
                    number += 1
                used.add(new_id)
                mapped_styles[style_id] = new_id
                definition = copy.deepcopy(available[style_id])
                definition.set(W_TAG + "styleId", new_id)
                for tag in ("basedOn", "next", "link"):
                    dependency = definition.find(W_TAG + tag)
                    if dependency is not None and dependency.get(W_TAG + "val"):
                        dependency.set(W_TAG + "val", clone_style(dependency.get(W_TAG + "val")))
                styles.append(definition)
                return new_id

            for part in set(references.values()):
                root = _xml(additions[part])
                for node in root.xpath(".//w:pStyle|.//w:rStyle|.//w:tblStyle", namespaces=NS):
                    old = node.get(W_TAG + "val")
                    if old:
                        node.set(W_TAG + "val", clone_style(old))
                for paragraph in root.xpath(".//w:p", namespaces=NS):
                    if not paragraph.xpath("./w:pPr/w:pStyle", namespaces=NS) and "Normal" in available:
                        properties = paragraph.find(W_TAG + "pPr")
                        if properties is None:
                            properties = etree.Element(W_TAG + "pPr")
                            paragraph.insert(0, properties)
                        style_node = etree.Element(W_TAG + "pStyle")
                        style_node.set(W_TAG + "val", clone_style("Normal"))
                        properties.insert(0, style_node)
                additions[part] = _bytes(root)
            original_files["word/styles.xml"] = _bytes(styles)

        replacement_count = _replace_tokens(document, values)
        page_field_created = False
        all_parts = {**original_files, **additions}
        for (kind, _variant), part in references.items():
            root = _xml(additions[part])
            replacement_count += _replace_tokens(root, values)
            if kind == "header" and values.get("doi_full"):
                _link_doi(root, part, all_parts, values["doi_full"])
            additions[part] = _bytes(root)
            all_parts[part] = additions[part]
        for part, data in all_parts.items():
            if part not in original_files:
                additions[part] = data
        if elkolind:
            for variant in ("default", "even", "first"):
                header_part = references.get(("header", variant))
                footer_part = references.get(("footer", variant))
                if footer_part is None:
                    continue
                if any(_has_page_field(_xml(additions[part])) for part in (header_part, footer_part) if part):
                    continue
                footer = _xml(additions[footer_part])
                _append_page_field(footer)
                additions[footer_part] = _bytes(footer)
                page_field_created = True
            even_text = _compact_word_xml_text(_xml(additions[references[("footer", "even")]]))
            expected_even = re.sub(r"\s+", "", values["first_author"] + ": " + values["short_title_4w"])
            if expected_even not in even_text:
                raise ArticleMetadataError("The official even footer must contain first-author and short-title placeholders.")
            if "2356-0533" not in even_text or "2355-9195" not in even_text:
                raise ArticleMetadataError("The official ELKOLIND ISSN values are missing from the even footer.")
            odd_text = _text(_xml(additions[references[("footer", "default")]]))
            if "2356-0533" not in odd_text or "2355-9195" not in odd_text:
                raise ArticleMetadataError("The official ELKOLIND ISSN values are missing from the odd footer.")
            first_text = _text(_xml(additions[references[("footer", "first")]]))
            if "2356-0533" not in first_text or "2355-9195" not in first_text:
                raise ArticleMetadataError("The official ELKOLIND ISSN values are missing from the first-page footer.")

        existing_ids = {item.get("Id") for item in source_rels}
        relation_ids: dict[str, str] = {}
        for part in set(references.values()):
            number = 1
            while f"rId{number}" in existing_ids:
                number += 1
            relation_id = f"rId{number}"
            existing_ids.add(relation_id)
            relation_ids[part] = relation_id
            relation = etree.SubElement(source_rels, REL_TAG + "Relationship")
            relation.set("Id", relation_id)
            relation.set("Type", f"{R}/{'header' if 'header' in PurePosixPath(part).name.lower() else 'footer'}")
            relation.set("Target", posixpath.relpath(part, "word"))
        for section in target_sections:
            for old in list(section):
                if old.tag in {W_TAG + "headerReference", W_TAG + "footerReference"}:
                    section.remove(old)
            index = 0
            for kind, variant in reference_order:
                part = references[(kind, variant)]
                node = etree.Element(W_TAG + kind + "Reference")
                node.set(W_TAG + "type", variant)
                node.set(R_TAG + "id", relation_ids[part])
                section.insert(index, node)
                index += 1
            title_page = section.find(W_TAG + "titlePg")
            needs_first = any(variant == "first" for _, variant in references)
            if needs_first and title_page is None:
                title_page = etree.Element(W_TAG + "titlePg")
                doc_grid = section.find(W_TAG + "docGrid")
                section.insert(section.index(doc_grid) if doc_grid is not None else len(section), title_page)
            elif not needs_first and title_page is not None:
                section.remove(title_page)
        original_files["word/document.xml"] = _bytes(document)
        original_files[source_rels_name] = _bytes(source_rels)
        original_files["[Content_Types].xml"] = _bytes(original_ct)
        settings_name = "word/settings.xml"
        if settings_name in original_files and (elkolind or
                b"evenAndOddHeaders" in master_files.get(settings_name, b"")):
            settings = _xml(original_files[settings_name])
            if settings.find(W_TAG + "evenAndOddHeaders") is None:
                etree.SubElement(settings, W_TAG + "evenAndOddHeaders")
            original_files[settings_name] = _bytes(settings)
        result = io.BytesIO()
        with ZipFile(result, "w", ZIP_DEFLATED) as output:
            for member in source.infolist():
                output.writestr(member, original_files[member.filename])
            for name, content in additions.items():
                if name not in original_files:
                    output.writestr(name, content)
        media_count = sum(PurePosixPath(part).suffix.lower() in {".png", ".jpg", ".jpeg", ".emf", ".wmf"}
                          for part in copied)
        return result.getvalue(), {
            "metadata_replacements": replacement_count,
            "master_parts_copied": len(copied),
            "master_media_copied": media_count,
            "native_page_field_created": page_field_created,
        }


def graft_integrity(before: bytes, after: bytes, values: dict[str, str], *, elkolind: bool = False) -> dict[str, object]:
    """Verify a page-furniture graft changed no manuscript science or existing assets."""
    with ZipFile(io.BytesIO(before)) as source, ZipFile(io.BytesIO(after)) as result:
        prior = set(source.namelist())
        current = set(result.namelist())
        allowed_changes = {"[Content_Types].xml", "word/document.xml", "word/_rels/document.xml.rels",
                           "word/styles.xml", "word/settings.xml"}
        old_parts_preserved = prior <= current and all(
            source.read(name) == result.read(name) for name in prior - allowed_changes
        )
        expected_body = _xml(source.read("word/document.xml"))
        _replace_tokens(expected_body, values)
        actual_body = _xml(result.read("word/document.xml"))
        text_expected = expected_body.xpath(".//w:t/text()", namespaces=NS)
        text_actual = actual_body.xpath(".//w:t/text()", namespaces=NS)
        protected_paths = (".//w:tbl", ".//w:drawing|.//w:pict", ".//m:oMath|.//m:oMathPara")
        protected_equal = all(
            [etree.tostring(item) for item in expected_body.xpath(path, namespaces=NS)] ==
            [etree.tostring(item) for item in actual_body.xpath(path, namespaces=NS)]
            for path in protected_paths
        )
        referenced_parts = [name for name in current - prior if re.search(r"jasArticle\d+_(?:header|footer)", name, re.I)
                            and name.endswith(".xml")]
        native_page = any(_has_page_field(_xml(result.read(name))) for name in referenced_parts)
        even_odd = ("word/settings.xml" in current and
                    _xml(result.read("word/settings.xml")).find(W_TAG + "evenAndOddHeaders") is not None)
    checks = {"original_package_parts_preserved": old_parts_preserved,
              "body_text_only_expected_metadata_changed": text_expected == text_actual,
              "tables_figures_equations_preserved": protected_equal,
              "native_page_field_present": native_page if elkolind else True,
              "different_odd_even_pages_enabled": even_odd if elkolind else True}
    return {"passed": all(checks.values()), "checks": checks}
