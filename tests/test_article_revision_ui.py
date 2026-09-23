"""Streamlit widget-level smoke test for the template-first editor workflow."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from models import ArticleRevisionJob
from services.revision_storage import LocalRevisionStorage
from test_article_revision import _elkolind_template, _template
from test_revision import _manuscript


def _article_app(folder: str, abbreviation: str = "UIARTICLE") -> None:
    from pathlib import Path
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from models import Base, Journal, Role, User, UserJournal
    from article_revision_page import article_templates_panel, quick_template_revision_page

    engine = create_engine("sqlite:///" + (Path(folder) / "article-ui.db").as_posix())
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        journal = session.scalar(select(Journal).where(Journal.abbreviation == abbreviation))
        if journal is None:
            journal = Journal(name="UI Article Journal", abbreviation=abbreviation)
            user = User(email="article-ui@local.test", display_name="UI Editor", password_hash="x", role=Role.JOURNAL_ADMIN)
            session.add_all([journal, user])
            session.flush()
            session.add(UserJournal(user_id=user.id, journal_id=journal.id))
            session.commit()
        else:
            user = session.scalar(select(User).where(User.email == "article-ui@local.test"))
        article_templates_panel(session, journal, user, lambda path, height=700: None)
        quick_template_revision_page(session, journal, user)
    engine.dispose()


def test_elkolind_margin_conflict_requires_explicit_admin_confirmation(tmp_path: Path, monkeypatch):
    import services.article_revision_service as revision_service
    import services.article_template_service as template_service

    storage = LocalRevisionStorage(tmp_path / "private")
    monkeypatch.setattr(revision_service, "get_revision_storage", lambda: storage)
    monkeypatch.setattr(template_service, "get_revision_storage", lambda: storage)
    app = AppTest.from_function(_article_app, args=(str(tmp_path), "ELKOLIND"), default_timeout=20).run()
    app.file_uploader[0].set_value(("ELKOLIND_article.docx", _elkolind_template(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    next(button for button in app.button if button.label == "Upload and activate Article Template").click().run()
    assert not app.exception
    assert any("14.32 mm" in message.value for message in app.warning)
    assert app.number_input[0].value != 14.32 or app.number_input[1].value != 14.32
    app.number_input[0].set_value(14.32)
    app.number_input[1].set_value(14.32)
    next(box for box in app.checkbox if "I compared the official DOCX" in box.label).check().run()
    next(button for button in app.button if button.label == "Save confirmed margins as new version").click().run()
    assert not app.exception
    engine = create_engine("sqlite:///" + (tmp_path / "article-ui.db").as_posix())
    with Session(engine) as session:
        from models import Journal
        from services.article_template_service import active_article_template, article_template_config
        journal = session.scalar(select(Journal).where(Journal.abbreviation == "ELKOLIND"))
        current = active_article_template(session, journal.id)
        assert current.version == 2
        assert article_template_config(current)["rules"]["left_margin_mm"] == 14.32
    engine.dispose()


def test_streamlit_article_template_upload_audit_approval_and_download(tmp_path: Path, monkeypatch):
    import services.article_revision_service as revision_service
    import services.article_template_service as template_service

    storage = LocalRevisionStorage(tmp_path / "private")
    monkeypatch.setattr(revision_service, "get_revision_storage", lambda: storage)
    monkeypatch.setattr(template_service, "get_revision_storage", lambda: storage)
    app = AppTest.from_function(_article_app, args=(str(tmp_path),), default_timeout=20).run()
    assert not app.exception
    app.file_uploader[0].set_value(("article-master.docx", _template(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    next(button for button in app.button if button.label == "Upload and activate Article Template").click().run()
    assert not app.exception
    assert "v1" in app.metric[2].value
    app.text_input[0].set_value("Controlled Revision of a Novel Method")
    app.text_input[1].set_value("11397")
    app.file_uploader[1].set_value(("author.docx", _manuscript(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    next(button for button in app.button if button.label == "Create formatting job").click().run()
    assert not app.exception
    next(radio for radio in app.radio if radio.label == "Formatting workflow").set_value("GUIDED_REVIEW").run()
    next(button for button in app.button if button.label == "Run Template Compliance Check").click().run()
    assert not app.exception
    assert all(any(button.label == label for button in app.button) for label in (
        "Apply Suggested Fix", "Keep As Is", "Mark False Positive", "Manual Review Completed"))
    next(button for button in app.button if button.label == "Apply All Safe Fixes").click().run()
    assert not app.exception
    engine = create_engine("sqlite:///" + (tmp_path / "article-ui.db").as_posix())
    with Session(engine) as session:
        from models import Journal, User
        from services.article_revision_service import job_findings, resolve_article_review_findings
        job = session.scalar(select(ArticleRevisionJob))
        journal = session.scalar(select(Journal).where(Journal.abbreviation == "UIARTICLE"))
        user = session.scalar(select(User).where(User.email == "article-ui@local.test"))
        reviews = {item.id: "KEEP_AS_IS" for item in job_findings(job)
                   if item.status == "REVIEW_REQUIRED"}
        assert reviews
        resolve_article_review_findings(session, journal, user, job.id, reviews)
    engine.dispose()
    app.run()
    next(button for button in app.button if button.label == "Generate Formatted Manuscript and Compliance Report").click().run()
    assert not app.exception
    engine = create_engine("sqlite:///" + (tmp_path / "article-ui.db").as_posix())
    with Session(engine) as session:
        job = session.scalar(select(ArticleRevisionJob))
        assert job is not None and job.status == "FORMATTED" and len(job.artifacts) == 2
        assert {item.filename for item in job.artifacts} == {
            "Formatted_Manuscript.docx", "Template_Compliance_Report.xlsx"}
    engine.dispose()


def test_streamlit_fast_auto_format_is_default_one_click_workflow(tmp_path: Path, monkeypatch):
    import services.article_revision_service as revision_service
    import services.article_template_service as template_service

    storage = LocalRevisionStorage(tmp_path / "private")
    monkeypatch.setattr(revision_service, "get_revision_storage", lambda: storage)
    monkeypatch.setattr(template_service, "get_revision_storage", lambda: storage)
    app = AppTest.from_function(_article_app, args=(str(tmp_path),), default_timeout=20).run()
    app.file_uploader[0].set_value(("article-master.docx", _template(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    next(button for button in app.button if button.label == "Upload and activate Article Template").click().run()
    app.text_input[0].set_value("Controlled Revision of a Novel Method")
    app.text_input[1].set_value("11397")
    app.file_uploader[1].set_value(("author.docx", _manuscript(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    next(button for button in app.button if button.label == "Create formatting job").click().run()
    assert not app.exception
    workflow = next(radio for radio in app.radio if radio.label == "Formatting workflow")
    assert workflow.value == "FAST_AUTO_FORMAT"
    assert any(button.label == "Generate While Preserving Protected Objects" for button in app.button)
    next(button for button in app.button if button.label == "Auto Format & Generate").click().run()
    assert not app.exception
    assert any(message.value.startswith("Formatting completed.") for message in app.success)
    engine = create_engine("sqlite:///" + (tmp_path / "article-ui.db").as_posix())
    with Session(engine) as session:
        job = session.scalar(select(ArticleRevisionJob))
        assert job is not None and job.status == "FORMATTED" and len(job.artifacts) == 2
    engine.dispose()
