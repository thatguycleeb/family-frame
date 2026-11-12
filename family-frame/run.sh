#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Starting Family Frame"
exec python3 -u /app/sync.py --daemon
