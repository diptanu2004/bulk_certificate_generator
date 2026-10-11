"""Certificate rendering, the work queue, and per-certificate failure isolation."""

import io
from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import create_job, drain
from pypdf import PdfReader
from sqlalchemy import select

from app import service
from app.models import Certificate, CertificateStatus, JobStatus
from app.renderer import render_certificate
from app.storage import storage_key
from app.worker import run_once


def test_renderer_embeds_the_recipient_details():
    pdf = render_certificate(
        recipient_name="Ada Lovelace",
        course_name="Advanced Aerodynamics",
        issued_on=date(2026, 10, 10),
    )

    assert pdf.startswith(b"%PDF")
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "Ada Lovelace" in text
    assert "Advanced Aerodynamics" in text
    assert "2026-10-10" in text


def test_renderer_rejects_a_blank_name():
    with pytest.raises(ValueError):
        render_certificate(recipient_name="   ", course_name="X", issued_on=date.today())


def test_worker_generates_a_pdf_for_every_recipient(client, recipients, db, storage):
    body = create_job(client, recipients)

    assert drain() == 3

    job = client.get(f"/v1/jobs/{body['id']}").json()
    assert (job["succeeded"], job["failed"]) == (3, 0)
    assert job["status"] == JobStatus.completed

    certs = db.scalars(select(Certificate)).all()
    for cert in certs:
        assert cert.status == CertificateStatus.succeeded
        assert cert.storage_key == storage_key(cert.job_id, cert.id)
        assert storage.read(cert.storage_key).startswith(b"%PDF")


def test_one_failing_certificate_does_not_block_the_others(client, recipients, db):
    body = create_job(client, recipients)

    def flaky_render(*, recipient_name, **kwargs):
        if recipient_name == "Alan Turing":
            raise RuntimeError("font subsystem unavailable")
        return render_certificate(recipient_name=recipient_name, **kwargs)

    drain(render=flaky_render)

    job = client.get(f"/v1/jobs/{body['id']}").json()
    assert (job["succeeded"], job["failed"], job["pending"]) == (2, 1, 0)
    assert job["status"] == JobStatus.completed_with_errors

    failed = client.get(f"/v1/jobs/{body['id']}/certificates?status=failed").json()["items"]
    assert len(failed) == 1
    assert failed[0]["recipient_name"] == "Alan Turing"
    assert "font subsystem unavailable" in failed[0]["error"]

    succeeded = client.get(f"/v1/jobs/{body['id']}/certificates?status=succeeded").json()["items"]
    assert {item["recipient_name"] for item in succeeded} == {"Ada Lovelace", "Grace Hopper"}


def test_a_transient_failure_is_retried(client, db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_attempts", 2)
    body = create_job(client, [{"name": "Ada Lovelace", "email": "ada@example.com"}])

    attempts = {"n": 0}

    def fails_once(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("disk hiccup")
        return render_certificate(**kwargs)

    # First pass fails and returns the row to the queue, second pass succeeds.
    assert run_once(render=fails_once) == 1
    cert = db.scalars(select(Certificate)).one()
    db.refresh(cert)
    assert cert.status == CertificateStatus.pending
    assert cert.attempts == 1

    assert run_once(render=fails_once) == 1
    job = client.get(f"/v1/jobs/{body['id']}").json()
    assert (job["succeeded"], job["failed"]) == (1, 0)
    assert job["status"] == JobStatus.completed


def test_claimed_rows_are_not_handed_out_twice(client, recipients, db):
    create_job(client, recipients)

    first = service.claim_batch(db, 2)
    assert len(first) == 2

    with_second_worker = service.claim_batch(db, 10)
    assert len(with_second_worker) == 1  # only the unclaimed row remains
    assert {c.id for c in first}.isdisjoint({c.id for c in with_second_worker})


def test_stale_claims_are_requeued_after_the_visibility_timeout(client, recipients, db):
    from app.config import settings

    create_job(client, recipients)
    claimed = service.claim_batch(db, 3)

    # Simulate a worker that died mid-render.
    stale = datetime.now(UTC) - timedelta(seconds=settings.visibility_timeout_seconds + 60)
    for cert in claimed:
        cert.claimed_at = stale
    db.commit()

    assert service.requeue_stale(db) == 3
    assert drain() == 3


def test_settling_the_same_certificate_twice_does_not_double_count(client, db):
    body = create_job(client, [{"name": "Ada Lovelace", "email": "ada@example.com"}])
    cert = service.claim_batch(db, 1)[0]

    assert service.mark_succeeded(db, cert, "a/b.pdf") is True
    assert service.mark_succeeded(db, cert, "a/b.pdf") is False  # already terminal

    job = client.get(f"/v1/jobs/{body['id']}").json()
    assert job["succeeded"] == 1
