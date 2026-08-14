# Standalone image — no Home Assistant required.
#
# This is NOT the add-on image. `plate/Dockerfile` builds that one, starting from
# Home Assistant's base image and expecting the Supervisor to provide config,
# tokens and Ingress. This one stands on its own: a plain Python container that
# stores everything in two mounted volumes.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLATE_DATA_DIR=/data \
    PLATE_USER_DIR=/config \
    PLATE_OPTIONS_FILE=/data/options.json

WORKDIR /opt/plate

COPY plate/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY plate/app ./app

# Runs unprivileged. The volumes are chowned at build time so a fresh `docker
# compose up` on an empty host directory doesn't fail on permissions.
RUN useradd --create-home --uid 1000 plate \
    && mkdir -p /data /config \
    && chown -R plate:plate /data /config /opt/plate
USER plate

EXPOSE 8099

HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8099/healthz', timeout=5).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8099", \
     "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
