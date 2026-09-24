from __future__ import annotations

import re
import uuid
import json
import base64
import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from config import settings
from models import (
    AuditLog,
    Author,
    DocumentVerification,
    DocumentTemplate,
    EditorialStatus,
    Invoice,
    InvoiceStatus,
    Journal,
    LoADocument,
    LoAStatus,
    Payment,
    PaymentStatus,
    PublicationStatus,
    Receipt,
    Role,
    Submission,
    User,
    UserJournal,
)
from services.core import (
    AuthorizationError,
    BusinessRuleError,
    authenticate,
    assert_journal_access,
    author_payment_url,
    bootstrap_admin,
    cancel_invoice,
    export_workbook,
    find_invoice_by_author_token,
    global_submission_search,
    hash_password,
    issue_invoice,
    issue_loa,
    issue_receipt,
    generate_loa_preview,
    journals_for_user,
    log_audit,
    money,
    record_manual_payment,
    reject_payment,
    revoke_loa,
    submit_payment,
    store_private_upload,
    update_editorial_status,
    update_publication_status,
    verify_payment,
    verification_url,
)
from services.public_verification import (
    public_amounts_enabled,
    public_document_verification,
    public_document_verification_by_number,
    set_public_amount_visibility,
)
from services.verification_admin import generate_missing_verification_tokens, verification_qr_png
from services.docx_templates import PDFConversionError, PDFConverterUnavailable, TemplateError
from services.template_service import (
    activate_loa_template,
    active_loa_template,
    preview_master_template,
    save_field_mapping,
    upload_loa_template,
)
from services.database import SessionLocal, init_database
from revision_page import quick_revision_page, revision_jobs_page, revision_history_page
from article_revision_page import article_templates_panel, quick_template_revision_page, revision_jobs_hub, revision_history_hub
from services.ojs_import import (
    FIELD_ALIASES,
    REQUIRED_FIELDS,
    ImportFileError,
    build_preview,
    confirm_import,
    detect_columns,
    error_report_csv,
    metadata_flag,
    preview_table,
    read_import_file,
)


st.set_page_config(page_title="Journal Administration System", page_icon="📚", layout="wide")
st.markdown(
    """
    <style>
    .block-container {padding-top: 1.5rem; padding-bottom: 3rem;}
    [data-testid="stMetric"] {background:#f8fafc;border:1px solid #e2e8f0;padding:14px;border-radius:12px;}
    [data-testid="stSidebar"] {border-right:1px solid #e2e8f0;}
    .jas-eyebrow {color:#2563eb;font-weight:700;letter-spacing:.08em;font-size:.78rem;text-transform:uppercase;}
    .jas-muted {color:#64748b;font-size:.9rem;}
    .jas-valid {padding:18px;border:1px solid #86efac;background:#f0fdf4;border-radius:12px;}
    .jas-invalid {padding:18px;border:1px solid #fca5a5;background:#fef2f2;border-radius:12px;}
    </style>
    """,
    unsafe_allow_html=True,
)


def notice_error(exc: Exception) -> None:
    if isinstance(exc, (BusinessRuleError, AuthorizationError, ValueError, IntegrityError, PDFConversionError, PDFConverterUnavailable, TemplateError)):
        st.error(str(exc.orig) if isinstance(exc, IntegrityError) else str(exc))
        retained = getattr(exc, "docx_path", None)
        if retained and Path(retained).is_file():
            st.download_button(
                "Download retained DOCX",
                Path(retained).read_bytes(),
                file_name=Path(retained).name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
    else:
        st.error("The operation could not be completed. Check the server log.")


def show_pdf(path: str | Path, *, height: int = 900) -> None:
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    components.html(
        f'<embed src="data:application/pdf;base64,{data}" type="application/pdf" width="100%" height="{height}px" />',
        height=height + 20,
        scrolling=True,
    )


def route_token(kind: str) -> str | None:
    try:
        query_value = st.query_params.get(kind)
        if query_value:
            return str(query_value)
    except Exception:
        pass
    try:
        path = urlparse(str(st.context.url)).path
        match = re.search(rf"/{kind}/([^/?#]+)", path)
        return match.group(1) if match else None
    except Exception:
        return None


def render_verification(token: str) -> None:
    st.markdown('<div class="jas-eyebrow">Journal Administration System</div>', unsafe_allow_html=True)
    st.title("DOCUMENT VERIFICATION")
    try:
        with SessionLocal() as session:
            data = public_document_verification(session, token)
    except Exception:
        data = None
    if not data:
        st.markdown(
            '<div class="jas-invalid"><b>Document verification failed.</b><br/>'
            'The verification code is invalid or the document is not registered in JAS.</div>',
            unsafe_allow_html=True,
        )
        return
    valid = data["document_status"] == "VALID"
    css = "jas-valid" if valid else "jas-invalid"
    heading = "DOCUMENT VALID" if valid else f'DOCUMENT {data["document_status"]}'
    st.markdown(f'<div class="{css}"><b>{heading}</b></div>', unsafe_allow_html=True)
    rows = [
        ("Document Type", {"LOA": "Letter of Acceptance", "INVOICE": "Invoice", "RECEIPT": "Receipt"}.get(data["document_type"], data["document_type"])),
        ("Document Number", data["document_number"]),
        ("Journal", data["journal"]),
        ("Submission ID", data["submission_id"]),
        ("Article Title", data["article_title"]),
        ("Author / Corresponding Author", data["author"]),
        ("Issue Date", data["issue_date"]),
        ("Document Status", data["document_status"]),
    ]
    if data["document_type"] == "LOA":
        rows.append(("LoA Status", data.get("loa_status") or "Not recorded"))
        rows.append(("Acceptance Status", data.get("acceptance_status") or "Not recorded"))
    elif data["document_type"] == "INVOICE":
        rows.append(("Invoice Status", data.get("invoice_status") or "Not recorded"))
    elif data["document_type"] == "RECEIPT" and data.get("payment_status"):
        rows.append(("Payment Status", data["payment_status"]))
    if data.get("amount"):
        rows.append(("Amount", data["amount"]))
    st.table(pd.DataFrame(rows, columns=["Field", "Value"]).set_index("Field"))


def render_author_payment(token: str) -> None:
    st.markdown('<div class="jas-eyebrow">Secure author payment confirmation</div>', unsafe_allow_html=True)
    st.title("Payment confirmation")
    with SessionLocal() as session:
        invoice = find_invoice_by_author_token(session, token)
        if not invoice:
            st.error("This secure link is invalid or expired.")
            return
        st.info(f"{invoice.journal.name} · Invoice {invoice.invoice_number}")
        st.subheader(invoice.submission.manuscript_title)
        col1, col2 = st.columns(2)
        col1.metric("Total payment", money(invoice.total_amount, invoice.currency))
        col2.metric("Current status", invoice.status.value.replace("_", " ").title())
        st.write("**Payment instructions**")
        st.write(f"Method: {invoice.payment_method or 'Contact the journal administration'}")
        st.write(f"Bank: {invoice.journal.bank_name or 'Not configured'}")
        st.write(f"Account: {invoice.journal.bank_account or 'Not configured'}")
        st.write(f"Account holder: {invoice.journal.account_holder or 'Not configured'}")
        existing = session.scalar(select(Payment).where(Payment.invoice_id == invoice.id).order_by(Payment.submitted_at.desc()))
        if existing:
            st.caption(f"Latest confirmation: {existing.status.value} ({existing.submitted_at:%Y-%m-%d %H:%M})")
            if existing.status == PaymentStatus.REJECTED and existing.internal_notes:
                st.warning(f"Correction requested: {existing.internal_notes}")
        if invoice.status in {InvoiceStatus.PAID, InvoiceStatus.CANCELLED}:
            st.success("No further payment confirmation is required.") if invoice.status == InvoiceStatus.PAID else st.error("This invoice has been cancelled.")
            return
        with st.form("author-payment-form", clear_on_submit=False):
            payer = st.text_input("Payer name")
            method = st.text_input("Bank / payment method")
            paid_date = st.date_input("Payment date", value=date.today(), max_value=date.today())
            amount = st.number_input("Amount paid", min_value=0.0, value=float(invoice.total_amount), step=1000.0)
            proof = st.file_uploader("Proof of payment (PDF, PNG, JPG; max 5 MB)", type=["pdf", "png", "jpg", "jpeg"])
            notes = st.text_area("Optional notes")
            sent = st.form_submit_button("Submit payment confirmation", type="primary", use_container_width=True)
        if sent:
            if not payer.strip() or not method.strip() or proof is None:
                st.error("Payer name, payment method, and proof are required.")
                return
            try:
                submit_payment(
                    session,
                    invoice,
                    payer_name=payer,
                    payment_method=method,
                    payment_date=paid_date,
                    amount_paid=Decimal(str(amount)),
                    upload_name=proof.name,
                    upload_content=proof.getvalue(),
                    notes=notes or None,
                )
                session.commit()
                st.success("Payment confirmation submitted for verification.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                notice_error(exc)


def render_login() -> None:
    left, center, right = st.columns([1, 1.1, 1])
    with center:
        st.markdown('<div class="jas-eyebrow">Academic journal operations</div>', unsafe_allow_html=True)
        st.title("Journal Administration System")
        st.caption("Sign in with an administrator-issued account.")
        with SessionLocal() as session:
            user_count = session.scalar(select(func.count(User.id))) or 0
        if user_count == 0:
            st.error("No administrator account exists. Configure ADMIN_EMAIL and ADMIN_PASSWORD, then restart the app or run scripts/init_db.py.")
            st.stop()
        with st.form("login"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if submitted:
            with SessionLocal() as session:
                user = authenticate(session, email, password)
                if user:
                    session.commit()
                    st.session_state.user_id = str(user.id)
                    st.session_state.auth_started_at = datetime.now(timezone.utc).isoformat()
                    st.rerun()
                st.error("Invalid email or password.")
        st.divider()
        with st.expander("Verify Document"):
            with st.form("public-document-number-verification"):
                lookup_number = st.text_input("Document Number", placeholder="058/SK/ELK/VIII/2026")
                lookup = st.form_submit_button("Verify Document")
            if lookup:
                try:
                    with SessionLocal() as session:
                        data = public_document_verification_by_number(session, lookup_number)
                except Exception:
                    data = None
                if not data:
                    st.error("Document verification failed. The document number is invalid or is not registered in JAS.")
                else:
                    valid = data["document_status"] == "VALID"
                    st.success("DOCUMENT VALID") if valid else st.error(f'DOCUMENT {data["document_status"]}')
                    st.table(pd.DataFrame([
                        ("Document type", data["document_type"]),
                        ("Journal", data["journal"]),
                        ("Document number", data["document_number"]),
                        ("Submission ID", data["submission_id"]),
                        ("Article title", data["article_title"]),
                        ("Author", data["author"]),
                        ("Issue date", data["issue_date"]),
                        ("Status", data["document_status"]),
                        *([("Amount", data["amount"])] if data.get("amount") else []),
                    ], columns=["Field", "Value"]).set_index("Field"))


def submission_rows(items: list[Submission]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "OJS ID": item.ojs_submission_id or "Manual",
                "Title": item.manuscript_title,
                "Corresponding author": item.corresponding_author,
                "Metadata": "INCOMPLETE_METADATA" if metadata_flag(item.notes) else "READY",
                "Editorial": item.editorial_status.value,
                "Financial": next((inv.status.value for inv in sorted(item_invoices(item), key=lambda x: x.created_at, reverse=True)), "NOT_INVOICED"),
                "Publication": item.publication_status.value,
                "Volume": item.planned_volume,
                "Issue": item.planned_issue,
                "Month": item.planned_publication_month,
                "Year": item.planned_year,
            }
            for item in items
        ]
    )


def item_invoices(item: Submission) -> list[Invoice]:
    return list(getattr(item, "_ui_invoices", []))


def dashboard(session, journal: Journal) -> None:
    st.title("Dashboard")
    submissions = list(session.scalars(select(Submission).where(Submission.journal_id == journal.id)))
    invoices = list(session.scalars(select(Invoice).where(Invoice.journal_id == journal.id)))
    payments = list(session.scalars(select(Payment).join(Invoice).where(Invoice.journal_id == journal.id)))
    loas = list(session.scalars(select(LoADocument).where(LoADocument.journal_id == journal.id)))
    receipts = list(session.scalars(select(Receipt).where(Receipt.journal_id == journal.id)))
    metrics = [
        ("Total submissions", len(submissions)),
        ("Accepted manuscripts", sum(x.editorial_status == EditorialStatus.ACCEPTED for x in submissions)),
        ("LoAs issued", len(loas)),
        ("Invoices issued", len(invoices)),
        ("Waiting payments", sum(x.status == InvoiceStatus.WAITING_PAYMENT for x in invoices)),
        ("Payments submitted", sum(x.status == PaymentStatus.SUBMITTED for x in payments)),
        ("Verified payments", sum(x.status == PaymentStatus.VERIFIED for x in payments)),
        ("Receipts issued", len(receipts)),
        ("Ready for publication", sum(x.publication_status == PublicationStatus.READY_FOR_PUBLICATION for x in submissions)),
        ("Published articles", sum(x.publication_status == PublicationStatus.PUBLISHED for x in submissions)),
    ]
    for start in range(0, len(metrics), 5):
        for column, (label, value) in zip(st.columns(5), metrics[start:start + 5]):
            column.metric(label, value)
    st.subheader("Financial overview")
    total_invoiced = sum((Decimal(x.total_amount) for x in invoices if x.status != InvoiceStatus.CANCELLED), Decimal("0"))
    total_paid = sum((Decimal(x.amount_paid) for x in payments if x.status == PaymentStatus.VERIFIED), Decimal("0"))
    month_start = date.today().replace(day=1)
    monthly_paid = sum((Decimal(x.amount_paid) for x in payments if x.status == PaymentStatus.VERIFIED and x.payment_date >= month_start), Decimal("0"))
    for column, (label, value) in zip(st.columns(4), [("Total invoiced", total_invoiced), ("Total paid", total_paid), ("Outstanding", max(total_invoiced - total_paid, 0)), ("Current month revenue", monthly_paid)]):
        column.metric(label, money(value, journal.currency))
    if invoices:
        chart = pd.DataFrame([{"Month": x.invoice_date.strftime("%Y-%m"), "Amount": float(x.total_amount)} for x in invoices if x.status != InvoiceStatus.CANCELLED]).groupby("Month", as_index=False).sum()
        st.subheader("Monthly invoices")
        st.bar_chart(chart, x="Month", y="Amount", use_container_width=True)
    status_frame = pd.DataFrame([x.status.value for x in invoices], columns=["Status"])
    if not status_frame.empty:
        st.subheader("Invoice status distribution")
        st.bar_chart(status_frame.value_counts().rename("Count"))


def all_submissions(session, journal: Journal, user: User) -> None:
    st.title("All submissions")
    search = st.text_input("Search by ID, title, author, email, document number, or DOI")
    col1, col2, col3 = st.columns(3)
    editorial = col1.selectbox("Editorial status", ["ALL"] + [x.value for x in EditorialStatus])
    year = col2.text_input("Planned year")
    issue = col3.text_input("Issue")
    if search.strip():
        items = global_submission_search(session, [journal.id], search)
    else:
        statement = select(Submission).where(Submission.journal_id == journal.id).order_by(Submission.created_at.desc())
        if editorial != "ALL":
            statement = statement.where(Submission.editorial_status == EditorialStatus(editorial))
        if year.strip().isdigit():
            statement = statement.where(Submission.planned_year == int(year))
        if issue.strip():
            statement = statement.where(Submission.planned_issue == issue.strip())
        items = list(session.scalars(statement))
    invoices = list(session.scalars(select(Invoice).where(Invoice.journal_id == journal.id)))
    by_submission: dict[uuid.UUID, list[Invoice]] = {}
    for inv in invoices:
        by_submission.setdefault(inv.submission_id, []).append(inv)
    for item in items:
        item._ui_invoices = by_submission.get(item.id, [])
    st.dataframe(submission_rows(items), use_container_width=True, hide_index=True)
    if not items:
        return
    selected_id = st.selectbox("Select submission to update", [str(x.id) for x in items], format_func=lambda value: next(f"{x.ojs_submission_id or 'Manual'} · {x.manuscript_title[:80]}" for x in items if str(x.id) == value))
    selected = next(x for x in items if str(x.id) == selected_id)
    with st.expander("Editorial and metadata update", expanded=False):
        st.caption("Editorial state is independent from payment and publication state.")
        target = st.selectbox("Editorial status", [x.value for x in EditorialStatus], index=list(EditorialStatus).index(selected.editorial_status))
        volume = st.text_input("Planned volume", value=selected.planned_volume or "")
        planned_issue = st.text_input("Planned issue", value=selected.planned_issue or "")
        planned_month = st.text_input("Publication month", value=selected.planned_publication_month or journal.default_publication_month or "")
        planned_year = st.number_input("Planned year", min_value=0, max_value=2200, value=selected.planned_year or 0)
        doi = st.text_input("DOI", value=selected.doi or "")
        article_url = st.text_input("Article URL", value=selected.article_url or "")
        if st.button("Save submission changes", type="primary"):
            try:
                update_editorial_status(session, selected, EditorialStatus(target), user)
                before = {"volume": selected.planned_volume, "issue": selected.planned_issue, "month": selected.planned_publication_month, "year": selected.planned_year, "doi": selected.doi, "article_url": selected.article_url}
                selected.planned_volume = volume or None
                selected.planned_issue = planned_issue or None
                selected.planned_publication_month = planned_month or None
                selected.planned_year = int(planned_year) or None
                selected.doi = doi or None
                selected.article_url = article_url or None
                log_audit(session, action="SUBMISSION_CHANGED", object_type="submission", object_id=selected.id, user_id=user.id, journal_id=journal.id, previous=before, new={"volume": selected.planned_volume, "issue": selected.planned_issue, "month": selected.planned_publication_month, "year": selected.planned_year, "doi": selected.doi, "article_url": selected.article_url})
                session.commit()
                st.success("Submission updated.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                notice_error(exc)


def add_submission(session, journal: Journal, user: User) -> None:
    st.title("Add submission")
    try:
        assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    except AuthorizationError as exc:
        st.error(str(exc))
        return
    with st.form("add-submission"):
        ojs_id = st.text_input("OJS Submission ID (optional for manual entry)")
        title = st.text_area("Manuscript title")
        corresponding = st.text_input("Corresponding author")
        email = st.text_input("Email")
        affiliation = st.text_area("Affiliation")
        submitted_date = st.date_input("Date submitted", value=date.today())
        col1, col2, col3, col4 = st.columns(4)
        planned_volume = col1.text_input("Target volume", value=journal.default_volume or "")
        planned_issue = col2.text_input("Target issue", value=journal.default_issue or "")
        planned_month = col3.text_input("Publication month", value=journal.default_publication_month or "")
        planned_year = col4.number_input("Publication year", min_value=0, max_value=2200, value=journal.default_publication_year or date.today().year)
        authors = st.text_area(
            "Authors in publication order (one per line)",
            help="Format: Name | Affiliation | Email. Simple name-only lines are also accepted. The corresponding author is never alphabetically reordered.",
        )
        notes = st.text_area("Internal notes")
        saved = st.form_submit_button("Add submission", type="primary")
    if saved:
        if not title.strip() or not corresponding.strip() or "@" not in email:
            st.error("Title, corresponding author, and a valid email are required.")
            return
        try:
            item = Submission(journal_id=journal.id, ojs_submission_id=ojs_id.strip() or None, manuscript_title=title.strip(), corresponding_author=corresponding.strip(), email=email.strip().lower(), affiliation=affiliation.strip() or None, date_submitted=submitted_date, planned_volume=planned_volume.strip() or None, planned_issue=planned_issue.strip() or None, planned_publication_month=planned_month.strip() or None, planned_year=int(planned_year) or None, notes=notes.strip() or None, created_by=user.id)
            session.add(item)
            session.flush()
            author_rows = []
            for line in authors.splitlines():
                parts = [part.strip() for part in line.split("|")]
                if parts and parts[0]:
                    author_rows.append((parts[0], parts[1] if len(parts) > 1 and parts[1] else None, parts[2] if len(parts) > 2 and parts[2] else None))
            if corresponding.strip() not in [row[0] for row in author_rows]:
                author_rows.append((corresponding.strip(), affiliation.strip() or None, email.strip().lower()))
            for position, (name, author_affiliation, author_email) in enumerate(author_rows, 1):
                is_corresponding = name == corresponding.strip()
                session.add(Author(submission_id=item.id, name=name, email=(email.strip().lower() if is_corresponding else author_email), affiliation=(affiliation.strip() or None) if is_corresponding else author_affiliation, is_corresponding=is_corresponding, position=position))
            log_audit(session, action="SUBMISSION_CREATED", object_type="submission", object_id=item.id, user_id=user.id, journal_id=journal.id, new={"ojs_submission_id": item.ojs_submission_id, "title": item.manuscript_title})
            session.commit()
            st.success("Submission added.")
        except Exception as exc:
            session.rollback()
            notice_error(exc)


def import_submissions(session, journal: Journal, user: User) -> None:
    st.title("Import submissions")
    try:
        assert_journal_access(user, journal.id, {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}, session)
    except AuthorizationError as exc:
        st.error(str(exc))
        return
    st.caption("Required data: OJS Submission ID and Manuscript Title. Other metadata can be completed after import.")
    uploaded = st.file_uploader("OJS CSV or XLSX report", type=["csv", "xlsx"])
    if not uploaded:
        return
    try:
        content = uploaded.getvalue()
        table = read_import_file(uploaded.name, content)
    except ImportFileError as exc:
        st.error(str(exc))
        return
    file_key = hashlib.sha256(content).hexdigest()[:16]
    detected, warnings = detect_columns(table.headers)
    st.subheader("OJS Column Mapping")
    st.caption("Automatic matches are preselected. Review all fields; ambiguous columns remain unselected.")
    mapping: dict[str, int | None] = {}
    field_labels = {
        "ojs_submission_id": "OJS Submission ID",
        "manuscript_title": "Manuscript Title",
        "authors": "Authors",
        "corresponding_author": "Corresponding Author",
        "email": "Email",
        "affiliation": "Affiliation",
        "editorial_status": "Editorial Status",
        "date_submitted": "Date Submitted",
        "date_accepted": "Date Accepted",
        "planned_volume": "Volume",
        "planned_issue": "Issue",
        "planned_publication_month": "Publication Month",
        "planned_year": "Publication Year",
        "notes": "Notes",
    }
    for field in FIELD_ALIASES:
        label_col, input_col = st.columns([2, 3])
        label_col.write(field_labels[field] + (" *required*" if field in REQUIRED_FIELDS else ""))
        options = [None, *range(len(table.headers))]
        mapping[field] = input_col.selectbox(
            field_labels[field], options, index=options.index(detected[field]),
            format_func=lambda index: "— Not mapped —" if index is None else f"{table.headers[index]} (column {index + 1})",
            key=f"ojs-map-{journal.id}-{file_key}-{field}", label_visibility="collapsed",
        )
        if field in warnings:
            st.caption(f"{field_labels[field]}: {warnings[field]}")
    if any(mapping[field] is None for field in REQUIRED_FIELDS):
        st.error("Select the OJS ID and manuscript title columns to continue.")
        return
    policy = st.radio("Existing submissions", ["Skip existing", "Update existing metadata"], horizontal=True,
                      help="Updates only unprotected descriptive metadata. Accepted, publication, LoA, invoice, and payment information is never overwritten.")
    try:
        preview = build_preview(session, journal, table, mapping, update_existing=policy == "Update existing metadata")
    except ValueError as exc:
        st.error(str(exc))
        return
    st.subheader("Import preview — no database changes yet")
    st.dataframe(preview_table(preview), use_container_width=True, hide_index=True)
    if not preview:
        st.warning("No data rows found in this report.")
        return
    if st.button("Confirm Import", type="primary"):
        try:
            result = confirm_import(session, journal, user, table, mapping, uploaded.name,
                                    update_existing=policy == "Update existing metadata")
            st.session_state[f"ojs-import-result-{journal.id}-{file_key}"] = result
        except Exception as exc:
            session.rollback()
            notice_error(exc)
    result = st.session_state.get(f"ojs-import-result-{journal.id}-{file_key}")
    if result:
        st.success(f"Imported: {result.imported} · Skipped duplicates: {result.skipped_duplicates} · "
                   f"Updated: {result.updated} · Incomplete: {result.incomplete} · Invalid: {result.invalid}")
        st.download_button("Download import error report", error_report_csv(result),
                           file_name="ojs_import_errors.csv", mime="text/csv")


def loa_page(session, journal: Journal, user: User) -> None:
    st.title("Letters of Acceptance")
    accepted = list(session.scalars(select(Submission).where(Submission.journal_id == journal.id, Submission.editorial_status == EditorialStatus.ACCEPTED).order_by(Submission.date_accepted.desc())))
    if accepted:
        sid = st.selectbox("Accepted submission", [str(x.id) for x in accepted], format_func=lambda value: next(f"{x.ojs_submission_id or 'Manual'} · {x.manuscript_title[:90]}" for x in accepted if str(x.id) == value))
        submission = next(x for x in accepted if str(x.id) == sid)
        current = session.scalar(select(LoADocument).where(LoADocument.submission_id == submission.id, LoADocument.status == LoAStatus.VALID).order_by(LoADocument.version.desc()))
        st.table(pd.DataFrame([
            ("Journal", journal.abbreviation),
            ("OJS Submission ID", submission.ojs_submission_id or "Manual entry"),
            ("Article title", submission.manuscript_title),
            ("Authors", ", ".join(author.name for author in submission.authors) or submission.corresponding_author),
            ("Corresponding author", submission.corresponding_author),
            ("Affiliation", submission.affiliation or "-"),
            ("Acceptance date", submission.date_accepted or "-"),
            ("Planned volume", submission.planned_volume or journal.default_volume or "-"),
            ("Planned issue", submission.planned_issue or journal.default_issue or "-"),
            ("Publication month", submission.planned_publication_month or journal.default_publication_month or "-"),
            ("Publication year", submission.planned_year or journal.default_publication_year or "-"),
        ], columns=["Field", "Value"]).set_index("Field"))
        st.caption("A preview is required. Previewing does not allocate a LoA number or create a VALID verification record.")
        col1, col2, col3, col4, col5 = st.columns(5)
        volume = col1.text_input("Volume", value=submission.planned_volume or journal.default_volume or "", key=f"loa-volume-{submission.id}")
        issue = col2.text_input("Issue", value=submission.planned_issue or journal.default_issue or "", key=f"loa-issue-{submission.id}")
        month = col3.text_input("Publication month", value=submission.planned_publication_month or journal.default_publication_month or "", key=f"loa-month-{submission.id}")
        year = col4.number_input("Publication year", min_value=1900, max_value=2200, value=submission.planned_year or journal.default_publication_year or date.today().year, key=f"loa-year-{submission.id}")
        printed_date = col5.date_input("Printed LoA date", value=date.today(), key=f"loa-date-{submission.id}")
        overrides = {"volume": volume or "-", "issue": issue or "-", "publication_month": month or "-", "publication_year": int(year)}
        preview_key = f"loa-preview-{journal.id}"
        if st.button("Generate Preview", type="primary", key=f"preview-{submission.id}"):
            try:
                preview = generate_loa_preview(session, submission, user, printed_date=printed_date, overrides=overrides)
                st.session_state[preview_key] = {
                    "submission_id": str(submission.id),
                    "docx_path": str(preview.docx_path),
                    "pdf_path": str(preview.pdf_path) if preview.pdf_path else None,
                    "page_count": preview.page_count,
                    "printed_date": printed_date.isoformat(),
                    "overrides": overrides,
                }
                st.rerun()
            except Exception as exc:
                session.rollback()
                notice_error(exc)
        preview_state = st.session_state.get(preview_key)
        if preview_state and preview_state.get("submission_id") == str(submission.id):
            st.warning("DRAFT PREVIEW — NOT ISSUED")
            if preview_state.get("page_count", 0) > 1:
                st.error(f"Overflow warning: this preview is {preview_state['page_count']} pages. Review every page before issuing.")
            preview_docx = Path(preview_state["docx_path"])
            preview_pdf = Path(preview_state["pdf_path"]) if preview_state.get("pdf_path") else None
            downloads = st.columns(2)
            if preview_docx.is_file():
                downloads[0].download_button("Download preview DOCX", preview_docx.read_bytes(), file_name=f"DRAFT-{journal.abbreviation}-LoA.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            if preview_pdf and preview_pdf.is_file():
                downloads[1].download_button("Download preview PDF", preview_pdf.read_bytes(), file_name=f"DRAFT-{journal.abbreviation}-LoA.pdf", mime="application/pdf")
                show_pdf(preview_pdf)
            overflow_allowed = False
            if preview_state.get("page_count", 0) > 1:
                overflow_allowed = st.checkbox("I reviewed every preview page and explicitly approve multi-page issuance.")
            reason = st.text_area("Reason for reissue (required)", key=f"reissue-reason-{submission.id}") if current else None
            label = "Reissue LoA" if current else "Issue LoA"
            can_issue = (preview_state.get("page_count", 0) <= 1 or overflow_allowed) and (not current or bool((reason or "").strip()))
            if st.button(label, type="primary", disabled=not can_issue, key=f"issue-{submission.id}"):
                try:
                    issued = issue_loa(
                        session,
                        submission,
                        user,
                        reissue=bool(current),
                        printed_date=date.fromisoformat(preview_state["printed_date"]),
                        reissue_reason=reason,
                        allow_overflow=overflow_allowed,
                        overrides=preview_state["overrides"],
                    )
                    session.commit()
                    st.session_state.pop(preview_key, None)
                    st.success(f"LoA {issued.document_number} issued.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)
    else:
        st.info("No ACCEPTED submissions are available.")
    documents = list(session.scalars(select(LoADocument).where(LoADocument.journal_id == journal.id).order_by(LoADocument.created_at.desc())))
    st.subheader("Document history")
    for document in documents:
        with st.container(border=True):
            cols = st.columns([3, 1, 1, 1, 1])
            cols[0].write(f"**{document.document_number}**  \n{document.submission.manuscript_title}")
            cols[1].write(f"Version {document.version}")
            cols[2].write(document.status.value)
            if document.docx_path and Path(document.docx_path).is_file():
                cols[3].download_button("DOCX", Path(document.docx_path).read_bytes(), file_name=f"{document.document_number.replace('/', '-')}.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", key=f"loa-docx-{document.id}")
            if document.pdf_path and Path(document.pdf_path).is_file():
                cols[4].download_button("PDF", Path(document.pdf_path).read_bytes(), file_name=f"{document.document_number.replace('/', '-')}.pdf", mime="application/pdf", key=f"loa-dl-{document.id}")
            if document.reissue_reason:
                st.caption(f"Reissue reason: {document.reissue_reason}")
            if document.status == LoAStatus.VALID and st.button("Revoke", key=f"loa-revoke-{document.id}"):
                try:
                    revoke_loa(session, document, user)
                    session.commit()
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)


def invoices_page(session, journal: Journal, user: User) -> None:
    st.title("Invoices")
    accepted = list(session.scalars(select(Submission).where(Submission.journal_id == journal.id, Submission.editorial_status == EditorialStatus.ACCEPTED).order_by(Submission.created_at.desc())))
    if accepted and user.role in {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}:
        with st.expander("Generate invoice", expanded=False):
            sid = st.selectbox("Submission", [str(x.id) for x in accepted], format_func=lambda value: next(f"{x.ojs_submission_id or 'Manual'} · {x.manuscript_title[:90]}" for x in accepted if str(x.id) == value), key="invoice-submission")
            submission = next(x for x in accepted if str(x.id) == sid)
            with st.form("invoice-form"):
                due = st.date_input("Due date", value=date.today() + timedelta(days=14), min_value=date.today())
                apc = st.number_input("Publication fee / APC", min_value=0.0, value=float(journal.default_apc), step=1000.0)
                discount = st.number_input("Discount", min_value=0.0, value=0.0, step=1000.0)
                charge = st.number_input("Additional charge", min_value=0.0, value=0.0, step=1000.0)
                method = st.text_input("Payment method", value="Bank transfer")
                notes = st.text_area("Notes")
                created = st.form_submit_button("Issue invoice", type="primary")
            if created:
                try:
                    invoice, raw_token = issue_invoice(session, submission, user, due_date=due, apc=Decimal(str(apc)), discount=Decimal(str(discount)), additional_charge=Decimal(str(charge)), payment_method=method or None, notes=notes or None)
                    session.commit()
                    st.session_state.new_author_link = author_payment_url(raw_token)
                    st.success(f"Invoice {invoice.invoice_number} issued.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)
    if link := st.session_state.pop("new_author_link", None):
        st.success("Secure author link created. Copy it now; the raw token is not stored.")
        st.code(link)
    invoices = list(session.scalars(select(Invoice).where(Invoice.journal_id == journal.id).order_by(Invoice.created_at.desc())))
    for invoice in invoices:
        with st.container(border=True):
            cols = st.columns([3, 1, 1, 1])
            cols[0].write(f"**{invoice.invoice_number}**  \n{invoice.submission.manuscript_title}")
            cols[1].write(money(invoice.total_amount, invoice.currency))
            cols[2].write(invoice.status.value)
            if invoice.pdf_path and Path(invoice.pdf_path).is_file():
                cols[3].download_button("PDF", Path(invoice.pdf_path).read_bytes(), file_name=f"{invoice.invoice_number.replace('/', '-')}.pdf", mime="application/pdf", key=f"inv-dl-{invoice.id}")
            if invoice.status not in {InvoiceStatus.PAID, InvoiceStatus.CANCELLED} and st.button("Cancel invoice", key=f"inv-cancel-{invoice.id}"):
                try:
                    cancel_invoice(session, invoice, user)
                    session.commit()
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)


def payment_verification_page(session, journal: Journal, user: User) -> None:
    st.title("Payment verification")

    success = st.session_state.pop("manual_payment_success", None)
    if success:
        st.success(success["message"])
        if success.get("receipt_id"):
            receipt = session.get(Receipt, uuid.UUID(success["receipt_id"]))
            if receipt and receipt.pdf_path and Path(receipt.pdf_path).is_file():
                st.download_button(
                    "Download Receipt",
                    Path(receipt.pdf_path).read_bytes(),
                    file_name=f"{receipt.receipt_number.replace('/', '-')}.pdf",
                    mime="application/pdf",
                    key=f"manual-success-receipt-{receipt.id}",
                )
        elif success.get("payment_id") and st.button("Create Receipt", type="primary", key="manual-success-create-receipt"):
            payment = session.get(Payment, uuid.UUID(success["payment_id"]))
            if payment:
                try:
                    receipt = issue_receipt(session, payment, user)
                    session.commit()
                    st.session_state.manual_payment_success = {
                        "message": "Payment verified successfully. Receipt generated.",
                        "payment_id": str(payment.id),
                        "receipt_id": str(receipt.id),
                    }
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)

    top_left, top_right = st.columns([1, 4])
    if top_left.button("Record Payment", type="primary", use_container_width=True, key="open-manual-payment"):
        st.session_state.manual_payment_open = True
        st.session_state.pop("manual_payment_confirmation", None)
        st.rerun()
    top_right.caption("Record and verify payments received directly by the editorial team. Author-submitted confirmations remain available below.")

    all_payments = list(
        session.scalars(
            select(Payment)
            .join(Invoice)
            .where(Invoice.journal_id == journal.id)
            .order_by(Payment.submitted_at.desc())
        )
    )
    blocked_invoice_ids = {
        payment.invoice_id
        for payment in all_payments
        if payment.status in {PaymentStatus.SUBMITTED, PaymentStatus.VERIFIED}
    }
    eligible_invoices = [
        invoice
        for invoice in session.scalars(
            select(Invoice)
            .where(
                Invoice.journal_id == journal.id,
                Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.WAITING_PAYMENT]),
            )
            .order_by(Invoice.created_at.desc())
        )
        if invoice.id not in blocked_invoice_ids
    ]

    confirmation = st.session_state.get("manual_payment_confirmation")
    if confirmation:
        invoice = session.get(Invoice, uuid.UUID(confirmation["invoice_id"]))
        if invoice is None or invoice.journal_id != journal.id:
            st.session_state.pop("manual_payment_confirmation", None)
            st.error("The selected invoice is no longer available.")
        else:
            with st.container(border=True):
                st.subheader("Confirm Manual Payment")
                st.write(f"**Invoice:** {invoice.invoice_number}")
                st.write(f"**Amount:** {money(Decimal(confirmation['amount_paid']), invoice.currency)}")
                st.write(f"**Payment Date:** {confirmation['payment_date']}")
                st.write(f"**Method:** {confirmation['payment_method']}")
                amount_received = Decimal(confirmation["amount_paid"])
                invoice_amount = Decimal(invoice.total_amount)
                underpayment = amount_received < invoice_amount
                if underpayment:
                    st.warning("Amount received is lower than invoice amount.")
                    underpayment_confirmed = st.checkbox(
                        "I explicitly confirm this underpayment may mark the invoice as paid.",
                        key="manual-underpayment-confirmed",
                    )
                else:
                    underpayment_confirmed = True
                if amount_received > invoice_amount:
                    st.warning("Amount received is higher than invoice amount. The invoice amount will not be changed.")
                confirm_col, cancel_col = st.columns(2)
                confirm_label = (
                    "Confirm Payment & Generate Receipt"
                    if confirmation.get("generate_receipt")
                    else "Confirm Payment"
                )
                if confirm_col.button(
                    confirm_label,
                    type="primary",
                    disabled=not underpayment_confirmed,
                    use_container_width=True,
                    key="confirm-manual-payment",
                ):
                    try:
                        payment = record_manual_payment(
                            session,
                            invoice,
                            user,
                            payment_date=date.fromisoformat(confirmation["payment_date"]),
                            payment_method=confirmation["payment_method"],
                            amount_paid=amount_received,
                            payment_reference=confirmation.get("payment_reference"),
                            notes=confirmation.get("notes"),
                            upload_name=confirmation.get("upload_name"),
                            upload_content=confirmation.get("upload_content"),
                            confirm_underpayment=underpayment_confirmed,
                        )
                        session.commit()
                        receipt = None
                        receipt_error = None
                        if confirmation.get("generate_receipt"):
                            try:
                                receipt = issue_receipt(session, payment, user)
                                session.commit()
                            except Exception:
                                session.rollback()
                                receipt_error = "Receipt generation failed; use Create Receipt to retry."
                        message = "Payment verified successfully."
                        if receipt:
                            message += " Receipt generated."
                        elif receipt_error:
                            message += f" {receipt_error}"
                        st.session_state.manual_payment_success = {
                            "message": message,
                            "payment_id": str(payment.id),
                            "receipt_id": str(receipt.id) if receipt else None,
                        }
                        st.session_state.pop("manual_payment_confirmation", None)
                        st.session_state.manual_payment_open = False
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        notice_error(exc)
                if cancel_col.button("Back to Form", use_container_width=True, key="cancel-manual-confirmation"):
                    st.session_state.pop("manual_payment_confirmation", None)
                    st.rerun()

    elif st.session_state.get("manual_payment_open"):
        with st.container(border=True):
            st.subheader("Record Payment")
            if not eligible_invoices:
                st.info("No issued or unpaid invoices are available for manual payment recording.")
            else:
                selected_invoice_id = st.selectbox(
                    "Invoice",
                    [str(invoice.id) for invoice in eligible_invoices],
                    format_func=lambda value: next(
                        (
                            f"{invoice.invoice_number} · {invoice.submission.ojs_submission_id or 'Manual'} · "
                            f"{invoice.submission.manuscript_title[:70]} · {invoice.submission.corresponding_author} · "
                            f"{money(invoice.total_amount, invoice.currency)}"
                        )
                        for invoice in eligible_invoices
                        if str(invoice.id) == value
                    ),
                    key="manual-payment-invoice",
                )
                selected_invoice = next(
                    invoice for invoice in eligible_invoices if str(invoice.id) == selected_invoice_id
                )
                details = pd.DataFrame(
                    [
                        ("Invoice Number", selected_invoice.invoice_number),
                        ("Submission ID", selected_invoice.submission.ojs_submission_id or "Manual"),
                        ("Article Title", selected_invoice.submission.manuscript_title),
                        ("Corresponding Author", selected_invoice.submission.corresponding_author),
                        ("Invoice Amount", money(selected_invoice.total_amount, selected_invoice.currency)),
                    ],
                    columns=["Field", "Value"],
                ).set_index("Field")
                st.table(details)
                with st.form("manual-payment-form"):
                    paid_date = st.date_input("Payment Date", value=date.today(), max_value=date.today())
                    method_choice = st.selectbox("Payment Method", ["Bank Transfer", "QRIS", "Cash", "Other"])
                    other_method = st.text_input("Other payment method (complete only when Other is selected)")
                    payment_reference = st.text_input("Payment Reference / Transfer Reference")
                    amount_received = st.number_input(
                        "Amount Received",
                        min_value=0.0,
                        value=float(selected_invoice.total_amount),
                        step=1000.0,
                    )
                    payment_notes = st.text_area("Payment Notes")
                    payment_proof = st.file_uploader(
                        "Payment Proof (optional)",
                        type=["pdf", "jpg", "jpeg", "png"],
                    )
                    action_col, fast_col = st.columns(2)
                    record_only = action_col.form_submit_button(
                        "Verify & Record Payment",
                        use_container_width=True,
                    )
                    record_and_receipt = fast_col.form_submit_button(
                        "Verify Payment & Generate Receipt",
                        type="primary",
                        use_container_width=True,
                    )
                if record_only or record_and_receipt:
                    payment_method = other_method.strip() if method_choice == "Other" else method_choice
                    if not payment_method:
                        st.error("Payment Method is required.")
                    elif Decimal(str(amount_received)) > Decimal(selected_invoice.total_amount) and not payment_notes.strip():
                        st.error("Amount received is higher than invoice amount. Add a payment note before continuing.")
                    else:
                        st.session_state.manual_payment_confirmation = {
                            "invoice_id": str(selected_invoice.id),
                            "payment_date": paid_date.isoformat(),
                            "payment_method": payment_method,
                            "payment_reference": payment_reference.strip() or None,
                            "amount_paid": str(Decimal(str(amount_received))),
                            "notes": payment_notes.strip() or None,
                            "upload_name": payment_proof.name if payment_proof else None,
                            "upload_content": payment_proof.getvalue() if payment_proof else None,
                            "generate_receipt": bool(record_and_receipt),
                        }
                        st.rerun()
            if st.button("Close", key="close-manual-payment"):
                st.session_state.manual_payment_open = False
                st.session_state.pop("manual_payment_confirmation", None)
                st.rerun()

    pending = [payment for payment in all_payments if payment.status == PaymentStatus.SUBMITTED]
    st.subheader("Pending Verification")
    if not pending:
        st.info("No pending payment confirmations.")
        st.caption("If payment was received manually, use Record Payment to verify an issued invoice.")
        if st.button("Record Payment", key="empty-record-payment"):
            st.session_state.manual_payment_open = True
            st.rerun()
    for payment in pending:
        with st.container(border=True):
            st.write(f"**{payment.invoice.invoice_number} · {payment.payer_name}**")
            st.caption(f"{payment.payment_date} · {money(payment.amount_paid, payment.invoice.currency)} · {payment.status.value}")
            if payment.proof_path and Path(payment.proof_path).is_file():
                mime = mimetype_for(payment.proof_original_name)
                st.download_button("View/download payment proof", Path(payment.proof_path).read_bytes(), file_name=payment.proof_original_name, mime=mime, key=f"proof-{payment.id}")
            if payment.author_notes:
                st.write(f"Author note: {payment.author_notes}")
            note = st.text_area("Internal note", key=f"pay-note-{payment.id}")
            col1, col2, col3 = st.columns(3)
            if col1.button("Verify", type="primary", key=f"verify-{payment.id}"):
                try:
                    verify_payment(session, payment, user, note or None)
                    session.commit()
                    st.session_state.manual_payment_success = {
                        "message": "Payment verified successfully.",
                        "payment_id": str(payment.id),
                        "receipt_id": None,
                    }
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)
            if col2.button("Verify & Generate Receipt", key=f"verify-receipt-{payment.id}"):
                try:
                    verify_payment(session, payment, user, note or None)
                    session.commit()
                    receipt = issue_receipt(session, payment, user)
                    session.commit()
                    st.session_state.manual_payment_success = {
                        "message": "Payment verified successfully. Receipt generated.",
                        "payment_id": str(payment.id),
                        "receipt_id": str(receipt.id),
                    }
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)
            if col3.button("Reject / request correction", key=f"reject-{payment.id}"):
                try:
                    reject_payment(session, payment, user, note)
                    session.commit()
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)

    verified = [payment for payment in all_payments if payment.status == PaymentStatus.VERIFIED]
    receipt_by_payment = {
        receipt.payment_id: receipt
        for receipt in session.scalars(
            select(Receipt).where(Receipt.journal_id == journal.id).order_by(Receipt.created_at.desc())
        )
    }
    st.subheader("Verified Payments")
    if not verified:
        st.caption("No verified payments yet.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Payment Date": payment.payment_date,
                        "Invoice Number": payment.invoice.invoice_number,
                        "Submission ID": payment.invoice.submission.ojs_submission_id or "Manual",
                        "Author": payment.payer_name,
                        "Amount": money(payment.amount_paid, payment.invoice.currency),
                        "Method": payment.payment_method,
                        "Status": payment.status.value,
                        "Receipt Status": "GENERATED" if payment.id in receipt_by_payment else "NOT GENERATED",
                    }
                    for payment in verified
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
        action_payment_id = st.selectbox(
            "Payment actions",
            [str(payment.id) for payment in verified],
            format_func=lambda value: next(
                f"{payment.invoice.invoice_number} · {payment.payer_name} · {money(payment.amount_paid, payment.invoice.currency)}"
                for payment in verified
                if str(payment.id) == value
            ),
            key="verified-payment-actions",
        )
        action_payment = next(payment for payment in verified if str(payment.id) == action_payment_id)
        action_receipt = receipt_by_payment.get(action_payment.id)
        view_col, invoice_col, receipt_col = st.columns(3)
        if view_col.button("View", use_container_width=True, key="view-verified-payment"):
            st.session_state.payment_view_id = str(action_payment.id)
        if invoice_col.button(
            "Open Invoice",
            use_container_width=True,
            disabled=not (action_payment.invoice.pdf_path and Path(action_payment.invoice.pdf_path).is_file()),
            key="open-verified-invoice",
        ):
            st.session_state.payment_invoice_preview = str(action_payment.invoice.id)
        if action_receipt:
            if receipt_col.button(
                "Open Receipt",
                use_container_width=True,
                disabled=not (action_receipt.pdf_path and Path(action_receipt.pdf_path).is_file()),
                key="open-verified-receipt",
            ):
                st.session_state.payment_receipt_preview = str(action_receipt.id)
        elif receipt_col.button("Generate Receipt", type="primary", use_container_width=True, key="generate-verified-receipt"):
            try:
                receipt = issue_receipt(session, action_payment, user)
                session.commit()
                st.session_state.manual_payment_success = {
                    "message": "Receipt generated.",
                    "payment_id": str(action_payment.id),
                    "receipt_id": str(receipt.id),
                }
                st.rerun()
            except Exception as exc:
                session.rollback()
                notice_error(exc)

        if st.session_state.get("payment_view_id") == str(action_payment.id):
            with st.container(border=True):
                st.write(f"**{action_payment.invoice.invoice_number} · {action_payment.payer_name}**")
                st.write(f"Payment date: {action_payment.payment_date}")
                st.write(f"Method: {action_payment.payment_method}")
                st.write(f"Amount received: {money(action_payment.amount_paid, action_payment.invoice.currency)}")
                if action_payment.internal_notes:
                    st.write(f"Notes: {action_payment.internal_notes}")
                if action_payment.proof_path and Path(action_payment.proof_path).is_file():
                    st.download_button(
                        "View/download payment proof",
                        Path(action_payment.proof_path).read_bytes(),
                        file_name=action_payment.proof_original_name,
                        mime=mimetype_for(action_payment.proof_original_name),
                        key=f"verified-proof-{action_payment.id}",
                    )
        if (
            st.session_state.get("payment_invoice_preview") == str(action_payment.invoice.id)
            and action_payment.invoice.pdf_path
            and Path(action_payment.invoice.pdf_path).is_file()
        ):
            show_pdf(action_payment.invoice.pdf_path, height=700)
        if (
            action_receipt
            and st.session_state.get("payment_receipt_preview") == str(action_receipt.id)
            and action_receipt.pdf_path
            and Path(action_receipt.pdf_path).is_file()
        ):
            show_pdf(action_receipt.pdf_path, height=700)

    rejected = [payment for payment in all_payments if payment.status == PaymentStatus.REJECTED]
    if rejected:
        with st.expander(f"Rejected confirmations ({len(rejected)})"):
            for payment in rejected:
                st.write(
                    f"{payment.invoice.invoice_number} · {payment.payer_name} · "
                    f"{money(payment.amount_paid, payment.invoice.currency)}"
                )
                if payment.internal_notes:
                    st.caption(payment.internal_notes)


def mimetype_for(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(suffix, "application/octet-stream")


def receipts_page(session, journal: Journal, user: User) -> None:
    st.title("Receipts")
    eligible = list(session.scalars(select(Payment).join(Invoice).outerjoin(Receipt, Receipt.payment_id == Payment.id).where(Invoice.journal_id == journal.id, Payment.status == PaymentStatus.VERIFIED, Receipt.id.is_(None)).order_by(Payment.verified_at.desc())))
    if eligible:
        pid = st.selectbox("Verified payment awaiting receipt", [str(x.id) for x in eligible], format_func=lambda value: next(f"{x.invoice.invoice_number} · {x.payer_name} · {money(x.amount_paid, x.invoice.currency)}" for x in eligible if str(x.id) == value))
        payment = next(x for x in eligible if str(x.id) == pid)
        if st.button("Generate receipt", type="primary"):
            try:
                issue_receipt(session, payment, user)
                session.commit()
                st.success("Receipt generated.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                notice_error(exc)
    receipts = list(session.scalars(select(Receipt).where(Receipt.journal_id == journal.id).order_by(Receipt.created_at.desc())))
    for receipt in receipts:
        with st.container(border=True):
            cols = st.columns([3, 1, 1])
            cols[0].write(f"**{receipt.receipt_number}**  \n{receipt.invoice.submission.manuscript_title}")
            cols[1].write(money(receipt.amount, receipt.invoice.currency))
            if receipt.pdf_path and Path(receipt.pdf_path).is_file():
                cols[2].download_button("PDF", Path(receipt.pdf_path).read_bytes(), file_name=f"{receipt.receipt_number.replace('/', '-')}.pdf", mime="application/pdf", key=f"receipt-{receipt.id}")


def issue_monitor_page(session, journal: Journal) -> None:
    st.title("Issue monitor")
    col1, col2, col3 = st.columns(3)
    volume = col1.text_input("Volume")
    issue = col2.text_input("Issue")
    year = col3.number_input("Year", min_value=0, max_value=2200, value=date.today().year)
    statement = select(Submission).where(Submission.journal_id == journal.id)
    if volume:
        statement = statement.where(Submission.planned_volume == volume)
    if issue:
        statement = statement.where(Submission.planned_issue == issue)
    if year:
        statement = statement.where(Submission.planned_year == int(year))
    submissions = list(session.scalars(statement.order_by(Submission.manuscript_title)))
    loas = list(session.scalars(select(LoADocument).where(LoADocument.journal_id == journal.id, LoADocument.status == LoAStatus.VALID)))
    invoices = list(session.scalars(select(Invoice).where(Invoice.journal_id == journal.id, Invoice.status != InvoiceStatus.CANCELLED)))
    receipts = list(session.scalars(select(Receipt).where(Receipt.journal_id == journal.id)))
    for item in submissions:
        has_loa = any(x.submission_id == item.id for x in loas)
        item_invs = [x for x in invoices if x.submission_id == item.id]
        paid = any(x.status == InvoiceStatus.PAID for x in item_invs)
        has_receipt = any(x.invoice_id in {inv.id for inv in item_invs} for x in receipts)
        checks = [has_loa, bool(item_invs), paid, has_receipt, bool(item.doi), item.publication_status == PublicationStatus.PUBLISHED]
        with st.container(border=True):
            st.write(f"**{item.manuscript_title}**")
            st.caption(" · ".join(f"{name} {'✓' if ok else '–'}" for name, ok in zip(["LoA", "Invoice", "Payment", "Receipt", "DOI", "Published"], checks)))
            st.progress(sum(checks) / len(checks), text=f"{sum(checks)}/{len(checks)} administrative requirements complete")


def publication_page(session, journal: Journal, user: User) -> None:
    st.title("Publication tracking")
    submissions = list(session.scalars(select(Submission).where(Submission.journal_id == journal.id).order_by(Submission.updated_at.desc())))
    for item in submissions:
        with st.container(border=True):
            st.write(f"**{item.manuscript_title}**")
            st.caption(f"Editorial: {item.editorial_status.value} · Publication: {item.publication_status.value} · DOI: {item.doi or '-'}")
            allowed = {
                PublicationStatus.NOT_READY: [PublicationStatus.NOT_READY, PublicationStatus.READY_FOR_PUBLICATION],
                PublicationStatus.READY_FOR_PUBLICATION: [PublicationStatus.READY_FOR_PUBLICATION, PublicationStatus.NOT_READY, PublicationStatus.PUBLISHED],
                PublicationStatus.PUBLISHED: [PublicationStatus.PUBLISHED],
            }[item.publication_status]
            target = st.selectbox("Next publication status", [x.value for x in allowed], index=0, key=f"pub-{item.id}")
            if target != item.publication_status.value and st.button("Update", key=f"pub-btn-{item.id}"):
                try:
                    update_publication_status(session, item, PublicationStatus(target), user)
                    session.commit()
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)


def reports_page(session, journal: Journal) -> None:
    st.title("Export data")
    st.write("The workbook includes Submission, LoA, Invoice, Payment, Receipt, and Issue Administration reports.")
    if st.button("Prepare Excel report", type="primary"):
        try:
            st.session_state.report_bytes = export_workbook(session, [journal.id])
        except Exception as exc:
            notice_error(exc)
    if data := st.session_state.get("report_bytes"):
        st.download_button("Download Excel workbook", data, file_name=f"JAS-{journal.abbreviation}-{date.today().isoformat()}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def journals_page(session, selected: Journal, user: User) -> None:
    st.title("Journal settings")
    if user.role != Role.SUPER_ADMIN:
        st.warning("Only Super Admin can edit journal configuration.")
        return
    active_template = active_loa_template(session, selected.id)
    public_amount_visibility = public_amounts_enabled(session, selected.id)
    with st.form("journal-settings"):
        name = st.text_input("Journal name", value=selected.name)
        abbreviation = st.text_input("Abbreviation", value=selected.abbreviation)
        issn = st.text_input("ISSN", value=selected.issn or "")
        e_issn = st.text_input("e-ISSN", value=selected.e_issn or "")
        publisher = st.text_input("Publisher", value=selected.publisher or "")
        website = st.text_input("Website", value=selected.website or "")
        address = st.text_area("Address", value=selected.address or "")
        contact = st.text_input("Contact email", value=selected.contact_email or "")
        editor = st.text_input("Editor-in-Chief", value=selected.editor_in_chief or "")
        currency = st.text_input("Currency (ISO 4217)", value=selected.currency, max_chars=3)
        apc = st.number_input("Default APC", min_value=0.0, value=float(selected.default_apc), step=1000.0)
        bank = st.text_input("Bank name", value=selected.bank_name or "")
        account = st.text_input("Bank account", value=selected.bank_account or "")
        holder = st.text_input("Account holder", value=selected.account_holder or "")
        st.subheader("Document Settings")
        st.text_input("LoA master template", value=(f"v{active_template.version} · {active_template.original_filename}" if active_template else "NOT CONFIGURED"), disabled=True)
        loa_format = st.text_input("LoA numbering pattern", value=selected.loa_number_format, help="Available fields: {sequence:03d}, {journal}, {roman_month}, {month}, {year}")
        qr_enabled = st.checkbox("Verification QR on LoA", value=selected.loa_qr_enabled)
        qr_placement = st.selectbox("LoA QR placement", ["template-placeholder", "bottom-right", "bottom-left", "top-right", "top-left"], index=["template-placeholder", "bottom-right", "bottom-left", "top-right", "top-left"].index(selected.loa_qr_placement if selected.loa_qr_placement in {"template-placeholder", "bottom-right", "bottom-left", "top-right", "top-left"} else "bottom-right"), disabled=not qr_enabled, help="Use template-placeholder for the safest layout. Corner placement is absolute and must be checked in preview so it never covers text, signatures, stamps, or logos.")
        qr_size = st.number_input("LoA QR size (mm)", min_value=10, max_value=40, value=selected.loa_qr_size_mm, disabled=not qr_enabled)
        show_public_amounts = st.checkbox(
            "Show invoice/receipt amount on public verification page",
            value=public_amount_visibility,
            help="Off by default. Email, bank details, payment proof, and internal notes are never exposed.",
        )
        invoice_format = st.text_input("Invoice numbering pattern", value=selected.invoice_number_format)
        receipt_format = st.text_input("Receipt numbering pattern", value=selected.receipt_number_format)
        st.subheader("Publication Settings")
        default_volume = st.text_input("Default volume", value=selected.default_volume or "")
        default_issue = st.text_input("Default issue", value=selected.default_issue or "")
        default_month = st.text_input("Default publication month", value=selected.default_publication_month or "")
        default_year = st.number_input("Default publication year", min_value=0, max_value=2200, value=selected.default_publication_year or 0)
        st.subheader("Journal assets")
        logo_upload = st.file_uploader("Logo (PNG/JPG)", type=["png", "jpg", "jpeg"], key="journal-logo")
        signature_upload = st.file_uploader("Editor signature (PNG/JPG)", type=["png", "jpg", "jpeg"], key="journal-signature")
        stamp_upload = st.file_uploader("Stamp / seal (PNG/JPG)", type=["png", "jpg", "jpeg"], key="journal-stamp")
        qris_upload = st.file_uploader("QRIS image (PNG/JPG)", type=["png", "jpg", "jpeg"], key="journal-qris")
        saved = st.form_submit_button("Save journal settings", type="primary")
    if saved:
        if not name.strip() or not abbreviation.strip() or len(currency.strip()) != 3:
            st.error("Name, abbreviation, and three-letter currency are required.")
            return
        before = {"name": selected.name, "abbreviation": selected.abbreviation}
        selected.name, selected.abbreviation = name.strip(), abbreviation.strip().upper()
        selected.issn, selected.e_issn, selected.publisher, selected.website = issn or None, e_issn or None, publisher or None, website or None
        selected.address, selected.contact_email, selected.editor_in_chief = address or None, contact or None, editor or None
        selected.currency, selected.default_apc = currency.strip().upper(), Decimal(str(apc))
        selected.bank_name, selected.bank_account, selected.account_holder = bank or None, account or None, holder or None
        selected.loa_number_format, selected.invoice_number_format, selected.receipt_number_format = loa_format, invoice_format, receipt_format
        selected.loa_qr_enabled, selected.loa_qr_placement, selected.loa_qr_size_mm = qr_enabled, qr_placement, int(qr_size)
        set_public_amount_visibility(session, selected.id, show_public_amounts)
        selected.default_volume, selected.default_issue = default_volume or None, default_issue or None
        selected.default_publication_month, selected.default_publication_year = default_month or None, int(default_year) or None
        for upload, attribute in [
            (logo_upload, "logo_path"),
            (signature_upload, "signature_path"),
            (stamp_upload, "stamp_path"),
            (qris_upload, "qris_path"),
        ]:
            if upload is not None:
                setattr(selected, attribute, store_private_upload(upload.name, upload.getvalue()))
        log_audit(session, action="JOURNAL_SETTINGS_CHANGED", object_type="journal", object_id=selected.id, user_id=user.id, journal_id=selected.id, previous=before, new={"name": selected.name, "abbreviation": selected.abbreviation, "public_verification_show_amount": show_public_amounts})
        try:
            session.commit()
            st.success("Journal settings saved.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            notice_error(exc)


def users_page(session, journal: Journal, user: User) -> None:
    st.title("Users")
    if user.role != Role.SUPER_ADMIN:
        st.warning("Only Super Admin can manage users.")
        return
    users = list(session.scalars(select(User).order_by(User.email)))
    st.dataframe(pd.DataFrame([{"Name": x.display_name, "Email": x.email, "Role": x.role.value, "Active": x.active} for x in users]), hide_index=True, use_container_width=True)
    with st.expander("Add user"):
        with st.form("add-user"):
            name = st.text_input("Display name")
            email = st.text_input("Email")
            password = st.text_input("Initial password", type="password", help="Minimum 12 characters")
            role = st.selectbox("Role", [x.value for x in Role if x != Role.SUPER_ADMIN])
            assigned = st.multiselect("Assigned journals", [str(x.id) for x in session.scalars(select(Journal).order_by(Journal.abbreviation))], format_func=lambda value: session.get(Journal, uuid.UUID(value)).abbreviation)
            created = st.form_submit_button("Create user", type="primary")
        if created:
            try:
                new_user = User(email=email.strip().lower(), display_name=name.strip(), password_hash=hash_password(password), role=Role(role))
                session.add(new_user)
                session.flush()
                for journal_id in assigned:
                    session.add(UserJournal(user_id=new_user.id, journal_id=uuid.UUID(journal_id)))
                log_audit(session, action="USER_CREATED", object_type="user", object_id=new_user.id, user_id=user.id, journal_id=journal.id, new={"email": new_user.email, "role": new_user.role.value})
                session.commit()
                st.success("User created.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                notice_error(exc)


def templates_page(session, journal: Journal, user: User) -> None:
    st.title("Document templates")
    if user.role not in {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}:
        st.warning("Only Super Admin or Journal Admin can edit templates.")
        return
    article_templates_panel(session, journal, user, show_pdf)
    if user.role != Role.SUPER_ADMIN:
        st.caption("LoA master template and invoice/receipt text remain restricted to Super Admin.")
        return
    st.divider()
    st.subheader("Letter of Acceptance Master Template")
    current = active_loa_template(session, journal.id)
    details = st.columns(3)
    details[0].metric("Journal", journal.abbreviation)
    details[1].metric("Template Status", "ACTIVE" if current else "NOT CONFIGURED")
    details[2].metric("Template version", f"v{current.version}" if current else "-")
    if current:
        st.table(pd.DataFrame([
            ("Current LoA Template", current.original_filename),
            ("Last updated", current.uploaded_at),
            ("Updated by", current.uploader.display_name if current.uploader else "System / deleted user"),
            ("Checksum", current.checksum),
        ], columns=["Field", "Value"]).set_index("Field"))
        actions = st.columns(3)
        actions[0].download_button(
            "Download Current Template",
            Path(current.storage_path).read_bytes(),
            file_name=current.original_filename,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        if actions[1].button("Preview Template"):
            try:
                preview = preview_master_template(session, current, user)
                session.commit()
                st.session_state[f"template-preview-{journal.id}"] = str(preview)
                st.rerun()
            except Exception as exc:
                session.rollback()
                notice_error(exc)
        if actions[2].button("Configure Field Mapping"):
            st.session_state[f"show-mapping-{journal.id}"] = True
        preview_path = st.session_state.get(f"template-preview-{journal.id}") or current.preview_pdf_path
        if preview_path and Path(preview_path).is_file():
            st.info("Template preview: inspect the logo, header, footer, signature/stamp, and indexing logos.")
            show_pdf(preview_path)
        if st.session_state.get(f"show-mapping-{journal.id}"):
            mapping_text = st.text_area(
                "Placeholder → data-source mapping (JSON)",
                value=current.field_mapping or "{}",
                height=240,
                key=f"mapping-{current.id}",
            )
            if st.button("Save Field Mapping", type="primary"):
                try:
                    save_field_mapping(session, current, user, mapping_text)
                    session.commit()
                    st.success("Field mapping saved.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    notice_error(exc)
    uploaded = st.file_uploader(
        "Upload Master DOCX" if not current else "Replace Template",
        type=["docx"],
        key=f"loa-template-upload-{journal.id}",
        help="The uploaded file becomes a new version. Previous versions are retained.",
    )
    if uploaded and st.button("Upload and activate", type="primary"):
        try:
            template = upload_loa_template(
                session,
                journal,
                user,
                original_filename=uploaded.name,
                content=uploaded.getvalue(),
                activate=True,
            )
            session.commit()
            st.success(f"Template v{template.version} uploaded and activated.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            notice_error(exc)
    versions = list(session.scalars(select(DocumentTemplate).where(
        DocumentTemplate.journal_id == journal.id,
        DocumentTemplate.template_type == "LOA",
    ).order_by(DocumentTemplate.version.desc())))
    if versions:
        with st.expander("Template version history / Restore Previous Version"):
            for template in versions:
                cols = st.columns([3, 1, 1, 1])
                cols[0].write(f"**v{template.version} · {template.original_filename}**  \n{template.uploaded_at}")
                cols[1].write(template.status.value)
                cols[2].download_button("DOCX", Path(template.storage_path).read_bytes(), file_name=template.original_filename, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", key=f"template-dl-{template.id}")
                if template.status.value != "ACTIVE" and cols[3].button("Restore", key=f"restore-template-{template.id}"):
                    try:
                        activate_loa_template(session, template, user)
                        session.commit()
                        st.success(f"Template v{template.version} restored.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        notice_error(exc)
    st.divider()
    st.subheader("Invoice and receipt text")
    st.caption("Invoice and receipt remain independent from the official LoA Master DOCX architecture.")
    with st.form("invoice-receipt-templates"):
        invoice = st.text_area("Invoice custom text", value=journal.invoice_template or "", height=140)
        receipt = st.text_area("Receipt custom text", value=journal.receipt_template or "", height=140)
        saved = st.form_submit_button("Save invoice and receipt text", type="primary")
    if saved:
        journal.invoice_template, journal.receipt_template = invoice or None, receipt or None
        log_audit(session, action="INVOICE_RECEIPT_TEMPLATES_CHANGED", object_type="journal", object_id=journal.id, user_id=user.id, journal_id=journal.id)
        session.commit()
        st.success("Invoice and receipt text saved.")


def audit_page(session, journal: Journal) -> None:
    st.title("Audit log")
    logs = list(session.scalars(select(AuditLog).where(AuditLog.journal_id == journal.id).order_by(AuditLog.created_at.desc()).limit(1000)))
    st.dataframe(pd.DataFrame([{"Timestamp": x.created_at, "Action": x.action, "Object": f"{x.object_type}:{x.object_id}", "User ID": str(x.user_id or "System"), "Previous": x.previous_value, "New": x.new_value} for x in logs]), use_container_width=True, hide_index=True)


def document_verification_page(session, journal: Journal, user: User) -> None:
    st.title("Document Verification")
    st.caption("Verification links identify documents issued by JAS. Payment QRIS remains a separate payment function.")
    if user.role in {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}:
        if st.button("Generate Missing Verification Token"):
            try:
                repaired = generate_missing_verification_tokens(
                    session,
                    journal=journal,
                    user=user,
                )
                session.commit()
                if repaired:
                    st.success(f"Generated {len(repaired)} missing verification token(s). Existing tokens were unchanged.")
                    st.rerun()
                else:
                    st.info("No missing verification tokens were found. Existing tokens were unchanged.")
            except Exception as exc:
                session.rollback()
                notice_error(exc)
    records = list(
        session.scalars(
            select(DocumentVerification)
            .where(DocumentVerification.journal_id == journal.id)
            .order_by(DocumentVerification.created_at.desc())
        )
    )
    if not records:
        st.info("No verification records are registered for this journal yet.")
        return
    rows = []
    for record in records:
        configured = bool((record.token or "").strip())
        rows.append(
            {
                "Document": record.document_type.value,
                "Document Number": record.document_number,
                "Verification Token Status": "CONFIGURED" if configured else "MISSING",
                "Verification URL": verification_url(record.token) if configured else "Not available",
                "Document Status": record.document_status.value,
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    selected_id = st.selectbox(
        "Document",
        [str(record.id) for record in records],
        format_func=lambda value: next(
            f"{record.document_type.value} · {record.document_number} · {record.document_status.value}"
            for record in records
            if str(record.id) == value
        ),
        key=f"verification-record-{journal.id}",
    )
    selected = next(record for record in records if str(record.id) == selected_id)
    if not (selected.token or "").strip():
        st.warning("This legacy document does not have a verification token. Use Generate Missing Verification Token above.")
        return
    public_url = verification_url(selected.token)
    st.markdown("**Copy Verification URL**")
    st.code(public_url, language=None)
    st.link_button("Open Verification Page", public_url)
    st.download_button(
        "Regenerate QR Image",
        data=verification_qr_png(public_url),
        file_name=f"verify-{selected.document_type.value.lower()}-{selected.id}.png",
        mime="image/png",
    )


def main() -> None:
    public_url_warnings = settings.public_base_url_warnings()
    init_database()
    with SessionLocal() as bootstrap_session:
        if bootstrap_admin(bootstrap_session):
            bootstrap_session.commit()

    verify_token = route_token("verify")
    if verify_token:
        render_verification(verify_token)
        return
    payment_token = route_token("payment") or route_token("payment_token")
    if payment_token:
        render_author_payment(payment_token)
        return

    if "user_id" not in st.session_state:
        render_login()
        return
    try:
        authenticated_at = datetime.fromisoformat(st.session_state.auth_started_at)
        if datetime.now(timezone.utc) - authenticated_at > timedelta(hours=settings.session_hours):
            st.session_state.clear()
            st.warning("Your session expired. Please sign in again.")
            render_login()
            return
    except (AttributeError, KeyError, TypeError, ValueError):
        st.session_state.clear()
        render_login()
        return

    with SessionLocal() as session:
        try:
            user = session.get(User, uuid.UUID(st.session_state.user_id))
        except (ValueError, TypeError):
            user = None
        if not user or not user.active:
            st.session_state.pop("user_id", None)
            st.rerun()
        journals = journals_for_user(session, user)
        if not journals:
            st.error("Your account is not assigned to any active journal.")
            if st.button("Sign out"):
                st.session_state.clear()
                st.rerun()
            return

        st.sidebar.markdown('<div class="jas-eyebrow">Journal Administration</div>', unsafe_allow_html=True)
        st.sidebar.write(f"**{user.display_name}**")
        st.sidebar.caption(user.role.value.replace("_", " ").title())
        journal_id = st.sidebar.selectbox("Active journal", [str(x.id) for x in journals], format_func=lambda value: next(f"{x.abbreviation} · {x.name}" for x in journals if str(x.id) == value))
        journal = next(x for x in journals if str(x.id) == journal_id)

        search = st.sidebar.text_input("Global search", placeholder="ID, title, author, DOI…")
        if search.strip():
            st.title("Search results")
            results = global_submission_search(session, [x.id for x in journals], search)
            st.dataframe(pd.DataFrame([{"Journal": x.journal.abbreviation, "OJS ID": x.ojs_submission_id or "Manual", "Title": x.manuscript_title, "Author": x.corresponding_author, "Editorial": x.editorial_status.value, "Publication": x.publication_status.value} for x in results]), use_container_width=True, hide_index=True)
        else:
            if user.role == Role.FINANCE:
                pages = [
                    "Dashboard",
                    "DOCUMENTS · Invoices",
                    "DOCUMENTS · Receipts",
                    "FINANCE · Payment Verification",
                    "FINANCE · Financial Report",
                    "REPORTS · Export Data",
                ]
            else:
                pages = [
                    "Dashboard",
                    "SUBMISSIONS · All Submissions",
                    "SUBMISSIONS · Add Submission",
                    "SUBMISSIONS · Import CSV/Excel",
                    "SUBMISSIONS · Issue Monitor",
                    "REVISION · Quick Template Revision",
                    "REVISION · Reviewer Revision",
                    "REVISION · Revision Jobs",
                    "REVISION · Revision History",
                    "DOCUMENTS · Letter of Acceptance",
                    "DOCUMENTS · Invoices",
                    "DOCUMENTS · Receipts",
                    "DOCUMENTS · Verification",
                    "FINANCE · Payment Verification",
                    "FINANCE · Financial Report",
                    "PUBLICATION · Publication Tracking",
                    "REPORTS · Export Data",
                ]
            if user.role == Role.SUPER_ADMIN:
                pages += ["ADMINISTRATION · Journals", "ADMINISTRATION · Users", "ADMINISTRATION · Templates", "ADMINISTRATION · Audit Log"]
            elif user.role == Role.JOURNAL_ADMIN:
                pages += ["ADMINISTRATION · Templates", "ADMINISTRATION · Audit Log"]
            if st.session_state.pop("revision_go_to_quick", False):
                st.session_state.jas_navigation = "REVISION · Reviewer Revision"
            page = st.sidebar.radio("Navigation", pages, label_visibility="collapsed", key="jas_navigation")
            renderers = {
                "Dashboard": lambda: dashboard(session, journal),
                "SUBMISSIONS · All Submissions": lambda: all_submissions(session, journal, user),
                "SUBMISSIONS · Add Submission": lambda: add_submission(session, journal, user),
                "SUBMISSIONS · Import CSV/Excel": lambda: import_submissions(session, journal, user),
                "SUBMISSIONS · Issue Monitor": lambda: issue_monitor_page(session, journal),
                "REVISION · Quick Template Revision": lambda: quick_template_revision_page(session, journal, user),
                "REVISION · Reviewer Revision": lambda: quick_revision_page(session, journal, user),
                "REVISION · Revision Jobs": lambda: revision_jobs_hub(session, journal, user),
                "REVISION · Revision History": lambda: revision_history_hub(session, journal, user),
                "DOCUMENTS · Letter of Acceptance": lambda: loa_page(session, journal, user),
                "DOCUMENTS · Invoices": lambda: invoices_page(session, journal, user),
                "DOCUMENTS · Receipts": lambda: receipts_page(session, journal, user),
                "DOCUMENTS · Verification": lambda: document_verification_page(session, journal, user),
                "FINANCE · Payment Verification": lambda: payment_verification_page(session, journal, user),
                "FINANCE · Financial Report": lambda: reports_page(session, journal),
                "PUBLICATION · Publication Tracking": lambda: publication_page(session, journal, user),
                "REPORTS · Export Data": lambda: reports_page(session, journal),
                "ADMINISTRATION · Journals": lambda: journals_page(session, journal, user),
                "ADMINISTRATION · Users": lambda: users_page(session, journal, user),
                "ADMINISTRATION · Templates": lambda: templates_page(session, journal, user),
                "ADMINISTRATION · Audit Log": lambda: audit_page(session, journal),
            }
            renderers[page]()
        st.sidebar.divider()
        if user.role in {Role.SUPER_ADMIN, Role.JOURNAL_ADMIN}:
            for warning in public_url_warnings:
                st.sidebar.warning(warning)
        if settings.is_development_database:
            st.sidebar.warning("Development database: SQLite. Set DATABASE_URL to PostgreSQL for production.")
        if st.sidebar.button("Sign out", use_container_width=True):
            st.session_state.clear()
            st.rerun()


if __name__ == "__main__":
    main()
