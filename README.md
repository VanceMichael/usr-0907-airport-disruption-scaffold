# Airport disruption scaffold

This repository is an implementation scaffold. It contains domain fixtures, an input contract, and a containerized fixture-checking workspace, but no airport disruption API or persistence implementation.

## Source context

The scenario was inspired by the China News article "印尼7座机场因火山灰持续关闭 约34.1万旅客受影响", published on 2026-09-07:

https://www.chinanews.com/gj/2026/09-07/10692284.shtml

All airport names, codes, schedules, and passenger counts in this repository are synthetic test data.

## Supplied material

- `contracts/disruption-event.schema.json`: accepted event envelope
- `fixtures/airports.json`: airport time zones and operational buffers
- `fixtures/flights.json`: deterministic schedule data, including flights around local midnight
- `compose.yaml`: a minimal scaffold container that validates the supplied inputs and then remains available for development
- `scaffold/validate_inputs.sh`: dependency-free integrity checks for the starting data

Timestamps in the fixtures are ISO 8601 UTC values. API consumers may submit equivalent offset timestamps, which must be normalized before interval comparison. The fixture identifiers and field meanings are part of the contract and must not be silently changed.

## Start the scaffold environment

```bash
docker compose up -d --build --wait
docker compose exec scaffold sh scaffold/validate_inputs.sh
```

The container health check runs the same validation. Stop the environment with `docker compose down`. The future application service, its image, storage volume, and application health endpoint are intentionally not part of this baseline.
