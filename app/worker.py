"""Background generation worker -- run with `python -m app.worker`.

A thin driver: claim rows, render, store, settle. All invariants live in
`service`, so running one worker or twenty changes nothing but throughput.
"""

import logging
import signal
import time
from collections.abc import Callable
from types import FrameType

from app import service
from app.config import settings
from app.db import SessionLocal, init_db
from app.renderer import render_certificate
from app.storage import Storage, get_storage, storage_key

log = logging.getLogger("certgen.worker")

Renderer = Callable[..., bytes]


def run_once(storage: Storage | None = None, render: Renderer = render_certificate) -> int:
    """Claim and process one batch. Returns the number of rows handled."""
    storage = storage or get_storage()

    with SessionLocal() as session:
        certs = service.claim_batch(session, settings.worker_batch_size)

        for cert in certs:
            job = cert.job
            try:
                data = render(
                    recipient_name=cert.recipient_name,
                    course_name=job.course_name,
                    issued_on=job.issued_on,
                )
                key = storage_key(job.id, cert.id)
                storage.write(key, data)
                service.mark_succeeded(session, cert, key)
            except Exception as exc:
                # Contained per row: the loop continues with the next recipient.
                reason = f"{type(exc).__name__}: {exc}"
                log.warning("certificate %s failed (attempt %s): %s", cert.id, cert.attempts, reason)
                service.mark_failed(session, cert, reason)

        return len(certs)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_db()

    running = True

    def stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal running
        log.info("signal %s received, finishing current batch", signum)
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    reap_every = max(settings.visibility_timeout_seconds / 2, 1)
    last_reap = 0.0
    log.info("worker started (batch=%s)", settings.worker_batch_size)

    while running:
        now = time.monotonic()
        if now - last_reap > reap_every:
            with SessionLocal() as session:
                recovered = service.requeue_stale(session)
            if recovered:
                log.info("requeued %s stale certificate(s)", recovered)
            last_reap = now

        # Only sleep when the queue was empty, so a backlog drains at full speed.
        if run_once() == 0:
            time.sleep(settings.worker_poll_seconds)

    log.info("worker stopped")


if __name__ == "__main__":
    main()
