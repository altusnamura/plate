ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/plate

# Build deps only for the wheel-less packages, dropped in the same layer.
COPY requirements.txt ./
RUN apk add --no-cache --virtual .build-deps gcc musl-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

COPY app ./app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL io.hass.name="PLATE" \
      io.hass.description="Adaptive menu planner driven by your health metrics" \
      io.hass.type="addon"

CMD ["/run.sh"]
