# Migration 002 — Master LoA templates

Run `python scripts/migrate_master_loa.py` from the project directory. The migration is additive and idempotent:

- SQLite is copied to a timestamped `backups/*.db.bak` file before schema changes.
- PostgreSQL requires a completed `pg_dump` and `JAS_MIGRATION_BACKUP_CONFIRMED=1`.
- Existing tables and rows are never dropped or recreated.
- `document_templates` is added; Journal, Submission, and LoA records receive nullable or safely defaulted columns.
- Existing users, journals, submissions, LoAs, invoices, payments, receipts, verifications, sequences, reports, and audit logs remain in place.
