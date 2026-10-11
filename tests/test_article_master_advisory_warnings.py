"""Advisory-only ELKOLIND Article Master notes must never block generation.

Regression for a bug where "No native PAGE field" and "No footer image" (both
informational: JAS adds its own PAGE field, and a missing barcode image only needs a
visual check) were treated the same as a structurally broken master, surfacing as a
DOCUMENT_CORRUPTION blocker that stopped Auto Format & Generate.
"""

from __future__ import annotations

import services.article_metadata as metadata

ADVISORY_ONLY = [
    "No native PAGE field was found; JAS will add one to each needed footer variant.",
    "No footer image was detected; verify the official barcode in visual preview.",
]


def test_advisory_only_master_notes_are_not_blockers(monkeypatch):
    monkeypatch.setattr(metadata, "elkolind_master_warnings", lambda content: list(ADVISORY_ONLY))
    assert metadata.elkolind_master_blockers(b"") == []


def test_a_real_structural_problem_still_blocks(monkeypatch):
    broken = ADVISORY_ONLY + ["The default header needs the ELKOLIND dynamic placeholders."]
    monkeypatch.setattr(metadata, "elkolind_master_warnings", lambda content: list(broken))
    assert metadata.elkolind_master_blockers(b"") == ["The default header needs the ELKOLIND dynamic placeholders."]


def test_compliance_finding_is_compliant_with_only_advisory_notes(monkeypatch):
    import services.article_revision_service as svc

    monkeypatch.setattr(svc, "elkolind_master_warnings", lambda content: list(ADVISORY_ONLY))
    monkeypatch.setattr(svc, "elkolind_master_blockers", lambda content: [])
    monkeypatch.setattr(svc, "audit_article", lambda *a, **k: [])
    findings = svc._compliance_findings(b"man", b"tpl", {"profile": {}, "rules": {"header_footer_mode": "MASTER"}},
                                        elkolind=True)
    master_finding = next(item for item in findings if item.check == "ELKOLIND Article Master header/footer")
    assert master_finding.status == "COMPLIANT"
    assert svc.finding_blocker_code(master_finding) is None
    assert "No native PAGE field" in master_finding.detected


def test_compliance_finding_still_blocks_a_real_structural_problem(monkeypatch):
    import services.article_revision_service as svc

    broken = ["The default header needs the ELKOLIND dynamic placeholders."]
    monkeypatch.setattr(svc, "elkolind_master_warnings", lambda content: list(broken))
    monkeypatch.setattr(svc, "elkolind_master_blockers", lambda content: list(broken))
    monkeypatch.setattr(svc, "audit_article", lambda *a, **k: [])
    findings = svc._compliance_findings(b"man", b"tpl", {"profile": {}, "rules": {"header_footer_mode": "MASTER"}},
                                        elkolind=True)
    master_finding = next(item for item in findings if item.check == "ELKOLIND Article Master header/footer")
    assert master_finding.status == "MANUAL_ACTION_REQUIRED"
    assert svc.finding_blocker_code(master_finding) == "DOCUMENT_CORRUPTION"
