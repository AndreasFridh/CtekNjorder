#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Starting CTEK Njord Load Balancer ..."
exec python3 -m app.main
