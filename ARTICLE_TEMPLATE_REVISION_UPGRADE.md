# Quick Manuscript Template Revision — upgrade existing JAS

This is an additive update to the existing application. It does not reset Supabase or `jas.db`, replace LoA masters, or alter invoice/payment/receipt records. Reviewer Revision remains available separately.

## What changed

- Navigation/UI: `app.py`, `article_revision_page.py`, `revision_page.py`.
- Existing model/storage integration: `models/__init__.py`, `services/database.py`, `services/revision_storage.py`.
- New model/services: `models/article_revision.py`, `services/article_template_service.py`, `services/article_formatting.py`, `services/article_metadata.py`, `services/article_revision_service.py`.
- Migration and verification: `scripts/migrate_article_revisions.py`, `scripts/generate_article_revision_demo.py`, `tests/test_article_revision.py`, `tests/test_article_revision_ui.py`, `samples/article_revision/`.
- Documentation: `README.md`, this guide, `ARTICLE_TEMPLATE_REVISION_TEST_RESULTS.md`.

The existing `document_templates` table already supports journal-specific types and versions. Article masters use `template_type=ARTICLE_TEMPLATE`, the existing unique active key, the existing checksum field, and separate JSON `profile` and `rules` objects in `field_mapping`. No legacy table is changed. Two additive tables hold formatting jobs and immutable output versions: `article_revision_jobs` and `article_revision_artifacts`. The article job table now has a nullable `metadata_json` field for dynamic publication metadata; the migration safely adds that field if an older article job table already exists. The existing audit log and private revision storage are reused.

## Local SQLite upgrade

Run these commands from the **existing** JAS project directory. Stop the current Streamlit process first. No Python reinstall or virtual-environment rebuild is needed.

```powershell
New-Item -ItemType Directory -Path .\backups -Force
$stamp = Get-Date -Format yyyyMMdd-HHmmss
Copy-Item -LiteralPath .\jas.db -Destination ".\backups\jas-before-article-revision-$stamp.db"
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/migrate_article_revisions.py
streamlit run app.py
```

If the earlier Reviewer Revision module was not migrated on this installation, run `python scripts/migrate_revisions.py` after the backup as well. The new migration makes another timestamped SQLite backup and is repeatable; it creates missing article-revision tables and adds only a missing `metadata_json` column to the article job table. Do not delete `jas.db`.

Open `http://localhost:8501/`, sign in, choose the active journal in the JAS sidebar, and open `ADMINISTRATION · Templates`. A Super Admin or Journal Admin can upload that journal's official **Article Template DOCX** and activate it. Repeat for ELKOLIND and JASENS using their real article master files. Do **not** upload the LoA documents as article masters. Previous article versions remain available for download and activation. The demo template under `samples/article_revision/` is synthetic and must not be activated as an official journal template.

Then open `REVISION · Quick Template Revision`, choose a submission or standalone job, upload the original manuscript DOCX, keep the default `Fast Auto Format (Template Only)` mode, and click `Auto Format & Generate`. Deterministic formatting is applied in one pass and the DOCX/XLSX are generated unless a scientific/integrity blocker is detected. Warnings remain downloadable in the report and do not block the DOCX. Use `Guided Review` only when detailed per-finding control is needed. Inspect the formatted document in Word, then mark the job complete. `REVISION · Reviewer Revision` remains a separate text-edit workflow.

Protected caption OOXML is preserved automatically. The alternate `Generate While Preserving Protected Objects` button invokes the same safe Fast Auto Format pipeline and is available as an explicit fallback. No database migration or new secret is required for this behavior.

## Template Rules JSON

Rules are optional and journal/template-specific. They are distinct from the visual style profile extracted from the uploaded DOCX. For example:

```json
{
  "title_max_words": 20,
  "abstract_min_words": 150,
  "abstract_max_words": 250,
  "keywords_min": 3,
  "keywords_max": 6,
  "figure_caption_prefix": "Figure",
  "table_caption_prefix": "Table",
  "reference_style": "Journal-provided style",
  "required_sections": ["Introduction", "Methodology", "Conclusion", "References"],
  "body_font": "Arial",
  "body_font_size": 11,
  "style_overrides": {"heading1": {"font": "Arial", "size_pt": 14, "alignment": 0}}
}
```

These values are examples only, **not official ELKOLIND or JASENS rules**. Enter only rules verified from each journal's article template/instructions. Missing scientific sections, references, figures, equations, and author metadata are flagged for human action; the system does not fabricate them.

## ELKOLIND official article master

The ELKOLIND initial semantic rules supplied for this revision are loaded for ELKOLIND only: A4, 19 mm top, 43 mm bottom, single column, Gadugi, centered 24 pt title (maximum 15 words), 9 pt abstract (100–200 words), 3–5 keywords, 10 pt body, 8 pt figure/table captions, and 8 pt IEEE-numbered references. Figure numbers are Arabic, table numbers uppercase Roman, and native Word equations are preserved. Caption placement/numbering, scientific references, equation numbering, and capitalization are audited; potentially unsafe text renumbering or scientific changes remain **REVIEW_REQUIRED**, not silently rewritten.

The written left/right margin instruction says 14.32 mm, while the uploaded official DOCX may have different physical values. In `ADMINISTRATION · Templates → Article Template`, compare the displayed values and explicitly confirm **both** margins. JAS stores the choice as a new template-rule version and blocks ELKOLIND document generation until this is done. No value is silently chosen.

Prepare a template-compatible copy of the real **ELKOLIND article DOCX** by changing only example metadata text to these placeholders, including any first/even/default header variants: `{{volume}}`, `{{issue}}`, `{{publication_month}}`, `{{publication_year}}`, either `{{doi_full}}` or `{{doi_suffix}}`, `{{first_author}}`, `{{short_title_4w}}`, `{{received_date}}`, `{{revised_date}}`, `{{accepted_date}}`. When `{{doi_suffix}}` is used, retain the official prefix `http://dx.doi.org/10.33795/elkolind.v{{volume}}i{{issue}}.` in the header. Placeholders may remain inside the original Word textboxes/shapes and may be split across runs; JAS scans the complete `w:t` XML and does not require moving them into ordinary paragraphs. The even footer must contain the first-author/short-title placeholders; retain the original ISSN wording, barcode image, placement and styles. Use `{{short_title_4w}}` without a hard-coded trailing ellipsis: JAS adds it only for titles longer than four words. Keep or add a native Word **PAGE field**, never `{{page}}`. JAS enables Word's Different Odd & Even Pages and can add a PAGE field where a master footer variant lacks one; inspect that fallback visually before distribution.

Editors enter or confirm metadata when creating the formatting job. Ordered submission authors supply the default first author; dates and DOI may be entered explicitly. If `doi_full` is blank but `doi_suffix` is present, JAS forms `http://dx.doi.org/10.33795/elkolind.v{volume}i{issue}.{doi_suffix}` and links it in the header. Required placeholders without values block generation. Before generating artifacts, an editor can correct metadata and rerun the audit, or select a newer active article template version; both actions are audited and invalidate prior formatting approvals. Issued artifact versions are never silently changed. The source master remains unchanged; only the generated manuscript copy receives dynamic values and copied header/footer parts. Other journals retain their independently configured rules.

The existing private storage contained an ELKOLIND Article Master during this verification. It was used read-only to reproduce the textbox issue and confirm generation; the committed regression fixture removes embedded font binaries while preserving the relevant header, textbox, PAGE-field, barcode/media, and relationship structures. Continue to inspect first, even, and odd pages whenever a new official version is activated. Equivalent JASENS visual verification still requires its active Article Master.

## PostgreSQL, Supabase and Streamlit Cloud

Back up the production PostgreSQL database using the existing Supabase backup or `pg_dump` process and verify it is restorable. In a controlled maintenance session using the existing `DATABASE_URL`, set `JAS_MIGRATION_BACKUP_CONFIRMED=1`, then run `python scripts/migrate_article_revisions.py`. The script refuses PostgreSQL changes without that flag. It does not reset the database or alter old tables.

No new secret is required. Article masters, manuscripts, DOCX outputs, and XLSX reports use the existing private `REVISION_STORAGE_BACKEND` service. For Streamlit Cloud, use `REVISION_STORAGE_BACKEND=supabase` with the existing `REVISION_SUPABASE_URL`, `REVISION_SUPABASE_SERVICE_ROLE_KEY`, and a **private** `REVISION_SUPABASE_BUCKET`. Do not publish the bucket or put the service-role key in source control. Local storage is for development only. The existing `REVISION_MAX_FILE_BYTES` limit applies.

Install the updated `requirements.txt`; it adds `python-docx>=1.1,<2` to the baseline, while `openpyxl` was already present. No Python reinstall or environment rebuild is required. Basic template formatting is deterministic and sends no manuscript content to an AI provider. `Template + Language Polish` and `Template + Reviewer` currently run the deterministic formatting phase only; textual changes still belong in the separate editor-approved Reviewer Revision workflow. Do not treat either combined mode as automatic polishing/reviewer response.

## Commit and push (not run automatically)

Review the diff and verify the target branch and deployment Secrets first. Then:

```powershell
git status --short
git diff --check
git add .env.example .gitignore README.md app.py config.py requirements.txt article_revision_page.py revision_page.py models/__init__.py models/article_revision.py models/revision.py services/database.py services/revision_storage.py services/revision_ai.py services/revision_analysis.py services/revision_docx.py services/revision_service.py services/article_template_service.py services/article_formatting.py services/article_metadata.py services/article_revision_service.py scripts/migrate_revisions.py scripts/migrate_article_revisions.py scripts/generate_article_revision_demo.py tests/test_revision.py tests/test_revision_ui.py tests/test_article_revision.py tests/test_article_revision_ui.py samples/revision samples/article_revision REVISION_UPGRADE.md REVISION_TEST_RESULTS.md ARTICLE_TEMPLATE_REVISION_UPGRADE.md ARTICLE_TEMPLATE_REVISION_TEST_RESULTS.md
git commit -m "Add journal-specific manuscript template revision"
git push origin HEAD
```

The explicit staging command includes the earlier Reviewer Revision module because those files remain uncommitted in this working tree and `app.py` imports them. If they were already committed separately, Git simply stages no change for them. Never push `.env`, Streamlit Secrets, private manuscripts, or `jas.db`.
