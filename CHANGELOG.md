# Changelog

Versions are date-based: `YYYY.MM.DD.N` (date + number of the change that day).

## 2026.10.28.1
- **New: MiniDLNA on OpenMediaVault** is now controlled like a device: a profile of kind `minidlna` switches the
  service on for the granted minutes and off when the time is up (also on manual *Blokuj* and on daily limit).
  Done through the OMV API (`omv-rpc`) over a key-restricted SSH login, so the OMV panel never shows pending changes.
- OMV calls run in the background (never delay the timer), failures are retried every tick and shown in the panel;
  state is enforced again after a netguard/OMV restart and on the periodic reconcile.
- API: `kind` field on `/profiles` (`device` default, `minidlna`); `service` object on profile responses;
  `services` in `/diagnostics`. Existing databases are migrated automatically (all current profiles stay devices).
- Panel: profile type selector, service state column, service-aware banners.
- New files: `omv_client.py`, `scripts/omv/dlna.sh` (install on OMV), `docs/omv-minidlna.md`.
- New settings: `OMV_HOST`, `OMV_SSH_KEY`, `OMV_SSH_USER`, `OMV_SSH_PORT`, `OMV_KNOWN_HOSTS`, `OMV_TIMEOUT`.
- Needs `openssh-client` on the netguard host.
- README: project description now covers both DNS and MiniDLNA control.

## 2026.10.23.1
- Panel: new **+15 min** button (smallest grant step); the correction button is now **-15 min** (was -30).
- README: documented `/subtract` and the 1-1440 minute range.

## 2026.10.19.1
- New **-30 min** button and `POST /profiles/{name}/subtract` to correct over-granted time; blocks immediately when time reaches 0.
- `/grant` and `/subtract` validate `minutes` in 1-1440 (a negative `/grant` used to subtract time).

## 2026.10.11.1
- Initial public release.
