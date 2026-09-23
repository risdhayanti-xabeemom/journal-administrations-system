"""Generate non-official regression artifacts from the representative test manuscript.

These files are DEMO fixtures, not ELKOLIND/JASENS article masters.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from docx import Document
from docx.shared import Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.article_formatting import (
    apply_safe_fixes, audit_article, compliance_report_xlsx, extract_style_profile, formatting_integrity,
)


def main() -> None:
    original_path = ROOT / "samples" / "revision" / "multipage_original.docx"
    output = ROOT / "samples" / "article_revision"
    output.mkdir(parents=True, exist_ok=True)
    original = original_path.read_bytes()
    master = Document(io.BytesIO(original))
    section = master.sections[0]
    section.page_width, section.page_height = Inches(8.27), Inches(11.69)
    section.top_margin = section.bottom_margin = Inches(0.75)
    section.left_margin = section.right_margin = Inches(0.85)
    master.styles["Normal"].font.name = "Arial"
    master.styles["Normal"].font.size = Pt(11)
    master.styles["Title"].font.name = "Arial"
    master.styles["Title"].font.size = Pt(18)
    master.styles["Heading 1"].font.name = "Arial"
    master.styles["Heading 1"].font.size = Pt(13)
    master_path = output / "DEMO_Article_Template.docx"
    master.save(master_path)
    profile = extract_style_profile(master_path.read_bytes())
    rules = {"required_sections": ["Introduction", "Methodology", "References"],
             "figure_caption_prefix": "Figure", "table_caption_prefix": "Table"}
    findings = audit_article(original, profile, rules)
    selected = {item.id for item in findings if item.status == "SAFE_FIX_AVAILABLE"}
    formatted = apply_safe_fixes(original, findings, selected)
    integrity = formatting_integrity(original, formatted)
    if not integrity["passed"]:
        raise RuntimeError(f"Demo integrity failed: {integrity}")
    (output / "DEMO_Formatted_Manuscript.docx").write_bytes(formatted)
    report = compliance_report_xlsx(journal="DEMO ONLY", manuscript_name=original_path.name,
        template_version=1, findings=findings, approved_ids=selected, integrity=integrity)
    (output / "DEMO_Template_Compliance_Report.xlsx").write_bytes(report)
    print(f"Demo safe fixes: {len(selected)}; integrity: PASS; files: {output}")


if __name__ == "__main__":
    main()
