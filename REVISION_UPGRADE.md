# Quick Article Revision — upgrade existing JAS

This is an additive update. It does not reset JAS, delete `jas.db`, alter existing financial/document tables, or upload to OJS.

## Files changed

- Application and configuration: `app.py`, `config.py`, `.env.example`, `.gitignore`, `README.md`, `requirements.txt`.
- Database model/migration: `models/__init__.py`, `models/revision.py`, `services/database.py`, `scripts/migrate_revisions.py`.
- Revision module: `revision_page.py`, `services/revision_ai.py`, `services/revision_analysis.py`, `services/revision_docx.py`, `services/revision_service.py`, `services/revision_storage.py`.
- Verification and examples: `tests/test_revision.py`, `tests/test_revision_ui.py`, `REVISION_TEST_RESULTS.md`, `samples/revision/`.

The migration creates only five new tables: `revision_jobs`, `revision_review_files`, `revision_comments`, `revision_artifacts`, and `revision_integrity_checks`. Existing JAS columns remain unchanged. Original manuscript, reviewer files, and every generated artifact are stored under random private object keys and hashed with SHA-256. `revision_jobs` can refer to an existing submission and journal, or be standalone within a journal. Rounds are unique per linked submission; artifacts are versioned and never overwritten.

New dependency: `python-docx>=1.1,<2`. The existing SQLAlchemy, pypdf, lxml, ReportLab, and Streamlit dependencies are reused.

## Local SQLite upgrade

1. Stop Streamlit. In the existing project directory, make an explicit offline backup (the migration also creates its own timestamped SQLite backup):

   ```powershell
   New-Item -ItemType Directory -Path .\backups -Force
   $stamp = Get-Date -Format yyyyMMdd-HHmmss
   Copy-Item -LiteralPath .\jas.db -Destination ".\backups\jas-before-revisions-$stamp.db"
   ```

2. Activate the **existing** virtual environment; do not rebuild it. Install the one new dependency and reconcile existing requirements:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Run the additive migration:

   ```powershell
   python scripts/migrate_revisions.py
   ```

   The script uses SQLite's online backup API and prints the generated backup location. It does not delete or recreate `jas.db`.

4. Restart the existing app:

   ```powershell
   streamlit run app.py
   ```

5. Open `http://localhost:8501/` (not the old `/revision` URL). Sign in as a Super Admin or Journal Admin, select the correct journal, then open `REVISION · Quick Article Revision`. Create a job, upload the original DOCX already formatted in the journal template plus reviewer files, analyze, approve/edit/reject each proposal, generate and inspect the four artifacts, then mark the job complete. Existing LoA templates and payment records are untouched.

## PostgreSQL / Supabase and Streamlit Cloud

Back up the **production database** first using your established Supabase backup or `pg_dump` process. Confirm that the backup is restorable. Then run the migration against the existing production `DATABASE_URL` in a controlled maintenance session, with `JAS_MIGRATION_BACKUP_CONFIRMED=1`. The script refuses PostgreSQL migration without this flag. Do not reset Supabase or apply the SQLite file to production.

Streamlit Cloud's local disk must not be used for durable unpublished manuscripts. In Supabase Storage, create a bucket named `jas-private-revisions` (or your chosen configured name) with **Public OFF**. The adapter checks the bucket metadata before every upload/download and refuses public buckets. Set the following in Streamlit Secrets (use your actual project URL/key; never commit the key):

```toml
REVISION_STORAGE_BACKEND = "supabase"
REVISION_SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
REVISION_SUPABASE_SERVICE_ROLE_KEY = "YOUR_SERVER_SIDE_SERVICE_ROLE_KEY"
REVISION_SUPABASE_BUCKET = "jas-private-revisions"
REVISION_MAX_FILE_BYTES = "20971520"
REVISION_DEFAULT_MODE = "REVIEWER_DRIVEN"
REVISION_HIGHLIGHT_STYLE = "yellow"
REVISION_ALLOW_FULL_POLISHING = "false"
REVISION_AI_ENABLED = "false"
```

The service-role key is server-side only; JAS still checks role and journal membership before serving an artifact. The bucket must be private because a public bucket would expose unpublished work to anyone with an object URL. [Supabase private bucket guidance](https://supabase.com/docs/guides/storage/buckets/fundamentals) and [authenticated downloads](https://supabase.com/docs/guides/storage/serving/downloads) describe this behavior.

AI is optional and disabled by default. To opt in, configure an approved HTTPS OpenAI-compatible endpoint and add these **Secrets**, not database plaintext:

```toml
REVISION_AI_ENABLED = "true"
REVISION_AI_PROVIDER = "openai_compatible"
REVISION_AI_ENDPOINT = "https://YOUR_APPROVED_ENDPOINT/v1/chat/completions"
REVISION_AI_MODEL = "YOUR_APPROVED_MODEL"
REVISION_AI_API_KEY = "YOUR_PRIVATE_API_KEY"
```

Even when enabled, the editor must consent for each analysis. The provider receives reviewer comment text and mapped manuscript paragraph text; in Full Academic Polishing it can receive every eligible body paragraph, one request at a time. It does **not** receive the binary DOCX or reviewer files. Confirm your institution's research-data policy and vendor agreement before enabling it. If AI is unavailable, manual proposals remain available and the rest of JAS continues working.

## Commit and push (not run automatically)

Review `git diff` and `git status` first. Then from the JAS project directory:

```powershell
git add .env.example .gitignore README.md REVISION_UPGRADE.md REVISION_TEST_RESULTS.md requirements.txt app.py config.py models/__init__.py models/revision.py services/database.py services/revision_ai.py services/revision_analysis.py services/revision_docx.py services/revision_service.py services/revision_storage.py revision_page.py scripts/migrate_revisions.py tests/test_revision.py tests/test_revision_ui.py samples/revision
git commit -m "Add editor-controlled Quick Article Revision module"
git push origin main
```

Only push after verifying the target branch and deployment Secrets. No automatic OJS synchronization, reviewer messaging, or acceptance decision is included.
