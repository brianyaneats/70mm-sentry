# Dropwatch: API + poller + dispatch runner in one process. stdlib only,
# no pip install. Build from the repo root:
#   docker build -f deploy/Dockerfile -t dropwatch .
# (The root Dockerfile is the retired 70mm sentry watcher; unrelated.)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8787 \
    DROPWATCH_DB=/data/dropwatch.sqlite3 \
    DROPWATCH_CAMPGROUNDS=/app/app/campgrounds.json

WORKDIR /app
COPY sources/ sources/
COPY engine/ engine/
COPY notify/ notify/
COPY billing/ billing/
COPY app/ app/
COPY deploy/__init__.py deploy/backup.py deploy/
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin dropwatch \
    && mkdir -p /data /backups && chown dropwatch:dropwatch /data /backups

# subscriptions live here; without a volume every redeploy forgets them
VOLUME ["/data"]
USER dropwatch
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/status' % os.environ['PORT'], timeout=4)"]

# exec form: python is PID 1 and receives SIGTERM directly
CMD ["python", "-m", "app.serve"]
