"""HTTP layer. Parses requests, calls `service`, maps results to responses.

No business rules and no SQL live here.
"""

import uuid
import zipfile
from tempfile import SpooledTemporaryFile

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import service
from app.db import get_db
from app.models import CertificateStatus
from app.schemas import (
    CertificateOut,
    CertificatePage,
    JobCreate,
    JobCreated,
    JobOut,
)
from app.storage import Storage, get_storage

router = APIRouter(prefix="/v1")


@router.post("/jobs", response_model=JobCreated, status_code=status.HTTP_202_ACCEPTED)
def create_job(
    payload: JobCreate,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JobCreated:
    """Accept a bulk request and return immediately; generation runs in the worker.

    Replaying the same Idempotency-Key returns the original job instead of
    duplicating the whole batch.
    """
    job, accepted, rejected = service.create_job(db, payload, idempotency_key)
    return JobCreated(
        id=job.id,
        status=job.status,
        total=job.total,
        accepted=accepted,
        rejected=rejected,
    )


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobOut:
    """Progress and counters. A single-row read, whatever the job size."""
    job = service.get_job(db, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return JobOut.model_validate(job)


@router.get("/jobs/{job_id}/certificates", response_model=CertificatePage)
def list_certificates(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    cert_status: CertificateStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = None,
) -> CertificatePage:
    """Per-recipient outcomes, filterable by status. Filter `status=failed` to
    see exactly which recipients failed and why."""
    if service.get_job(db, job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")

    try:
        items, next_cursor = service.list_certificates(db, job_id, cert_status, limit, cursor)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from exc

    return CertificatePage(
        items=[CertificateOut.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


@router.get("/certificates/{certificate_id}/file")
def download_certificate(
    certificate_id: uuid.UUID,
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> Response:
    cert = service.get_certificate(db, certificate_id)
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "certificate not found")
    if cert.status != CertificateStatus.succeeded or cert.storage_key is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"certificate is not available (status: {cert.status.value})",
        )

    filename = f"{cert.recipient_name.replace(' ', '_')}.pdf"
    return Response(
        content=storage.read(cert.storage_key),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/jobs/{job_id}/archive")
def download_archive(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> StreamingResponse:
    """Every generated certificate in the job as one zip.

    Built into a SpooledTemporaryFile so memory stays bounded for large jobs.
    """
    job = service.get_job(db, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")

    certs = service.succeeded_certificates(db, job_id)
    if not certs:
        raise HTTPException(status.HTTP_409_CONFLICT, "no generated certificates yet")

    spool: SpooledTemporaryFile = SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, cert in enumerate(certs, start=1):
            name = cert.recipient_name.replace(" ", "_") or "certificate"
            archive.writestr(f"{index:04d}_{name}.pdf", storage.read(cert.storage_key))
    spool.seek(0)

    return StreamingResponse(
        spool,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="certificates_{job_id}.zip"'},
    )
