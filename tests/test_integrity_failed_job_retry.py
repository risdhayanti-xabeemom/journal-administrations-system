"""A job that failed integrity once must stay retryable.

Regression for a bug where INTEGRITY_FAILED was in EDITABLE_STATUSES (so the Quick
Template Revision buttons still rendered) but audit_article_revision_job's own status
check omitted it. Auto Format & Generate calls audit_article_revision_job internally,
so the very first thing a retry did was raise "This job cannot be re-analyzed after
formatting.", permanently trapping the job after one integrity failure.
"""

from __future__ import annotations

import inspect

import services.article_revision_service as svc


def test_editable_statuses_includes_integrity_failed():
    assert "INTEGRITY_FAILED" in svc.EDITABLE_STATUSES


def test_audit_gate_uses_editable_statuses_not_a_narrower_literal():
    source = inspect.getsource(svc.audit_article_revision_job)
    assert "EDITABLE_STATUSES" in source
    assert '"UPLOADED", "ANALYZED", "REVIEW_REQUIRED", "READY_TO_FORMAT"}' not in source


def test_integrity_failed_job_can_be_reaudited(monkeypatch):
    class FakeTemplate:
        version = 1

    class FakeJob:
        status = "INTEGRITY_FAILED"
        template_id = "tpl-id"
        template = FakeTemplate()
        id = "job-id"

    class FakeUser:
        id = "user-id"

    monkeypatch.setattr(svc, "_job", lambda *a, **k: FakeJob())
    monkeypatch.setattr(svc, "_read_original", lambda *a, **k: b"orig")
    monkeypatch.setattr(svc, "authorized_article_template_bytes", lambda *a, **k: (None, b"tpl"))
    monkeypatch.setattr(svc, "article_template_config", lambda *a, **k: {"profile": {}, "rules": {}})
    monkeypatch.setattr(svc, "_compliance_findings", lambda *a, **k: [])
    monkeypatch.setattr(svc, "compliance_score", lambda findings: 0)
    monkeypatch.setattr(svc, "log_audit", lambda *a, **k: None)

    class FakeSession:
        def scalar(self, *a, **k):
            return None

        def commit(self):
            pass

    class FakeJournal:
        abbreviation = "ELKOLIND"
        id = "journal-id"

    raised = False
    try:
        svc.audit_article_revision_job(FakeSession(), FakeJournal(), FakeUser(), "job-id", storage=object())
    except svc.ArticleRevisionRuleError:
        raised = True
    assert not raised, "an INTEGRITY_FAILED job must be re-auditable, not permanently rejected"
