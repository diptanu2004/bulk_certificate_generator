import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.config import settings
from app.models import CertificateStatus, JobStatus


class RecipientIn(BaseModel):
    """Per-recipient rules. Applied row by row so one bad row cannot reject the batch."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    email: EmailStr


class JobCreate(BaseModel):
    """The request envelope. Only envelope problems reject the whole request.

    `recipients` stays untyped here on purpose: each element is validated
    separately in the service layer so invalid rows can be *recorded* as failed
    instead of aborting the submission.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    course_name: str = Field(min_length=1, max_length=200)
    issued_on: date | None = None
    recipients: list[dict[str, Any]] = Field(min_length=1, max_length=settings.max_recipients)


class JobCreated(BaseModel):
    id: uuid.UUID
    status: JobStatus
    total: int
    accepted: int
    rejected: int


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    course_name: str
    issued_on: date
    status: JobStatus
    total: int
    succeeded: int
    failed: int
    pending: int
    created_at: datetime
    updated_at: datetime


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recipient_name: str
    recipient_email: str
    status: CertificateStatus
    attempts: int
    error: str | None


class CertificatePage(BaseModel):
    items: list[CertificateOut]
    next_cursor: str | None
