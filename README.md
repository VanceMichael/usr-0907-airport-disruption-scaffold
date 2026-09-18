# Airport disruption service

A pure-backend Python 3.12 HTTP service that tracks airport closures caused
by volcanic ash and computes which flights and passengers are affected. It
ingests disruption events (`airport.closed`, `airport.extended`,
`airport.reopened`), computes flight impacts from the fixture schedule, and
persists everything in SQLite so state survives restarts.

The service has **no third-party dependencies** (standard library only) and
makes **no external calls** — all data comes from the repository fixtures.

## Repository layout

- `app/` — the service package (`python -m app` starts it)
- `contracts/disruption-event.schema.json` — accepted event envelope
- `fixtures/airports.json` — airport time zones and reopen buffers
- `fixtures/flights.json` — deterministic schedule data, including flights around local midnight
- `tests/` — pytest suite (validation, impact math, API, persistence)
- `scripts/docker_selftest.sh` — end-to-end Docker self-test
- `Dockerfile` — standalone image for the service
- `Dockerfile.scaffold`, `scaffold/` — the original fixture-checking scaffold (unchanged)
- `compose.yaml` — runs both the scaffold and the service + persistent volume

## Event model

Events are submitted as JSON to `POST /events` and must conform to
`contracts/disruption-event.schema.json`. Timestamps may use any ISO 8601
offset form (`Z`, `+07:00`, ...); they are normalised to UTC before any
interval comparison, so closure windows crossing local or UTC midnight are
handled correctly.

Closure chains per airport:

- `airport.closed` opens a new chain. Rejected (409) while another chain is
  open for that airport. `effective_until` may be `null` (open-ended).
- `airport.extended` supersedes the current chain head: it must set
  `supersedes_event_id`, restate the original `effective_from`, move
  `effective_until` strictly forward, and carry a greater `event_version`.
- `airport.reopened` supersedes the current chain head and closes the
  chain; the closure ends at its `effective_from`.

**Idempotency:** submissions are idempotent on `event_id`. Replaying a
byte-identical event returns the original computed result (HTTP 200); the
same id with a different payload is rejected (409 `event_id_conflict`).

**Atomicity:** an event, its impacts, and the supersession of the previous
chain head are written in a single SQLite transaction. Rejected events
leave no partial state behind.

## Impact computation

A flight is affected when its operation at the closed airport (departure
from it, or arrival at it) falls inside `[effective_from, effective_until +
reopen_buffer)`:

- **pending** — the closure is open-ended; no decision can be made yet.
- **delayed** — the flight can absorb the required shift within its
  `max_delay_minutes` allowance (`delay_minutes` reports the shift).
- **cancelled** — the flight cannot be retimed, or the required shift
  exceeds its allowance.

When a closure is extended or reopened, the previous event's impacts are
marked superseded and impacts are recomputed against the new window.

## API

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/events` | Submit a disruption event. `201` on success, `200` on idempotent replay. |
| `GET` | `/events/{event_id}` | Processing status, chain links, and impacts of one event. |
| `GET` | `/airports/{code}/impact-summary` | Current impact counts, affected flights/passengers, open closure. |
| `GET` | `/impacts/flights` | Paginated affected flights. Params: `airport_code`, `impact_status`, `include_superseded`, `page`, `page_size` (max 100). |
| `GET` | `/health` | Health check (database + fixtures). |

Errors always use a stable envelope:

```json
{"error": {"code": "invalid_time_window", "message": "...", "details": [{"field": "...", "issue": "..."}]}}
```

Codes: `malformed_json` (400), `schema_violation` (400),
`invalid_parameter` (400), `not_found` (404), `method_not_allowed` (405),
`payload_too_large` (413), `unsupported_media_type` (415),
`unknown_airport` (404/422), `invalid_time_window` (422),
`unknown_superseded_event` (422), `invalid_supersede_target` (422),
`event_id_conflict` (409), `event_version_conflict` (409),
`invalid_state_transition` (409), `internal_error` (500).

## Run with Docker Compose

```bash
docker compose up -d --build --wait
curl http://localhost:8080/health
```

The service listens on port 8080 (override the host port with
`DISRUPTION_PORT`). Events and impacts are stored in the `disruption-data`
named volume (`/data/disruptions.db` in the container) and survive
container restarts and recreations. The scaffold container from the
baseline is unchanged and still validates the fixtures via its health
check.

## Run locally

```bash
python3 -m app            # serves on 0.0.0.0:8080, DB at ./data/disruptions.db
```

Configuration via environment: `HOST`, `PORT`, `DISRUPTION_DB_PATH`,
`FIXTURES_DIR`.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

## Docker self-test

`scripts/docker_selftest.sh` verifies the full deliverable in a clean
environment: it builds the image, starts the stack, checks valid events,
invalid events, idempotent replay, and cross-midnight computation, then
recreates the container on the same volume and confirms that all events,
impacts, and idempotency guarantees survived. It tears everything down
afterwards (`KEEP_STACK=1` keeps it running).

```bash
scripts/docker_selftest.sh
```

## Source context

The scenario was inspired by the China News article "印尼7座机场因火山灰持续关闭 约34.1万旅客受影响", published on 2026-09-07:

https://www.chinanews.com/gj/2026/09-07/10692284.shtml

All airport names, codes, schedules, and passenger counts in this
repository are synthetic test data. Timestamps in the fixtures are ISO 8601
UTC values; API consumers may submit equivalent offset timestamps, which
are normalised before interval comparison. The fixture identifiers and
field meanings are part of the contract and must not be silently changed.
