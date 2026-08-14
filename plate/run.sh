#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

LOG_LEVEL=$(bashio::config 'log_level')
export PLATE_LOG_LEVEL="${LOG_LEVEL}"

# The add-on options file is the single source of truth for configuration; the
# app reads it directly rather than us exporting three dozen env vars.
export PLATE_OPTIONS_FILE="/data/options.json"
export PLATE_DATA_DIR="/data"
export PLATE_USER_DIR="/config"
export PLATE_INGRESS_ENTRY="$(bashio::addon.ingress_entry || echo '')"

bashio::log.info "Starting PLATE on :8099 (ingress entry: ${PLATE_INGRESS_ENTRY:-none})"

exec python3 -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8099 \
  --app-dir /opt/plate \
  --log-level "$(bashio::string.lower "${LOG_LEVEL}")" \
  --no-access-log \
  --proxy-headers \
  --forwarded-allow-ips '*'
