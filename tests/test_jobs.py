"""Job creation, input validation and status reporting."""

import uuid

from conftest import create_job, drain

from app.models import JobStatus


def test_create_job_queues_one_certificate_per_recipient(client, recipients):
    body = create_job(client, recipients)

    assert body["total"] == 3
    assert body["accepted"] == 3
    assert body["rejected"] == 0
    assert body["status"] == JobStatus.pending

    listing = client.get(f"/v1/jobs/{body['id']}/certificates").json()
    assert [item["status"] for item in listing["items"]] == ["pending"] * 3
    assert {item["recipient_email"] for item in listing["items"]} == {
        r["email"] for r in recipients
    }


def test_envelope_problems_reject_the_whole_request(client, recipients):
    # Missing course name.
    assert client.post("/v1/jobs", json={"recipients": recipients}).status_code == 422
    # No recipients at all.
    assert (
        client.post("/v1/jobs", json={"course_name": "X", "recipients": []}).status_code == 422
    )
    # Blank course name.
    assert (
        client.post(
            "/v1/jobs", json={"course_name": "   ", "recipients": recipients}
        ).status_code
        == 422
    )


def test_too_many_recipients_is_rejected(client):
    from app.config import settings

    oversized = [
        {"name": f"Person {i}", "email": f"p{i}@example.com"}
        for i in range(settings.max_recipients + 1)
    ]
    response = client.post("/v1/jobs", json={"course_name": "X", "recipients": oversized})
    assert response.status_code == 422


def test_invalid_recipients_are_recorded_without_blocking_valid_ones(client):
    body = create_job(
        client,
        [
            {"name": "Ada Lovelace", "email": "ada@example.com"},
            {"name": "", "email": "blank@example.com"},
            {"name": "Bad Email", "email": "not-an-email"},
            {"name": "Grace Hopper", "email": "grace@example.com"},
            {"email": "missing-name@example.com"},
        ],
    )

    assert body["total"] == 5
    assert body["accepted"] == 2
    assert body["rejected"] == 3
    assert body["status"] == JobStatus.pending  # the two valid rows still run

    failed = client.get(f"/v1/jobs/{body['id']}/certificates?status=failed").json()["items"]
    assert len(failed) == 3
    assert all(item["error"] for item in failed)
    assert any("email" in item["error"] for item in failed)

    # The valid recipients generate normally.
    drain()
    job = client.get(f"/v1/jobs/{body['id']}").json()
    assert (job["succeeded"], job["failed"]) == (2, 3)
    assert job["status"] == JobStatus.completed_with_errors


def test_job_with_no_valid_recipients_is_closed_immediately(client):
    body = create_job(client, [{"name": "", "email": "nope"}])

    assert body["accepted"] == 0
    assert body["status"] == JobStatus.completed_with_errors
    assert drain() == 0  # nothing was ever queued


def test_status_reports_progress(client, recipients, db):
    from app import service

    body = create_job(client, recipients)

    before = client.get(f"/v1/jobs/{body['id']}").json()
    assert (before["succeeded"], before["failed"], before["pending"]) == (0, 0, 3)

    # Process a single certificate to observe a partially complete job.
    claimed = service.claim_batch(db, 1)
    service.mark_succeeded(db, claimed[0], "ignored/key.pdf")

    midway = client.get(f"/v1/jobs/{body['id']}").json()
    assert (midway["succeeded"], midway["pending"]) == (1, 2)
    assert midway["status"] == JobStatus.processing

    drain()
    after = client.get(f"/v1/jobs/{body['id']}").json()
    assert (after["succeeded"], after["failed"], after["pending"]) == (3, 0, 0)
    assert after["status"] == JobStatus.completed


def test_idempotency_key_returns_the_original_job(client, recipients, db):
    from sqlalchemy import func, select

    from app.models import Job

    headers = {"Idempotency-Key": "submit-once-abc"}
    first = create_job(client, recipients, headers=headers)
    second = create_job(client, recipients, headers=headers)

    assert first["id"] == second["id"]
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_unknown_job_is_404(client):
    missing = uuid.uuid4()
    assert client.get(f"/v1/jobs/{missing}").status_code == 404
    assert client.get(f"/v1/jobs/{missing}/certificates").status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
