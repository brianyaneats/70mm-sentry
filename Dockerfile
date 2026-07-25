# stdlib only — no pip install, no requirements.txt, no supply chain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    SENTRY_DATA=/data

WORKDIR /app
COPY sentry.py watchdog.py entrypoint.sh ./
RUN chmod +x entrypoint.sh \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin sentry \
    && mkdir -p /data && chown sentry:sentry /data

# config.json, state.json and logs live on the volume; the image stays
# read-only and secret-free so it is safe to push to a registry
VOLUME ["/data"]
USER sentry

HEALTHCHECK --interval=5m --timeout=20s --start-period=90s --retries=2 \
    CMD ["python", "/app/watchdog.py"]

ENTRYPOINT ["/app/entrypoint.sh"]
