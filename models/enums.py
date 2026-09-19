from enum import Enum


class Role(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    JOURNAL_ADMIN = "JOURNAL_ADMIN"
    FINANCE = "FINANCE"


class EditorialStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class PublicationStatus(str, Enum):
    NOT_READY = "NOT_READY"
    READY_FOR_PUBLICATION = "READY_FOR_PUBLICATION"
    PUBLISHED = "PUBLISHED"


class LoAStatus(str, Enum):
    VALID = "VALID"
    REVOKED = "REVOKED"
    SUPERSEDED = "SUPERSEDED"


class InvoiceStatus(str, Enum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    PAYMENT_SUBMITTED = "PAYMENT_SUBMITTED"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


class PaymentStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class DocumentStatus(str, Enum):
    VALID = "VALID"
    REVOKED = "REVOKED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class DocumentType(str, Enum):
    LOA = "LOA"
    INVOICE = "INVOICE"
    RECEIPT = "RECEIPT"


class TemplateStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
