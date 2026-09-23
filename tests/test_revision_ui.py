"""Streamlit widget-level smoke test for the revision entry workflow."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from models import RevisionJob
from services.revision_analysis import analyze_manuscript
from services.revision_storage import LocalRevisionStorage
from test_revision import _manuscript, _pdf_review


def test_revision_view_is_not_a_blank_streamlit_auto_page():
    project_root = Path(__file__).resolve().parents[1]
    assert not (project_root / "pages" / "revision.py").exists()
    from revision_page import quick_revision_page
    assert callable(quick_revision_page)


def _revision_app(base: str) -> None:
    from pathlib import Path
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from models import Base, Journal, Role, Submission, User, UserJournal
    from revision_page import quick_revision_page

    engine = create_engine("sqlite:///" + (Path(base) / "ui.db").as_posix())
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        journal = session.scalar(select(Journal).where(Journal.abbreviation == "UIJ"))
        if journal is None:
            journal = Journal(name="UI Journal", abbreviation="UIJ")
            user = User(email="ui-editor@local.test", display_name="UI Editor", password_hash="x", role=Role.JOURNAL_ADMIN)
            session.add_all([journal, user])
            session.flush()
            session.add(UserJournal(user_id=user.id, journal_id=journal.id))
            session.add(Submission(journal_id=journal.id, ojs_submission_id="11397",
                manuscript_title="Controlled Revision of a Novel Method", corresponding_author="A. Author",
                email="a@local.test", created_by=user.id))
            session.commit()
        else:
            user = session.scalar(select(User).where(User.email == "ui-editor@local.test"))
        quick_revision_page(session, journal, user)


def test_streamlit_create_and_analyze_revision_job(tmp_path: Path, monkeypatch):
    import revision_page
    import services.revision_service as revision_service

    storage = LocalRevisionStorage(tmp_path / "private")
    monkeypatch.setattr(revision_page, "get_revision_storage", lambda: storage)
    monkeypatch.setattr(revision_service, "get_revision_storage", lambda: storage)
    app = AppTest.from_function(_revision_app, args=(str(tmp_path),), default_timeout=15).run()
    assert not app.exception
    app.text_input[0].set_value("Controlled Revision of a Novel Method")
    app.text_input[1].set_value("11397")
    app.file_uploader[0].set_value(("manuscript.docx", _manuscript(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    app.file_uploader[1].set_value(("reviewer1.txt", b"1. Clarify the novelty in the Introduction.", "text/plain"))
    app.file_uploader[2].set_value(("reviewer2.pdf", _pdf_review(), "application/pdf"))
    next(button for button in app.button if button.label == "Create revision job").click().run()
    assert not app.exception
    engine = create_engine("sqlite:///" + (tmp_path / "ui.db").as_posix())
    with Session(engine) as session:
        job = session.scalar(select(RevisionJob))
        assert job is not None and len(job.review_files) == 2
        job_id = job.id
    next(button for button in app.button if button.label == "Analyze Reviews").click().run()
    assert not app.exception
    with Session(engine) as session:
        job = session.get(RevisionJob, job_id)
        assert job.status == "EDITOR_REVIEW" and len(job.comments) == 2
        first_id, second_id = (comment.id for comment in job.comments)
    target = next(paragraph for paragraph in analyze_manuscript(_manuscript()).paragraphs
                  if paragraph.text.startswith("The novelty"))
    app.selectbox(f"rev-target-{first_id}").set_value(target.identifier)
    app.text_area(f"rev-proposal-{first_id}").set_value(
        target.text.replace("stated briefly", "explained in relation to prior work"))
    app.text_area(f"rev-reason-{first_id}").set_value("Clarifies the novelty")
    app.button(f"rev-save-{first_id}").click().run()
    assert not app.exception
    app.selectbox(f"rev-action-{second_id}").set_value("REJECTED")
    app.text_area(f"rev-reason-{second_id}").set_value("Duplicate of Reviewer 1's point")
    app.button(f"rev-save-{second_id}").click().run()
    assert not app.exception
    with Session(engine) as session:
        assert session.get(RevisionJob, job_id).status == "READY_TO_GENERATE"
    next(button for button in app.button if button.label.startswith("Generate Revised Manuscript")).click().run()
    assert not app.exception
    with Session(engine) as session:
        job = session.get(RevisionJob, job_id)
        assert job.status == "GENERATED" and len(job.artifacts) == 4
    next(button for button in app.button if button.label == "Mark revision complete after document inspection").click().run()
    assert not app.exception
    with Session(engine) as session:
        assert session.get(RevisionJob, job_id).status == "COMPLETED"
    engine.dispose()
