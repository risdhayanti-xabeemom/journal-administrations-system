from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.docx_templates import pin_anchor_vertical_to_page, replace_docx_text, sha256_file, validate_required_fields


ELKOLIND_REPLACEMENTS = {
    "028/SK/ELK/IV/2026": "{{loa_number}}",
    "Muhammad Mundzir Mubarok, Kasiyanto, Mokhammad Syafaat": "{{authors}}",
    "Muhammad Mundzir Mubarok": "{{recipient_name}}",
    "9354": "{{ojs_submission_id}}",
    "Analisis kinerja penguat BJT common-emitter dengan kapasitor kopling dan kapasitor bypass": "{{article_title}}",
    "Volume 13": "Volume {{volume}}",
    "Nomor 1": "Nomor {{issue}}",
    "Mei": "{{publication_month}}",
    "Tahun 2026": "Tahun {{publication_year}}",
    "27 April 2027": "{{loa_date}}",
}

JASENS_REPLACEMENTS = {
    "04/V/JASENS/2026": "{{loa_number}}",
    "Bambang Harie Wiyono, Filia Nur Anjaini, Lukman Rosyidi": "{{authors}}",
    "Filia Nur Anjaini": "{{recipient_name}}",
    "Sekolah Tinggi Teknologi Terpadu Nurul Fikri": "{{recipient_affiliation}}",
    "Analisis Kerentanan Keamanan Chatbot Berbasis Large Language Model Terhadap Serangan SQL Injection": "{{article_title}}",
    "Volume 7": "Volume {{volume}}",
    "No. 1": "No. {{issue}}",
    "June 2026": "{{publication_month}} {{publication_year}}",
}


def build(source: Path, destination: Path, abbreviation: str, replacements: dict[str, str], *, pin_anchors: dict[int, int] | None = None) -> None:
    replace_docx_text(source, destination, replacements, require_all=True)
    for anchor_index, y_emu in (pin_anchors or {}).items():
        pin_anchor_vertical_to_page(destination, anchor_index, y_emu)
    validate_required_fields(destination, abbreviation)
    print(f"{abbreviation}: {destination} sha256={sha256_file(destination)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build official placeholder templates from untouched reference DOCX files.")
    parser.add_argument("--elkolind-source", type=Path, required=True)
    parser.add_argument("--jasens-source", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "templates" / "loa")
    args = parser.parse_args()
    build(
        args.elkolind_source,
        args.output_root / "elkolind" / "v1" / "LoA Elkolind-2026.docx",
        "ELKOLIND",
        ELKOLIND_REPLACEMENTS,
    )
    build(
        args.jasens_source,
        args.output_root / "jasens" / "v1" / "Draft_LoA_JASENS.docx",
        "JASENS",
        JASENS_REPLACEMENTS,
        # The official indexing banner was anchored to a body paragraph. Pin it to
        # the audited original page coordinate so shorter/longer manuscript text
        # cannot move the journal's static footer artwork.
        pin_anchors={1: 9_493_250},
    )


if __name__ == "__main__":
    main()
