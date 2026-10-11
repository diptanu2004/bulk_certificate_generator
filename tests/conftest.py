"""Tests run against SQLite so `pytest` needs no infrastructure.

Environment is set before the app package is imported, because `app.config`
reads it once at import time.
"""

import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="certgen-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(TMP / 'test.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(TMP / "storage")
os.environ["MAX_ATTEMPTS"] = "1"  # a failure is terminal unless a test says otherwise

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Certificate, Job  # noqa: E402
from app.storage import get_storage  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    init_db()
    with SessionLocal() as session:
        session.execute(delete(Certificate))
        session.execute(delete(Job))
        session.commit()
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def storage():
    return get_storage()


@pytest.fixture
def recipients():
    return [
        {"name": "Ada Lovelace", "email": "ada@example.com"},
        {"name": "Alan Turing", "email": "alan@example.com"},
        {"name": "Grace Hopper", "email": "grace@example.com"},
    ]


def create_job(client, recipients, course_name="Advanced Aerodynamics", **kwargs):
    """Submit a job and return the parsed response body."""
    response = client.post(
        "/v1/jobs",
        json={"course_name": course_name, "recipients": recipients},
        **kwargs,
    )
    assert response.status_code == 202, response.text
    return response.json()


def drain(render=None):
    """Run the worker until the queue is empty. Returns rows processed."""
    from app.renderer import render_certificate
    from app.worker import run_once

    processed = 0
    while True:
        handled = run_once(render=render or render_certificate)
        if handled == 0:
            return processed
        processed += handled
