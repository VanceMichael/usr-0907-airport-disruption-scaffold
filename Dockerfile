# Airport disruption API — self-contained runtime image.
#
# The service is standard-library only, so the build needs no package
# downloads beyond the base image and tzdata (for airport-local times).
FROM python:3.12-alpine

RUN apk add --no-cache tzdata \
    && adduser -D -u 10001 app

WORKDIR /srv

COPY app/ ./app/
COPY fixtures/ ./fixtures/
COPY contracts/ ./contracts/

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/disruptions.db \
    FIXTURES_DIR=/srv/fixtures \
    CONTRACT_PATH=/srv/contracts/disruption-event.schema.json

# SQLite lives on a mounted volume so computed results survive restarts.
RUN mkdir -p /data && chown app:app /data
USER app

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).getcode()==200 else 1)"

CMD ["python", "-m", "app"]
