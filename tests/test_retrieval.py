"""Retrieving generated certificates: listing, single download, bulk archive."""

import io
import uuid
import zipfile

from conftest import create_job, drain

from app.renderer import render_certificate


def test_download_a_generated_certificate(client, recipients):
    body = create_job(client, recipients)
    drain()

    items = client.get(f"/v1/jobs/{body['id']}/certificates").json()["items"]
    target = next(item for item in items if item["recipient_name"] == "Ada Lovelace")

    response = client.get(f"/v1/certificates/{target['id']}/file")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "Ada_Lovelace.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


def test_downloading_before_generation_is_a_conflict(client, recipients):
    body = create_job(client, recipients)
    pending = client.get(f"/v1/jobs/{body['id']}/certificates").json()["items"][0]

    response = client.get(f"/v1/certificates/{pending['id']}/file")
    assert response.status_code == 409
    assert "pending" in response.json()["detail"]


def test_downloading_an_unknown_certificate_is_404(client):
    assert client.get(f"/v1/certificates/{uuid.uuid4()}/file").status_code == 404


def test_listing_is_paginated(client):
    body = create_job(
        client,
        [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(5)],
    )

    first = client.get(f"/v1/jobs/{body['id']}/certificates?limit=2").json()
    assert len(first["items"]) == 2
    assert first["next_cursor"]

    seen = {item["id"] for item in first["items"]}
    cursor = first["next_cursor"]
    while cursor:
        page = client.get(
            f"/v1/jobs/{body['id']}/certificates", params={"limit": 2, "cursor": cursor}
        ).json()
        assert seen.isdisjoint({item["id"] for item in page["items"]})
        seen.update(item["id"] for item in page["items"])
        cursor = page["next_cursor"]

    assert len(seen) == 5


def test_bad_cursor_is_rejected(client, recipients):
    body = create_job(client, recipients)
    response = client.get(f"/v1/jobs/{body['id']}/certificates?cursor=not-a-cursor")
    assert response.status_code == 400


def test_archive_contains_every_generated_certificate(client, recipients):
    body = create_job(client, recipients)
    drain()

    response = client.get(f"/v1/jobs/{body['id']}/archive")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 3
        assert any("Ada_Lovelace" in name for name in names)
        assert archive.read(names[0]).startswith(b"%PDF")


def test_archive_skips_failed_certificates(client, recipients):
    body = create_job(client, recipients)

    def flaky_render(*, recipient_name, **kwargs):
        if recipient_name == "Alan Turing":
            raise RuntimeError("boom")
        return render_certificate(recipient_name=recipient_name, **kwargs)

    drain(render=flaky_render)

    response = client.get(f"/v1/jobs/{body['id']}/archive")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == 2
        assert not any("Alan_Turing" in name for name in archive.namelist())


def test_archive_before_any_generation_is_a_conflict(client, recipients):
    body = create_job(client, recipients)
    assert client.get(f"/v1/jobs/{body['id']}/archive").status_code == 409
