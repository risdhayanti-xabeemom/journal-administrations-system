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

## Review report and status completion (October 2026)

This revision adds reviewer names from the OJS review report and completes editorial/publication status from the OJS articles report.

**What changed**

- `services/ojs_status.py` (new): maps the OJS `Status` column. `Review` → UNDER_REVIEW; `Copyediting`, `Production`, `Scheduled` → ACCEPTED; `Published` → ACCEPTED and PUBLISHED; `Declined` → REJECTED. Previously the last four were "unrecognized" and imported as SUBMITTED.
- `date_accepted` is filled for accepted articles from the earliest "Accept Submission" editor decision (else "Send To Production"). Articles whose report has no such decision keep an empty acceptance date.
- `SUBMISSIONS → Import CSV/Excel` now has two tabs. **Articles report** gained the option *Complete editorial and publication status of existing submissions from OJS* (on by default). **Review report (reviewer names)** imports the OJS review export.
- New table `submission_reviewers` (reviewer name, OJS username, round, state, recommendation, assigned/completed dates). `init_database()` creates it automatically; `migrations/003_submission_reviewers.sql` does the same explicitly. No existing table or row is changed.
- `SUBMISSIONS → All Submissions` shows a `Reviewers` column and a per-submission reviewer list (SUPER_ADMIN and JOURNAL_ADMIN only).

**Status sync rules (existing records)**

- Forward only: SUBMITTED or UNDER_REVIEW may move to the OJS stage. An ACCEPTED record becomes PUBLISHED when OJS says Published.
- Records with an LoA or invoice, and records that are REJECTED or WITHDRAWN, are never changed.
- The preview shows each change (`Editorial SUBMITTED → ACCEPTED; Publication NOT_READY → PUBLISHED`) before you confirm. Re-running the same file changes nothing.

**Review report rules**

- Import the articles report first; review rows are matched to submissions by OJS Submission ID inside the active journal.
- Reviewer e-mail addresses and review comments are ignored. Only the name, OJS username, round, state (PENDING, IN_PROGRESS, COMPLETED, DECLINED, CANCELLED), recommendation, and two dates are stored.
- A title check skips rows whose title shares fewer than half its words with the JAS submission, which catches another journal's report uploaded by mistake. It can be switched off in the preview.
- Re-importing updates changed rows and leaves identical rows alone; nothing is duplicated.

**Upgrade steps**: follow the local upgrade steps above (back up the database, pull the files, run `pytest tests -q -p no:cacheprovider`, restart). Then import the articles report with the new option ticked, and the review report in the second tab.
