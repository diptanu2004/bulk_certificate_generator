"""All database logic and invariants.

Both processes import this module: the API calls the write/read helpers, the
worker calls the queue helpers. Nothing here knows about HTTP, PDFs or files.
"""

import base64
import uuid
from datetime import UTC, date, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import case, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.schemas import JobCreate, RecipientIn


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"]) or "body"
        parts.append(field + ": " + err["msg"])
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# Writes (API)
# --------------------------------------------------------------------------- #


def create_job(
    session: Session, payload: JobCreate, idempotency_key: str | None = None
) -> tuple[Job, int, int]:
    """Persist a job plus one certificate row per recipient.

    Returns (job, accepted, rejected). Recipients that fail validation are
    stored as `failed` certificates carrying the reason, so the client tracks
    one job and sees per-recipient outcomes in a single uniform shape.
    """
    if idempotency_key:
        replay = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if replay is not None:
            return replay, replay.total - replay.failed, replay.failed

    job = Job(
        id=uuid.uuid4(),
        course_name=payload.course_name,
        issued_on=payload.issued_on or date.today(),
        idempotency_key=idempotency_key,
    )

    rows: list[dict] = []
    accepted = rejected = 0
    for raw in payload.recipients:
        try:
            recipient = RecipientIn.model_validate(raw)
        except ValidationError as exc:
            rejected += 1
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "job_id": job.id,
                    "recipient_name": str(raw.get("name") or "")[:200],
                    "recipient_email": str(raw.get("email") or "")[:320],
                    "status": CertificateStatus.failed,
                    "error": _format_errors(exc),
                }
            )
        else:
            accepted += 1
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "job_id": job.id,
                    "recipient_name": recipient.name,
                    "recipient_email": recipient.email,
                    "status": CertificateStatus.pending,
                }
            )

    job.total = len(rows)
    job.failed = rejected
    job.status = JobStatus.pending if accepted else JobStatus.completed_with_errors

    try:
        session.add(job)
        session.flush()
        # One bulk INSERT rather than 10k ORM objects: no identity-map or
        # unit-of-work overhead on the hot submission path.
        session.execute(insert(Certificate), rows)
        session.commit()
    except IntegrityError:
        # Two concurrent submissions raced on the same Idempotency-Key.
        session.rollback()
        if idempotency_key:
            winner = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
            if winner is not None:
                return winner, winner.total - winner.failed, winner.failed
        raise

    return job, accepted, rejected


# --------------------------------------------------------------------------- #
# Reads (API)
# --------------------------------------------------------------------------- #


def get_job(session: Session, job_id: uuid.UUID) -> Job | None:
    return session.get(Job, job_id)


def get_certificate(session: Session, certificate_id: uuid.UUID) -> Certificate | None:
    return session.get(Certificate, certificate_id)


def _encode_cursor(cert: Certificate) -> str:
    raw = cert.created_at.isoformat() + "|" + str(cert.id)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    created_at, _, cert_id = raw.partition("|")
    return datetime.fromisoformat(created_at), uuid.UUID(cert_id)


def list_certificates(
    session: Session,
    job_id: uuid.UUID,
    status: CertificateStatus | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Certificate], str | None]:
    """Keyset pagination on (created_at, id) -- constant cost at any depth."""
    stmt = select(Certificate).where(Certificate.job_id == job_id)
    if status is not None:
        stmt = stmt.where(Certificate.status == status)
    if cursor:
        created_at, cert_id = _decode_cursor(cursor)
        stmt = stmt.where(
            (Certificate.created_at > created_at)
            | ((Certificate.created_at == created_at) & (Certificate.id > cert_id))
        )

    stmt = stmt.order_by(Certificate.created_at, Certificate.id).limit(limit + 1)
    items = list(session.scalars(stmt))
    if len(items) > limit:
        return items[:limit], _encode_cursor(items[limit - 1])
    return items, None


def succeeded_certificates(session: Session, job_id: uuid.UUID) -> list[Certificate]:
    return list(
        session.scalars(
            select(Certificate)
            .where(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.succeeded,
            )
            .order_by(Certificate.created_at)
        )
    )


# --------------------------------------------------------------------------- #
# Queue (worker)
# --------------------------------------------------------------------------- #


def claim_batch(session: Session, limit: int) -> list[Certificate]:
    """Atomically take up to `limit` pending rows for this worker.

    SELECT ... FOR UPDATE SKIP LOCKED gives competing-consumer semantics with
    no broker: concurrent workers lock disjoint rows and never render the same
    certificate twice. The locks are held until the commit below, so the
    read-then-mark sequence is safe.
    """
    certs = list(
        session.scalars(
            select(Certificate)
            .where(Certificate.status == CertificateStatus.pending)
            .order_by(Certificate.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if not certs:
        return []

    now = datetime.now(UTC)
    for cert in certs:
        cert.status = CertificateStatus.processing
        cert.claimed_at = now
        cert.attempts += 1

    session.execute(
        update(Job)
        .where(Job.id.in_({c.job_id for c in certs}), Job.status == JobStatus.pending)
        .values(status=JobStatus.processing)
    )
    session.commit()
    return certs


def requeue_stale(session: Session) -> int:
    """Return rows abandoned by a crashed worker to the queue."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.visibility_timeout_seconds)
    result = session.execute(
        update(Certificate)
        .where(
            Certificate.status == CertificateStatus.processing,
            Certificate.claimed_at < cutoff,
        )
        .values(status=CertificateStatus.pending, claimed_at=None)
    )
    session.commit()
    return result.rowcount


def _finalise_job(session: Session, job_id: uuid.UUID) -> None:
    """Close the job once every certificate has reached a terminal state.

    Expressed as one conditional UPDATE so concurrent workers cannot disagree
    about the final status.
    """
    session.execute(
        update(Job)
        .where(Job.id == job_id, Job.succeeded + Job.failed >= Job.total)
        .values(
            status=case(
                (Job.failed > 0, JobStatus.completed_with_errors),
                else_=JobStatus.completed,
            )
        )
    )


def _settle(session: Session, cert: Certificate, values: dict, counter) -> bool:
    """Move a claimed row to a terminal state and bump the matching job counter.

    The `status == processing` guard makes this idempotent: a redelivered row
    that is already settled updates nothing, so counters never double-count.
    """
    result = session.execute(
        update(Certificate)
        .where(Certificate.id == cert.id, Certificate.status == CertificateStatus.processing)
        .values(**values)
    )
    if result.rowcount == 0:
        session.commit()
        return False

    session.execute(update(Job).where(Job.id == cert.job_id).values({counter: counter + 1}))
    _finalise_job(session, cert.job_id)
    session.commit()
    session.refresh(cert)
    return True


def mark_succeeded(session: Session, cert: Certificate, storage_key: str) -> bool:
    return _settle(
        session,
        cert,
        {"status": CertificateStatus.succeeded, "storage_key": storage_key, "error": None},
        Job.succeeded,
    )


def mark_failed(session: Session, cert: Certificate, error: str) -> bool:
    """Fail the row, or return it to the queue if retries remain.

    A render failure is isolated to its own row: siblings in the same job are
    untouched and keep processing. Returns True when the failure is terminal.
    """
    if cert.attempts < settings.max_attempts:
        session.execute(
            update(Certificate)
            .where(Certificate.id == cert.id, Certificate.status == CertificateStatus.processing)
            .values(status=CertificateStatus.pending, claimed_at=None, error=error)
        )
        session.commit()
        session.refresh(cert)
        return False

    return _settle(session, cert, {"status": CertificateStatus.failed, "error": error}, Job.failed)
