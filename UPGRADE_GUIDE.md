# Upgrade existing local JAS installation

This upgrade is additive. Do **not** delete `jas.db`, do not recreate the virtual environment, and do not reinstall Python.

## 1. Changed files

Core application changes:

- `app.py`, `config.py`
- `models/enums.py`, `models/entities.py`, `models/__init__.py`
- `services/core.py`, `services/database.py`
- new `services/docx_templates.py`, `services/template_service.py`
- `requirements.txt`, `requirements-dev.txt`, `.env.example`
- `Dockerfile`, `docker-compose.yml`

Migration and administration:

- new `scripts/migrate_master_loa.py`
- new `scripts/seed_official_templates.py`
- new `scripts/convert_docx_to_pdf.ps1`
- new `scripts/build_official_templates.py`
- `migrations/002_master_loa_template.md`

Official assets, samples, and verification:

- `templates/loa/elkolind/v1/LoA Elkolind-2026.docx`
- `templates/loa/jasens/v1/Draft_LoA_JASENS.docx`
- `samples/` (two LoAs, invoice, receipt, and rendered evidence)
- `tests/test_core.py`, `VISUAL_REGRESSION.md`, `TEST_RESULTS.txt`

## 2. Back up `jas.db`

Stop Streamlit first, open PowerShell in the JAS project directory, then run:

```powershell
New-Item -ItemType Directory -Force -Path .\backups
$jasBackupStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item -LiteralPath .\jas.db -Destination ".\backups\jas-before-master-loa-$jasBackupStamp.db"
```

Confirm that the backup exists:

```powershell
Get-ChildItem -LiteralPath .\backups\jas-before-master-loa-*.db
```

The migration command also creates a timestamped `.bak` copy automatically, but the explicit backup above gives the operator a separately named checkpoint.

For PostgreSQL production, run `pg_dump` using the deployment's normal backup process before migration. Then set `$env:JAS_MIGRATION_BACKUP_CONFIRMED = '1'` for the migration session.

## 3. Install only the new dependencies

Activate the existing virtual environment exactly as usual, then run:

```powershell
python -m pip install -r requirements.txt
```

This installs/updates packages in the current environment. A new Python installation or rebuilt virtual environment is not required.

## 4. Run the safe migration

SQLite/local:

```powershell
python scripts\migrate_master_loa.py
```

PostgreSQL, after a verified `pg_dump`:

```powershell
$env:JAS_MIGRATION_BACKUP_CONFIRMED = '1'
python scripts\migrate_master_loa.py
```

The migration is idempotent. It adds tables/columns and journal-specific default patterns without dropping or recreating existing records.

## 5. Configure DOCX-to-PDF and restart Streamlit

Windows with Microsoft Word may keep the default:

```powershell
$env:DOCX_PDF_CONVERTER = 'auto'
```

For LibreOffice, set the executable when it is not on `PATH`:

```powershell
$env:DOCX_PDF_CONVERTER = 'libreoffice'
$env:LIBREOFFICE_PATH = 'C:\Program Files\LibreOffice\program\soffice.exe'
```

Restart using the same environment and port as the existing installation. The standard local command is:

```powershell
python -m streamlit run app.py
```

If Streamlit is already running in the terminal, press `Ctrl+C` once, then run the command above.

## 6. Upload and activate the two official Master templates

Preferred UI workflow:

1. Sign in as `SUPER_ADMIN`.
2. Select journal `ELKOLIND`.
3. Open **ADMINISTRATION → Templates**.
4. Under **Letter of Acceptance Master Template**, choose **Upload Master DOCX**.
5. Select `templates/loa/elkolind/v1/LoA Elkolind-2026.docx` and click **Upload and activate**.
6. Click **Preview Template** and inspect the banner, signature/stamp, Editor-in-Chief block, and indexing logos.
7. Select journal `JASENS` and repeat with `templates/loa/jasens/v1/Draft_LoA_JASENS.docx`.
8. Confirm both pages show `ACTIVE`, filename, version, last updated, and updated by.

Equivalent one-time seed command (uses the first existing `SUPER_ADMIN` as uploader):

```powershell
python scripts\seed_official_templates.py
```

The command copies the bundled masters into configured private template storage. It does not point production records at the source-code directory.

## Post-upgrade smoke check

1. Open an `ACCEPTED` submission with volume/issue/month/year.
2. Click **Generate Preview** and verify `DRAFT PREVIEW — NOT ISSUED`.
3. Confirm the sequence table and document history did not gain a final LoA.
4. Click **Issue LoA**, then download both DOCX and PDF.
5. Reissue once with a reason and confirm the old record is `SUPERSEDED` while both old files remain downloadable.
6. Complete invoice → payment submitted → payment verified → receipt and confirm Rupiah uses `Rp 300.000` style.
