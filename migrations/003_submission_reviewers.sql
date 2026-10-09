-- Journal Administration System - reviewer names from the OJS review report (PostgreSQL 14+)
-- Additive only: creates one new table and touches no existing data.
-- The app also creates this table automatically at start-up (init_database); run this
-- file only if you prefer to install it explicitly, for example on Supabase.
BEGIN;

CREATE TABLE IF NOT EXISTS submission_reviewers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id uuid NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    reviewer_name varchar(255) NOT NULL,
    ojs_reviewer varchar(128) NOT NULL,
    review_round integer NOT NULL DEFAULT 1,
    review_state varchar(16) NOT NULL DEFAULT 'PENDING',
    recommendation varchar(64),
    date_assigned date,
    date_completed date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_submission_reviewer_round UNIQUE (submission_id, review_round, ojs_reviewer)
);

CREATE INDEX IF NOT EXISTS ix_submission_reviewers_submission_id ON submission_reviewers(submission_id);

COMMIT;
