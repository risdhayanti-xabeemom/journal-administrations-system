-- Journal Administration System - PostgreSQL 14+ initial schema
-- Run inside the target database with a role allowed to create extensions/tables.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE journals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name varchar(255) NOT NULL,
    abbreviation varchar(32) NOT NULL UNIQUE,
    issn varchar(32), e_issn varchar(32), publisher varchar(255), website varchar(500),
    logo_path varchar(500), address text, contact_email varchar(255), editor_in_chief varchar(255),
    signature_path varchar(500), stamp_path varchar(500),
    default_apc numeric(14,2) NOT NULL DEFAULT 0 CHECK (default_apc >= 0),
    currency varchar(3) NOT NULL DEFAULT 'IDR',
    bank_name varchar(255), bank_account varchar(128), account_holder varchar(255), qris_path varchar(500),
    loa_number_format varchar(255) NOT NULL DEFAULT '{sequence:03d}/LoA/{journal}/{roman_month}/{year}',
    invoice_number_format varchar(255) NOT NULL DEFAULT 'INV/{journal}/{year}/{sequence:04d}',
    receipt_number_format varchar(255) NOT NULL DEFAULT 'RCP/{journal}/{year}/{sequence:04d}',
    loa_template text, invoice_template text, receipt_template text,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email varchar(255) NOT NULL UNIQUE,
    display_name varchar(255) NOT NULL,
    password_hash varchar(500) NOT NULL,
    role varchar(32) NOT NULL CHECK (role IN ('SUPER_ADMIN','JOURNAL_ADMIN','FINANCE')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(), last_login_at timestamptz
);

CREATE TABLE user_journals (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    journal_id uuid NOT NULL REFERENCES journals(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, journal_id)
);

CREATE TABLE submissions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_id uuid NOT NULL REFERENCES journals(id) ON DELETE RESTRICT,
    ojs_submission_id varchar(128), manuscript_title text NOT NULL,
    corresponding_author varchar(255) NOT NULL, email varchar(255) NOT NULL, affiliation text,
    date_submitted date, date_accepted date,
    editorial_status varchar(32) NOT NULL DEFAULT 'SUBMITTED' CHECK (editorial_status IN ('SUBMITTED','UNDER_REVIEW','ACCEPTED','REJECTED','WITHDRAWN')),
    publication_status varchar(32) NOT NULL DEFAULT 'NOT_READY' CHECK (publication_status IN ('NOT_READY','READY_FOR_PUBLICATION','PUBLISHED')),
    planned_volume varchar(32), planned_issue varchar(32), planned_year integer,
    doi varchar(255), article_url varchar(500), notes text,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_submission_journal_ojs UNIQUE (journal_id, ojs_submission_id)
);

CREATE TABLE authors (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id uuid NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    name varchar(255) NOT NULL, email varchar(255), affiliation text,
    is_corresponding boolean NOT NULL DEFAULT false, position integer NOT NULL,
    CONSTRAINT uq_author_position UNIQUE (submission_id, position)
);

CREATE TABLE document_verifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token varchar(128) NOT NULL UNIQUE,
    document_type varchar(32) NOT NULL CHECK (document_type IN ('LOA','INVOICE','RECEIPT')),
    journal_id uuid NOT NULL REFERENCES journals(id) ON DELETE RESTRICT,
    submission_id uuid NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
    document_number varchar(255) NOT NULL,
    document_status varchar(32) NOT NULL DEFAULT 'VALID' CHECK (document_status IN ('VALID','REVOKED','SUPERSEDED','CANCELLED')),
    issue_date date NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE loa_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_id uuid NOT NULL REFERENCES journals(id) ON DELETE RESTRICT,
    submission_id uuid NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
    verification_id uuid NOT NULL UNIQUE REFERENCES document_verifications(id) ON DELETE RESTRICT,
    document_number varchar(255) NOT NULL UNIQUE, version integer NOT NULL DEFAULT 1,
    issue_date date NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'VALID' CHECK (status IN ('VALID','REVOKED','SUPERSEDED')),
    pdf_path varchar(500), created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_loa_submission_version UNIQUE (submission_id, version)
);

CREATE TABLE invoices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_id uuid NOT NULL REFERENCES journals(id) ON DELETE RESTRICT,
    submission_id uuid NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
    verification_id uuid NOT NULL UNIQUE REFERENCES document_verifications(id) ON DELETE RESTRICT,
    invoice_number varchar(255) NOT NULL UNIQUE,
    invoice_date date NOT NULL, due_date date NOT NULL,
    apc_amount numeric(14,2) NOT NULL, discount numeric(14,2) NOT NULL DEFAULT 0,
    additional_charge numeric(14,2) NOT NULL DEFAULT 0, total_amount numeric(14,2) NOT NULL,
    currency varchar(3) NOT NULL, payment_method varchar(255), notes text,
    status varchar(32) NOT NULL DEFAULT 'ISSUED' CHECK (status IN ('DRAFT','ISSUED','WAITING_PAYMENT','PAYMENT_SUBMITTED','PAID','CANCELLED')),
    author_token_hash varchar(64) NOT NULL UNIQUE, author_token_expires_at timestamptz NOT NULL,
    pdf_path varchar(500), created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_invoice_nonnegative CHECK (apc_amount >= 0 AND discount >= 0 AND additional_charge >= 0 AND total_amount >= 0)
);

CREATE TABLE payments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
    payer_name varchar(255) NOT NULL, payment_method varchar(255) NOT NULL,
    payment_date date NOT NULL, amount_paid numeric(14,2) NOT NULL CHECK (amount_paid > 0),
    proof_path varchar(500) NOT NULL, proof_original_name varchar(255) NOT NULL,
    author_notes text, internal_notes text,
    status varchar(32) NOT NULL DEFAULT 'SUBMITTED' CHECK (status IN ('SUBMITTED','VERIFIED','REJECTED')),
    submitted_at timestamptz NOT NULL DEFAULT now(), verified_at timestamptz,
    verified_by uuid REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE receipts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_id uuid NOT NULL REFERENCES journals(id) ON DELETE RESTRICT,
    invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
    payment_id uuid NOT NULL UNIQUE REFERENCES payments(id) ON DELETE RESTRICT,
    verification_id uuid NOT NULL UNIQUE REFERENCES document_verifications(id) ON DELETE RESTRICT,
    receipt_number varchar(255) NOT NULL UNIQUE, issue_date date NOT NULL,
    amount numeric(14,2) NOT NULL, authorized_person varchar(255) NOT NULL,
    pdf_path varchar(500), created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE document_sequences (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_id uuid NOT NULL REFERENCES journals(id) ON DELETE RESTRICT,
    document_type varchar(32) NOT NULL CHECK (document_type IN ('LOA','INVOICE','RECEIPT')),
    year integer NOT NULL, current_value integer NOT NULL DEFAULT 0 CHECK (current_value >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_sequence_scope UNIQUE (journal_id, document_type, year)
);

CREATE TABLE audit_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    journal_id uuid REFERENCES journals(id) ON DELETE SET NULL,
    action varchar(128) NOT NULL, object_type varchar(128) NOT NULL,
    object_id varchar(128) NOT NULL, previous_value text, new_value text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE system_settings (
    key varchar(128) PRIMARY KEY, value text, updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_users_role ON users(role);
CREATE INDEX ix_submissions_journal ON submissions(journal_id);
CREATE INDEX ix_submissions_email ON submissions(email);
CREATE INDEX ix_submissions_doi ON submissions(doi);
CREATE INDEX ix_submissions_worklist ON submissions(journal_id, editorial_status, publication_status, planned_year);
CREATE INDEX ix_authors_submission ON authors(submission_id);
CREATE INDEX ix_verification_token ON document_verifications(token);
CREATE INDEX ix_verification_number ON document_verifications(document_number);
CREATE INDEX ix_loa_submission ON loa_documents(submission_id);
CREATE INDEX ix_invoice_submission ON invoices(submission_id);
CREATE INDEX ix_invoice_status ON invoices(status);
CREATE INDEX ix_payment_invoice ON payments(invoice_id);
CREATE INDEX ix_payment_status ON payments(status);
CREATE INDEX ix_receipt_invoice ON receipts(invoice_id);
CREATE INDEX ix_audit_journal_created ON audit_logs(journal_id, created_at DESC);

INSERT INTO journals (name, abbreviation, publisher, editor_in_chief)
VALUES
    ('ELKOLIND - configure journal name', 'ELKOLIND', 'Configure in Settings', 'Configure in Settings'),
    ('JASENS - configure journal name', 'JASENS', 'Configure in Settings', 'Configure in Settings')
ON CONFLICT (abbreviation) DO NOTHING;

COMMIT;

