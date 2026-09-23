# Quick Article Revision verification

## Automated tests

Command: `pytest -q -p no:cacheprovider` with a writable temporary test directory and dependencies from `requirements.txt`.

Latest complete run: **34 passed**. `tests/test_revision.py` covers DOCX/PDF/TXT reviewer extraction, comment classification and mapping, two-reviewer service-level lifecycle with a structured AI proposal, explicit decisions, immutable original and artifact downloads, response generation, multiple rounds, role/journal isolation, numeric/citation blocking, run formatting, header/footer/table/figure/equation/reference preservation, exact paragraph offsets, numbered references, AI outage and malicious numeric proposals, editorial polishing source separation, private Supabase adapter behavior, and PostgreSQL DDL compilation. `tests/test_revision_ui.py` verifies the revision view is not exposed as an empty automatic Streamlit page and exercises the Streamlit widget flow from uploads through analysis, editor decisions, four generated artifacts, and completion. Existing JAS tests for login-adjacent services, LoA, invoices, payment, receipts, and OJS import also passed.

## Migration rehearsal

The existing `jas.db` was **not migrated**. A copy was migrated instead, then migrated again to verify idempotence. The copy retained row counts for all 13 existing tables, gained five revision tables, and the SQLite backup passed `PRAGMA integrity_check`. The original `jas.db` SHA-256 remained `694BF14C0F9FCA602EF86DBC523996967F4BFA89ECAF171669F9C9215AB4608B`.

## Representative document visual check

The generated sample in `samples/revision/` includes original, highlighted revised, clean revised, response-to-reviewers, and revision-log files. Microsoft Word rendered the original, revised, clean, and response DOCX files to one-page PDFs; those PDFs were rasterized and inspected. Original/revised/clean page margins and header/footer placements match. The targeted wording changed in place; the marked version has yellow highlights and the clean version has none. Section headings, caption positions, table, equation, figure object placeholder, citation, and reference remain present. The response document has reviewer-by-reviewer sections and does not claim a change for the rejected duplicate comment. Automated package comparison additionally found no missing header/footer XML or media bytes.

A second representative case with an explicit page break was rendered before and after patching. Both PDFs have **two pages**. Inspection found header/footer on both pages, unchanged table/caption/equation/figure placement on page one, unchanged numbered reference section on page two, and only the approved word highlighted in the revised document.

The representative manuscript deliberately uses a tiny placeholder image; this confirms media-part preservation but is not a high-resolution figure quality test. A real journal manuscript should still be visually inspected by the editor before distribution, especially if a proposed paragraph grows substantially. Word Track Changes is **not** claimed; only yellow highlighting is implemented.

## Limits not concealed

- Live production PostgreSQL/Supabase was not accessed. PostgreSQL table DDL was compiled in tests, and the private Storage adapter was tested with an isolated HTTP mock, not real credentials.
- The Streamlit UI was exercised end-to-end with AppTest widgets, but a separate browser-click test against the deployed cloud instance and actual external AI output were not performed.
- PDF manuscripts can be reviewer input only. A manuscript must be DOCX for format-preserving patching.
- Page count and line wrapping are not deterministically checked by the OOXML integrity engine; the editor must inspect generated Word documents before final completion.
