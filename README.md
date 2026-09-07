# Airport disruption scaffold

This repository is an implementation scaffold. It contains domain fixtures and an input contract, but no application code.

## Source context

The scenario was inspired by the China News article "印尼7座机场因火山灰持续关闭 约34.1万旅客受影响", published on 2026-09-07:

https://www.chinanews.com/gj/2026/09-07/10692284.shtml

All airport names, codes, schedules, and passenger counts in this repository are synthetic test data.

## Supplied material

- `contracts/disruption-event.schema.json`: accepted event envelope
- `fixtures/airports.json`: airport time zones and operational buffers
- `fixtures/flights.json`: deterministic schedule data, including flights around local midnight

Timestamps in the fixtures are ISO 8601 UTC values. API consumers may submit equivalent offset timestamps, which must be normalized before interval comparison. The fixture identifiers and field meanings are part of the contract and must not be silently changed.

