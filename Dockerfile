# syntax=docker/dockerfile:1
# Standalone image for the airport disruption service.
# Pure standard-library Python 3.12 service: no pip dependencies.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DISRUPTION_DB_PATH=/data/disruptions.db \
    FIXTURES_DIR=/srv/app/fixtures

WORKDIR /srv/app

# tzdata backs zoneinfo (airport local-time rendering); the service fails
# fast at startup if the zone database is missing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /srv/app appuser

COPY app/ ./app/
COPY fixtures/ ./fixtures/
COPY contracts/ ./contracts/

RUN mkdir -p /data && chown appuser:appuser /data

USER appuser
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=12 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app"]
