# Bulk Certificate Generator

A backend API that accepts **one request containing many recipients** and generates a PDF
certificate for each of them from a single predefined template. Generation runs in the
background; the client polls the job for progress and downloads the results.

- **Stack:** Python 3.12 · FastAPI · PostgreSQL · SQLAlchemy 2.0 · ReportLab
- **Queue:** PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED` — no broker
- **Shape:** one codebase, two deployables (`api`, `worker`), two tables

---

## Setup

### With Docker (recommended)

```bash
cp .env.example .env          # optional; compose sets what it needs
docker compose up --build
```

This starts PostgreSQL, the API on <http://localhost:8000>, and one worker. Tables are
created on startup. Interactive API docs: <http://localhost:8000/docs>.

Scale generation throughput by adding workers — they coordinate through the database:

```bash
docker compose up --build --scale worker=4
```

### Without Docker

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

export DATABASE_URL=postgresql+psycopg://certgen:certgen@localhost:5432/certgen

uvicorn app.main:app --reload     # terminal 1
python -m app.worker              # terminal 2
```

Configuration is environment-driven; see [`.env.example`](.env.example) for every knob
(`MAX_RECIPIENTS`, `WORKER_BATCH_SIZE`, `MAX_ATTEMPTS`, `VISIBILITY_TIMEOUT_SECONDS`, …).

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

Tests run against SQLite, so **no database or broker is needed**. One test is specific to
PostgreSQL row locking and is skipped by default; to include it:

```bash
docker compose up -d db
TEST_DATABASE_URL=postgresql+psycopg://certgen:certgen@localhost:5432/certgen pytest -m postgres
```

Both paths run in CI on every push — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

---

## Using the API

### Submit a generation request

`POST /v1/jobs` returns **202 Accepted** immediately — it never waits for rendering.

```bash
curl -X POST http://localhost:8000/v1/jobs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: event-2026-cohort-a' \
  -d '{
        "course_name": "Advanced Aerodynamics",
        "issued_on": "2026-10-10",
        "recipients": [
          {"name": "Ada Lovelace", "email": "ada@example.com"},
          {"name": "Alan Turing",  "email": "alan@example.com"},
          {"name": "",             "email": "blank@example.com"}
        ]
      }'
```

```json
{
  "id": "0f2c9a7e-4b1d-4f0a-9c3e-2a1b8d7e6f54",
  "status": "pending",
  "total": 3,
  "accepted": 2,
  "rejected": 1
}
```

`issued_on` is optional (defaults to today). `Idempotency-Key` is optional: replaying the
same key returns the original job instead of generating the batch twice.

### Check progress

`GET /v1/jobs/{job_id}`

```json
{
  "id": "0f2c9a7e-…",
  "course_name": "Advanced Aerodynamics",
  "status": "completed_with_errors",
  "total": 3, "succeeded": 2, "failed": 1, "pending": 0
}
```

Job status is one of `pending`, `processing`, `completed`, `completed_with_errors`.

### Inspect per-recipient results

`GET /v1/jobs/{job_id}/certificates?status=failed&limit=50&cursor=…`

```json
{
  "items": [
    {
      "id": "7a3de57f-…",
      "recipient_name": "",
      "recipient_email": "blank@example.com",
      "status": "failed",
      "attempts": 0,
      "error": "name: String should have at least 1 character"
    }
  ],
  "next_cursor": null
}
```

Filter by `status` (`pending` / `processing` / `succeeded` / `failed`) to get exactly the
successes or exactly the failures with their reasons. Paginate by passing `next_cursor`
back as `cursor`.

### Retrieve generated certificates

```bash
# one certificate
curl -OJ http://localhost:8000/v1/certificates/{certificate_id}/file

# every generated certificate in the job, as a zip
curl -OJ http://localhost:8000/v1/jobs/{job_id}/archive
```

A download returns **409** while the certificate is not yet generated, and the archive
contains only successfully generated certificates.

### Endpoint summary

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/jobs` | Submit a bulk request → `202` |
| `GET` | `/v1/jobs/{id}` | Job status, counters, progress |
| `GET` | `/v1/jobs/{id}/certificates` | Per-recipient results, filterable + paginated |
| `GET` | `/v1/certificates/{id}/file` | Download one PDF |
| `GET` | `/v1/jobs/{id}/archive` | Download all generated PDFs as a zip |
| `GET` | `/health` | Liveness |

---

## Design decisions

### Architecture: two processes, not microservices

```
  POST /v1/jobs                        claim batch (SKIP LOCKED)
  ────────────▶  API (uvicorn)  ──┐          ┌──▶ worker 1 ──┐
  GET  /v1/jobs/{id}              │          │               │
  GET  …/file                     ▼          ├──▶ worker 2 ──┤  render → storage
                         ┌────────────────┐  │               │
                         │   PostgreSQL   │◀─┴──▶ worker N ──┘
                         │ jobs · certs   │     settle row + job counters
                         └────────────────┘
```

A microservice split is justified by independent scaling, failure or ownership boundaries.
Here there is exactly one real boundary: HTTP handling is latency-sensitive and bursty,
while PDF rendering is CPU-bound and throughput-sensitive. That is addressed with **two
deployables built from one codebase**, which buys independent scaling without a network
hop, a second schema, or distributed transactions. Splitting further would put `jobs` and
`certificates` in different services and force me to rebuild consistency that is currently
a single `UPDATE`.

### Bulk processing is asynchronous

A request may carry up to `MAX_RECIPIENTS` (default 10,000) recipients. Rendering them
inline would hold the HTTP connection open for minutes, risk proxy timeouts, and lose all
progress on failure. So `POST /v1/jobs` does only what must be transactional — validate,
persist one row per recipient, return `202` — and the worker does the rendering.

The tradeoff is that the client must poll. `GET /v1/jobs/{id}` is a single-row read, so
polling is cheap; webhooks would be the next step for a production client.

### The queue is the database, not a broker

Workers claim work with:

```sql
SELECT … FROM certificates WHERE status = 'pending'
ORDER BY created_at LIMIT :batch
FOR UPDATE SKIP LOCKED;
```

I chose this over Celery + Redis because:

- The `certificates` rows **are already the source of truth** for per-recipient state — the
  assignment requires reporting it. A broker would be a *second* home for that state, so
  every transition would be a dual write to keep consistent, for no gain.
- `SKIP LOCKED` gives the same competing-consumer semantics: N workers lock disjoint rows
  and never render the same certificate twice. Throughput scales with worker count.
- Crash recovery falls out of the schema rather than needing extra machinery (below).
- One less service to deploy, monitor and explain.

**When this stops being the right choice:** it polls, so it has a latency floor of
`WORKER_POLL_SECONDS` when idle, and the claim query adds load to the primary database. At
a few thousand jobs per second, or with fan-out to multiple consumer types, a real broker
wins. The swap is contained in one function — `service.claim_batch()`.

### Failure handling

Failures are isolated at the row level, in three layers:

1. **Envelope validation** — malformed JSON, missing `course_name`, zero recipients, or
   more than `MAX_RECIPIENTS` returns `422` and persists nothing. The request is
   unusable, so there is nothing to track.
2. **Per-recipient validation** — each recipient is validated *individually*. Invalid ones
   are stored as `failed` certificates carrying the reason; valid ones are queued. A
   request with 998 good and 2 bad rows returns one job with `accepted: 998,
   rejected: 2` rather than rejecting all 1000.
3. **Render failures** — wrapped per certificate inside the worker loop, so an exception
   on one recipient never touches its siblings. The row records the error and the loop
   continues.

Both failure sources land in the same per-recipient shape, so the client has one way to
read outcomes rather than two.

**Retries and crash recovery.** Delivery is at-least-once. A failing row is returned to
the queue until `attempts` reaches `MAX_ATTEMPTS`, then fails terminally with its last
error. If a worker dies mid-render, its rows would sit in `processing` forever, so a reaper
returns rows whose claim is older than `VISIBILITY_TIMEOUT_SECONDS` to `pending`.

Because delivery is at-least-once, settling a row is **idempotent**: the update is guarded
by `WHERE status = 'processing'`, so a redelivered row that is already terminal updates
nothing and the job counters never double-count.

### Why job counters are denormalised

`jobs` carries `total`, `succeeded` and `failed`. Status polling is the hottest read path,
and this makes it a single-row lookup instead of a `COUNT(*) … GROUP BY status` across
every certificate in the job. Workers bump them with `SET succeeded = succeeded + 1`,
which is atomic in SQL — no read-modify-write race. The job's final status is set by one
conditional `UPDATE` (`WHERE succeeded + failed >= total`), so concurrent workers cannot
disagree about who closed the job.

### Other decisions worth naming

- **Keyset pagination**, not `OFFSET`. A job can hold 10,000 rows; `OFFSET 9000` scans
  9,000 rows, while a cursor on `(created_at, id)` costs the same at any depth.
- **Timestamps generated in Python**, not `CURRENT_TIMESTAMP`. SQLite's version has
  second resolution, which collapses the queue's FIFO ordering and breaks keyset
  pagination; generating them in code behaves identically on both databases.
- **Bulk `INSERT`** for certificate rows rather than 10,000 ORM objects, keeping the
  submission path off the unit-of-work hot path.
- **Enums stored as `VARCHAR`** (`native_enum=False`), so adding a status later is a code
  change rather than a database type migration.
- **Storage behind a two-method interface.** `LocalStorage` writes to disk today; the
  interface is the seam for S3 when you run more than one API replica. Files are keyed
  `{job_id}/{certificate_id}.pdf`, which makes rendering idempotent — a retry overwrites
  its own object.
- **The template is drawn programmatically** rather than overlaid on a supplied image, so
  output needs no external font or asset and is reproducible on any machine.

### Out of scope (deliberately)

Authentication, multi-tenancy, a template editor or multiple designs, email delivery, and
webhook callbacks. One omission is worth calling out: **there are no Alembic migrations** —
tables are created at startup, which keeps setup to a single command for a two-table
service. Production would use Alembic, and that is the first thing I would add.

In production I would also serve downloads as presigned S3 URLs rather than streaming
bytes through the API, and emit a webhook on job completion so clients need not poll.

---

## Layout

```
app/
  main.py        FastAPI wiring, /health
  api.py         endpoints — HTTP only, no SQL
  service.py     all database logic and invariants (shared by api AND worker)
  models.py      Job, Certificate
  schemas.py     request/response contracts
  renderer.py    the certificate template — pure, no DB
  storage.py     Storage interface + LocalStorage
  worker.py      claim loop + reaper
  db.py          engine, session, init_db
  config.py      env-driven settings
tests/
  test_jobs.py          creation, validation, status/progress
  test_generation.py    rendering, failure isolation, retries, the queue
  test_retrieval.py     listing, pagination, downloads, archive
  test_concurrency.py   SKIP LOCKED under two workers (PostgreSQL only)
```

The layering rule: **`api.py` speaks HTTP, `service.py` owns the database, `renderer.py`
and `storage.py` know nothing about the database, and `worker.py` is only a driver.** Both
processes import the same `service.py`, so no logic is duplicated between the request path
and the background path.

## Test coverage

| Requirement | Test |
| --- | --- |
| Creating a generation job | `test_create_job_queues_one_certificate_per_recipient` |
| Input validation | `test_envelope_problems_reject_the_whole_request`, `test_too_many_recipients_is_rejected`, `test_invalid_recipients_are_recorded_without_blocking_valid_ones` |
| Certificate generation | `test_renderer_embeds_the_recipient_details`, `test_worker_generates_a_pdf_for_every_recipient` |
| Job status / progress | `test_status_reports_progress` |
| Individual certificate failure | `test_one_failing_certificate_does_not_block_the_others`, `test_a_transient_failure_is_retried` |
| Retrieving certificates | `test_download_a_generated_certificate`, `test_archive_contains_every_generated_certificate` |
| Queue correctness | `test_claimed_rows_are_not_handed_out_twice`, `test_stale_claims_are_requeued_after_the_visibility_timeout`, `test_settling_the_same_certificate_twice_does_not_double_count`, `test_a_second_worker_skips_locked_rows_instead_of_blocking` |
