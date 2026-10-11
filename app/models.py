import uuid
from datetime import UTC, date, datetime
from enum import Enum

from sqlalchemy import (
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class JobStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    completed_with_errors = "completed_with_errors"


class CertificateStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"


def _utcnow() -> datetime:
    """Timestamps are generated in Python, not by the database.

    SQLite's CURRENT_TIMESTAMP has second resolution, which would both collapse
    the queue's FIFO ordering and break keyset pagination. Generating them here
    gives identical microsecond-precision behaviour on SQLite and PostgreSQL.
    """
    return datetime.now(UTC)


# native_enum=False stores the value as VARCHAR, so adding a status later is a
# code change rather than a database type migration.
def _enum(py_enum: type[Enum]) -> SAEnum:
    return SAEnum(py_enum, native_enum=False, length=32, validate_strings=True)


class Job(Base):
    """One bulk generation request."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    course_name: Mapped[str] = mapped_column(String(200))
    issued_on: Mapped[date] = mapped_column(Date)
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus), default=JobStatus.pending)

    # Denormalised counters. Status polling is the hottest read path, so it must
    # not require aggregating every certificate row in the job.
    total: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    @property
    def pending(self) -> int:
        return self.total - self.succeeded - self.failed


class Certificate(Base):
    """One recipient's certificate. Doubles as the work-queue row."""

    __tablename__ = "certificates"
    __table_args__ = (
        # Supports the claim query: pending rows in arrival order.
        Index("ix_certificates_claim", "status", "created_at"),
        Index("ix_certificates_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))

    recipient_name: Mapped[str] = mapped_column(String(200))
    recipient_email: Mapped[str] = mapped_column(String(320))

    status: Mapped[CertificateStatus] = mapped_column(
        _enum(CertificateStatus), default=CertificateStatus.pending
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    storage_key: Mapped[str | None] = mapped_column(String(512), default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    job: Mapped[Job] = relationship(back_populates="certificates")
