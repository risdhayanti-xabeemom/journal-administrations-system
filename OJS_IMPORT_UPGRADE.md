# OJS import upgrade for the existing JAS installation

This revision changes `app.py`, adds `services/ojs_import.py`, `tests/test_ojs_import.py`, and `scripts/smoke_ojs_import.py`, and updates `README.md`, this guide, and `OJS_IMPORT_TEST_RESULTS.md`. It does not alter the database schema, remove data, or require a Python/virtual-environment rebuild. Existing `pandas` and `openpyxl` dependencies are sufficient.

## Local upgrade (Windows PowerShell)

1. Stop the running Streamlit process. In the existing project directory, back up the SQLite development database if present:

   ```powershell
   New-Item -ItemType Directory -Path .\backups -Force
   Copy-Item -LiteralPath .\jas.db -Destination ('.\backups\jas-pre-ojs-import-{0:yyyyMMdd-HHmmss}.db' -f (Get-Date))
   ```

   For PostgreSQL/Supabase, take a provider snapshot or `pg_dump` backup instead. Do **not** delete, recreate, or reset either database.

2. Check for local edits, then obtain the updated project files:

   ```powershell
   git status --short
   git pull --ff-only origin main
   ```

   If `git status` shows your own changes, preserve them and resolve the pull normally; do not force-reset. A ZIP installation can copy the changed application, test, script, and documentation files, leaving `jas.db`, secrets, uploads, and generated documents in place.

3. No new dependency is required. To verify an older environment against the existing requirements:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. No migration command is needed for this revision. The schema is unchanged. Run tests:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
   ```

5. Restart the existing app:

   ```powershell
   .\.venv\Scripts\python.exe -m streamlit run app.py
   ```

6. In `SUBMISSIONS → Import CSV/Excel`, upload an OJS CSV/XLSX, verify the mapping and preview, select the duplicate policy, then click `Confirm Import`. Download the error report if any rows need review.

## Push the source revision (maintainer)

From the project Git checkout, after reviewing the diff and tests:

```powershell
git status --short
git diff --check
git add app.py services/ojs_import.py tests/test_ojs_import.py README.md OJS_IMPORT_UPGRADE.md scripts/smoke_ojs_import.py OJS_IMPORT_TEST_RESULTS.md
git commit -m "Support mapped OJS CSV and XLSX submission imports"
git push origin main
```

Deploy the pushed `main` branch using the existing deployment process. Keep the current `DATABASE_URL`, private uploads, templates, and secrets. No Supabase migration or reset is part of this release.
