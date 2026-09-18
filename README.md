# Airport disruption service

This repository contains a small, dependency-free Python 3.12 HTTP service
that tells operations teams which flights and passengers are affected when
volcanic ash (or anything else) closes an airport. It ingests closure,
extension and reopening events, computes flight impacts against the fixture
schedule, and persists everything in SQLite so results survive restarts.

## Source context

The scenario was inspired by the China News article "印尼7座机场因火山灰持续关闭 约34.1万旅客受影响", published on 2026-09-07:

https://www.chinanews.com/gj/2026/09-07/10692284.shtml

All airport names, codes, schedules, and passenger counts in this repository are synthetic test data.

## Repository layout

- `app/`: the service (standard library only — no pip dependencies)
- `contracts/disruption-event.schema.json`: the accepted event envelope; the
  service validates incoming events against this file at runtime
- `fixtures/airports.json`, `fixtures/flights.json`: static base data
  (airport time zones and reopen buffers; deterministic schedules, including
  flights around local midnight)
- `tests/`: automated test suite (`python -m unittest discover -s tests`)
- `Dockerfile`: the service image; `Dockerfile.scaffold` + `scaffold/`: the
  original fixture-checking scaffold
- `compose.yaml`: both services plus the `disruption-data` persistent volume
- `scripts/selftest.sh`: end-to-end Docker self-test (see below)

Timestamps in the fixtures are ISO 8601 UTC values. API consumers may submit
equivalent offset timestamps (e.g. `+08:00`); they are normalized to UTC
before any interval comparison, so closure windows crossing midnight — in
UTC or in an airport's local zone — are evaluated correctly.

## Run with Docker Compose

```bash
docker compose up -d --build --wait
curl http://localhost:8080/healthz
```

The API listens on port 8080. Events and computed impacts are stored in the
`disruption-data` volume (`/data/disruptions.db` in the container) and
survive `docker compose restart` / `down` + `up`. Stop with
`docker compose down` (add `-v` to also drop the data).

The scaffold container from the original baseline is unchanged and still
validates the supplied fixtures via its health check.

## Run locally (no Docker)

```bash
python3 -m app            # serves on 0.0.0.0:8080, DB at ./data/disruptions.db
```

Configuration via environment variables: `HOST`, `PORT`, `DB_PATH`,
`FIXTURES_DIR`, `CONTRACT_PATH`.

## API

### `POST /v1/events`

Submit a disruption event (body per `contracts/disruption-event.schema.json`):

```json
{
  "event_id": "evt-aps-closure-01",
  "event_version": 1,
  "event_type": "airport.closed",
  "airport_code": "APS",
  "effective_from": "2026-09-07T18:00:00Z",
  "effective_until": "2026-09-08T00:30:00Z",
  "reported_at": "2026-09-07T17:45:00Z",
  "reason": "volcanic ash cloud"
}
```

Returns `201` with the computed result: the closure window (UTC and
airport-local), an impact summary, and the affected flights.

**Event lifecycle (per airport):**

- `airport.closed` opens a new closure. The airport must not have an active
  closure; `supersedes_event_id` must not be set. `effective_until` may be
  `null` for an indefinite closure.
- `airport.extended` moves the active closure's end. It must set
  `supersedes_event_id` to the current chain head, `event_version` must be
  greater than the superseded event's version, and a finite new
  `effective_until` must be later than the current end.
- `airport.reopened` closes the window at `effective_from` (which must lie
  inside the closure window) and must also reference the chain head with an
  increased version.

**Idempotency:** `event_id` is the idempotency key. Re-submitting a
byte-identical event returns `200` with the originally computed result and
`"idempotent_replay": true`. Re-using the id with a different payload
returns `409 event_conflict`. Failed validations persist nothing, so a
rejected request never produces partial writes.

**Impact rules:** a flight is affected when its scheduled departure (origin
airport) or arrival (destination airport) falls inside the closure window
`[effective_from, effective_until + reopen_buffer_minutes)`:

- window end unknown (indefinite closure) → `pending`
- flight is retimeable and the delay needed to clear the window fits within
  its `max_delay_minutes` → `delayed` (with `delay_minutes`)
- otherwise → `cancelled`

Extensions and reopenings recompute the closure's impacts atomically.

### `GET /v1/events/{event_id}`

Processing status of one event: the normalized payload, its closure's
current state and current impacts, plus receive/process timestamps.
Unknown ids return `404 event_not_found`.

### `GET /v1/airports/{code}/impact-summary`

Per-airport rollup: active closure (if any), totals per impact status with
affected flight/passenger counts, and a per-closure breakdown. Unknown
airport codes return `404 airport_not_found`.

### `GET /v1/impacts?airport_code=&impact_status=&page=&page_size=`

Paginated affected flights (all filters optional). `page` starts at 1,
`page_size` defaults to 20 (max 100). `impact_status` is one of
`cancelled`, `delayed`, `pending`. Invalid parameters return
`400 invalid_query`.

### `GET /healthz`

Liveness/readiness probe used by the Docker and Compose health checks;
verifies the database is reachable.

### Error envelope

All errors share one stable structure:

```json
{"error": {"code": "validation_error", "message": "...", "details": [{"field": "airport_code", "issue": "..."}]}}
```

| HTTP | code | meaning |
|------|------|---------|
| 400 | `validation_error` | payload violates the JSON Schema contract |
| 400 | `invalid_json` | body is not valid JSON |
| 400 | `invalid_query` | bad query parameter |
| 404 | `event_not_found` / `airport_not_found` / `not_found` | unknown resource |
| 405 | `method_not_allowed` | wrong HTTP verb |
| 409 | `event_conflict` | `event_id` re-used with a different payload |
| 409 | `state_conflict` | conflicts with the closure chain (active closure exists, none exists, supersedes is not the head, version not increasing) |
| 413 | `payload_too_large` | body exceeds 1 MiB |
| 422 | `unknown_airport` | airport code not in the fixtures |
| 422 | `invalid_time_window` | inverted/shortening window, reopen time outside the closure |
| 422 | `invalid_event_sequence` | payload-level lifecycle violation (e.g. `closed` with `supersedes_event_id`) |
| 500 | `internal_error` | unexpected server error |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

67 tests cover timestamp strictness, contract validation, impact
classification (including cross-midnight windows), the full HTTP API, and
persistence across a server restart.

## Docker self-test

```bash
scripts/selftest.sh
```

Requires only Docker on the host. The script builds the image, starts the
service with a fresh named volume, waits for the health check, then runs
`scripts/selftest_client.py` inside the container to verify valid events,
invalid events (stable errors, no partial writes), idempotent replay,
cross-midnight computation and the query endpoints; it then restarts the
container and verifies that events, impacts and idempotency all survived.
It exits non-zero on the first failed check and cleans up its container and
volume. No external flight or map services are ever contacted — the service
itself has no third-party dependencies at all.
