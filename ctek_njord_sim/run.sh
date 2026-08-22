#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Starting CtekNjorder ..."
exec python3 -m app.main
