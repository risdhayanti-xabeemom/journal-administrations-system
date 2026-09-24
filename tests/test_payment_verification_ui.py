"""Streamlit smoke tests for the manual editorial payment workflow."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from models import Invoice, InvoiceStatus, Payment, PaymentStatus, Receipt


def _payment_app(folder: str) -> None:
    from dataclasses import replace
    from datetime import date, timedelta
    from decimal import Decimal
    from pathlib import Path

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    import services.core as core
    from app import payment_verification_page
    from models import Base, EditorialStatus, Invoice, Journal, Role, Submission, User

    root = Path(folder)
    core.settings = replace(
        core.settings,
        document_dir=root / "documents",
        upload_dir=root / "private-uploads",
    )
    engine = create_engine("sqlite:///" + (root / "payment-ui.db").as_posix())
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        journal = session.scalar(select(Journal).where(Journal.abbreviation == "UIPAY"))
        if journal is None:
            journal = Journal(
                name="UI Payment Journal",
                abbreviation="UIPAY",
                currency="IDR",
                default_apc=Decimal("500000"),
            )
            user = User(
                email="payment-editor@local.test",
                display_name="Payment Editor",
                password_hash="x",
                role=Role.SUPER_ADMIN,
            )
            submission = Submission(
                journal=journal,
                ojs_submission_id="SUB-11398",
                manuscript_title="Manual Payment Workflow Article",
                corresponding_author="A. Author",
                email="author@local.test",
                editorial_status=EditorialStatus.ACCEPTED,
            )
            session.add_all([journal, user, submission])
            session.flush()
            core.issue_invoice(
                session,
                submission,
                user,
                due_date=date.today() + timedelta(days=14),
                apc=Decimal("500000"),
                discount=Decimal("0"),
                additional_charge=Decimal("0"),
                payment_method="Bank transfer",
                notes=None,
            )
            session.commit()
        else:
            user = session.scalar(select(User).where(User.email == "payment-editor@local.test"))
        payment_verification_page(session, journal, user)
    engine.dispose()


def test_manual_payment_empty_state_and_fast_receipt_workflow(tmp_path: Path):
    app = AppTest.from_function(_payment_app, args=(str(tmp_path),), default_timeout=25).run()
    assert not app.exception
    assert any(message.value == "No pending payment confirmations." for message in app.info)
    assert any(button.label == "Record Payment" for button in app.button)

    next(button for button in app.button if button.label == "Record Payment").click().run()
    assert not app.exception
    assert any(selectbox.label == "Invoice" for selectbox in app.selectbox)
    assert any(button.label == "Verify & Record Payment" for button in app.button)
    next(
        button
        for button in app.button
        if button.label == "Verify Payment & Generate Receipt"
    ).click().run()
    assert not app.exception
    assert any(button.label == "Confirm Payment & Generate Receipt" for button in app.button)

    next(
        button
        for button in app.button
        if button.label == "Confirm Payment & Generate Receipt"
    ).click().run()
    assert not app.exception
    assert any("Payment verified successfully" in message.value for message in app.success)

    engine = create_engine("sqlite:///" + (tmp_path / "payment-ui.db").as_posix())
    with Session(engine) as session:
        invoice = session.scalar(select(Invoice))
        payment = session.scalar(select(Payment))
        receipt = session.scalar(select(Receipt))
        assert invoice.status == InvoiceStatus.PAID
        assert payment.status == PaymentStatus.VERIFIED
        assert receipt is not None and receipt.payment_id == payment.id
    engine.dispose()
