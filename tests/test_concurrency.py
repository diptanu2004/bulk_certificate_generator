"""Proves the queue's core claim: two workers never render the same certificate.

SQLite has no SKIP LOCKED (SQLAlchemy drops the clause), so this is the one
test that needs a real PostgreSQL. It is skipped unless TEST_DATABASE_URL is
set, which keeps the default `pytest` run infrastructure-free.

    TEST_DATABASE_URL=postgresql+psycopg://certgen:certgen@localhost:5432/certgen \
        pytest -m postgres
"""

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, delete, insert, select, text
from sqlalchemy.orm import sessionmaker

from app import service
from app.db import Base
from app.models import Certificate, CertificateStatus, Job

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]

BATCH = 10


@pytest.fixture
def pg_sessions():
    """Two sessions on two connections -- i.e. two worker processes."""
    engine = create_engine(TEST_DATABASE_URL, future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False, future=True)

    with Session() as setup:
        setup.execute(delete(Certificate))
        setup.execute(delete(Job))

        job = Job(id=uuid.uuid4(), course_name="Concurrency", issued_on=date.today(), total=2 * BATCH)
        setup.add(job)
        setup.flush()
        setup.execute(
            insert(Certificate),
            [
                {
                    "id": uuid.uuid4(),
                    "job_id": job.id,
                    "recipient_name": f"Person {i}",
                    "recipient_email": f"p{i}@example.com",
                }
                for i in range(2 * BATCH)
            ],
        )
        setup.commit()

    with Session() as worker_a, Session() as worker_b:
        yield worker_a, worker_b

    engine.dispose()


def test_a_second_worker_skips_locked_rows_instead_of_blocking(pg_sessions):
    worker_a, worker_b = pg_sessions

    # Worker A locks a batch and holds the transaction open, as it would while
    # rendering. Nothing is committed yet, so the rows are still `pending`.
    locked = worker_a.scalars(
        select(Certificate)
        .where(Certificate.status == CertificateStatus.pending)
        .order_by(Certificate.created_at)
        .limit(BATCH)
        .with_for_update(skip_locked=True)
    ).all()
    assert len(locked) == BATCH

    # Without SKIP LOCKED worker B would wait on A's locks; the timeout turns
    # that regression into a fast failure rather than a hung test.
    worker_b.execute(text("SET LOCAL statement_timeout = '5s'"))
    claimed_b = service.claim_batch(worker_b, BATCH)

    assert len(claimed_b) == BATCH
    assert {c.id for c in locked}.isdisjoint({c.id for c in claimed_b})

    # A crashes instead of committing: its rows are untouched and claimable.
    worker_a.rollback()
    reclaimed = service.claim_batch(worker_a, 2 * BATCH)
    assert {c.id for c in reclaimed} == {c.id for c in locked}
