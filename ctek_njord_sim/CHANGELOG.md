# Changelog

## 0.2.0

- Installable as a Home Assistant add-on repository: added `repository.yaml`,
  add-on documentation and this changelog.
- Pinned `WORKDIR /` in the Dockerfile so `python3 -m app.main` cannot break if
  the base image changes its default working directory.
- Pointed the add-on `url` at the real repository.

## 0.1.0

First release.

- Impersonates the CTEK Nanogrid Air on the charger's own MQTT broker.
- Load-balances against per-phase current from Home Assistant.
- Auto-discovers the charger serial from the broker's retained topics.
- Ships with `dry_run` enabled: decisions are logged, nothing is sent to the
  charger until you turn it off.
