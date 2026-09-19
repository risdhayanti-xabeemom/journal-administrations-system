# OJS import verification — 20 September 2026

## Automated tests

The full test suite passed: **24 passed in 157.42 seconds**. The 14 new import tests cover exact JAS headings, OJS aliases, case/space/hyphen normalization, ambiguous headings, missing optional/required fields, journal-scoped duplicates, protected metadata updates, malformed rows including broken CSV quoting, HTML entity and tag cleanup, UTF-8 BOM and delimiters, XLSX, partial import, author order/raw retention, and CSV error-report formula escaping. The 10 existing JAS tests also passed.

Command: `.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider --basetemp='C:\Users\Lenovo\Documents\Codex\2026-09-06\jalankan-sesuai-yang-dimin\work\ojs-pytest-final-67e21c04'`

An initial full-suite attempt could not access Windows' default pytest temporary directory during three LoA test setups. A unique writable `--basetemp` path resolved that environment-only issue. The final run had no failures.

## Manual smoke check

Command: `.\.venv\Scripts\python.exe scripts\smoke_ojs_import.py`

The script uses **only in-memory SQLite** and a synthetic UTF-8-BOM, semicolon-delimited OJS CSV. Auto mapping selected `Submission-ID`, `Article Title`, `Contributors`, `Primary Contact`, and `Contact Email`. Preview showed two `INCOMPLETE_METADATA` rows, one `MISSING_REQUIRED_FIELD`, and one `INVALID_ROW`. Row count was **0 before confirmation**, then **2 after confirmation**. Result: imported 2, incomplete 2, invalid 2, duplicates skipped 0, updated 0; one batch audit event. The check exited successfully.

No production OJS export was supplied, and no interactive browser click-through against a deployed Streamlit instance was performed. A journal editor should inspect the mapping and preview from a representative real report before confirmation.

## Data/schema integrity

No model or migration file changed. The local `jas.db` was neither opened by the smoke check nor modified; its SHA-256 remained `694BF14C0F9FCA602EF86DBC523996967F4BFA89ECAF171669F9C9215AB4608B`.
