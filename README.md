# adguard-time-limits

Time limits for your home network, one panel for two kinds of things:

- **Devices** (TV, tablet, laptop…) – internet access via [AdGuard Home](https://github.com/AdguardTeam/AdGuardHome):
  grant a device N minutes, and when the time runs out its DNS queries are refused until you add more.
- **MiniDLNA on OpenMediaVault** – the DLNA media server is switched **on** for the minutes you grant and
  **off** again when the time is up (through the OMV API, so the OMV panel always shows the real state).

Self-hosted, no subscription, no cloud. Small web panel + REST API, optional Home Assistant notifications.

> Unofficial community project. Not affiliated with or endorsed by AdGuard Software Ltd.

Built as a free replacement for the paid parental-control time limits of consumer routers
(e.g. TP-Link HomeShield) when you already run AdGuard Home as your LAN DNS.

## How it works

- You define **profiles**. A profile is either a *device* (e.g. `TV`) with one or more IP addresses
  (a TV often has a wired and a Wi-Fi address), or the *MiniDLNA service* on OpenMediaVault (no IP addresses).
  The time logic (grants, subtract, daily limit, history, notifications) is identical for both.
- You **grant minutes** (`+15`, `+30`, `+60` buttons) in the panel or via the API. The profile becomes *allowed* and the clock runs.
- Granted too much? The **-15 min** button (API: `/subtract`) takes minutes back; at 0 the profile is blocked immediately.
- Every 30 s a scheduler counts the time. When the granted time is used up, the profile is **blocked**.
- Blocking is done by one managed AdGuard **custom filtering rule**:
  `/.*/$client=192.168.1.20|192.168.1.21` – every DNS query from those clients gets an instant refusal.
  Your own custom rules are left untouched; the rule is changed only when the set of blocked IPs changes, and
  no AdGuard restart is ever needed.
- For the **MiniDLNA** profile "allowed" means the service is enabled in OMV and running, "blocked" means it is
  disabled and stopped. netguard changes the *Enabled* switch through `omv-rpc` and applies the module the same way
  the OMV panel does ("Save" + "Apply"), so OMV never shows pending changes. Slow OMV calls run in the background and
  never delay the timer; failures are retried every tick and shown in the panel. After a netguard or OMV restart the
  state from the database is enforced again. Details and setup: [docs/omv-minidlna.md](docs/omv-minidlna.md).
- State lives in SQLite (survives restarts). A reconcile loop re-applies the rule periodically
  (self-healing) and the panel shows whether AdGuard matches the expected state.
- A history of grants, blocks and syncs is kept (90 days) and shown in the panel.
- Optional: a Home Assistant notification every 30 minutes of an active session.

### Why a rule and not "disallowed clients"?

AdGuard's *disallowed clients* silently drops queries. Some devices (notably Samsung TVs) treat a
non-answering DNS server as broken and fall back to a hardcoded one (e.g. 8.8.8.8), bypassing the block.
Answering immediately with a refusal avoids that fallback. The old behaviour is still available with
`BLOCK_MODE=disallow`.

## Limitations (please read)

Devices are controlled by DNS-level blocking. It is a *soft* control:

- It does **not** terminate connections that are already open (a running stream may continue for a while).
- It does not stop devices using hardcoded IPs, DNS-over-HTTPS/TLS to external resolvers, or a VPN.
- Make sure the router hands out **only** your AdGuard as DNS (no secondary resolver, and check IPv6 DNS
  advertisement), otherwise clients can fall back around it.
- Clients are matched by IP – give devices static DHCP leases. A device reaching AdGuard via another
  address (e.g. Tailscale) is a different client.
- The panel has **no authentication**. Run it on a trusted LAN or behind a reverse proxy with login.

For hard enforcement you need a router/firewall-level block in addition to this.

The MiniDLNA control is not DNS based – it really stops the service – but a stream that is already running is
interrupted only when the service stops (up to one scheduler tick after the time runs out, plus the OMV apply time,
usually a few seconds).

## Requirements

- AdGuard Home (tested with v0.107.x) with an admin user
- Python 3.11+ (tested on Debian 13)
- Optional: Home Assistant with a long-lived access token
- Optional, for MiniDLNA: OpenMediaVault 7/8 with the `openmediavault-minidlna` plugin (tested with OMV 8.5 / plugin 8.1),
  SSH reachable from the netguard host, and `openssh-client` installed on it – see [docs/omv-minidlna.md](docs/omv-minidlna.md)

## Install (Debian/Ubuntu, systemd)

```bash
useradd --system --home /opt/netguard --shell /usr/sbin/nologin netguard
git clone https://github.com/kriskomp/adguard-time-limits.git /opt/netguard
cd /opt/netguard
python3 -m venv venv && venv/bin/pip install -r requirements.txt
chown -R netguard:netguard /opt/netguard

mkdir -p /etc/netguard
cp .env.example /etc/netguard/netguard.env   # edit it
chmod 600 /etc/netguard/netguard.env

cp netguard.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now netguard
```

Panel: `http://<host>:8088/`. Set the container/host time zone correctly
(`timedatectl set-timezone Europe/Warsaw`) – the daily counter resets at local midnight.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ADGUARD_URL` | `http://127.0.0.1:3000` | AdGuard Home base URL |
| `ADGUARD_USER` / `ADGUARD_PASS` | – | AdGuard admin credentials |
| `BLOCK_MODE` | `rule` | `rule` (instant refusal) or `disallow` (drop) |
| `HA_URL` / `HA_TOKEN` | – | Home Assistant URL and token (empty = notifications off) |
| `HA_NOTIFY_SERVICE` | `notify.notify` | e.g. `notify.mobile_app_<phone>` |
| `TICK_SECONDS` | `30` | scheduler interval |
| `NOTIFY_EVERY_MIN` | `30` | notification step during a session |
| `RECONCILE_EVERY_TICKS` | `40` | how often AdGuard and OMV are re-synced |
| `OMV_HOST` | – | OpenMediaVault address (empty = MiniDLNA control off) |
| `OMV_SSH_KEY` | – | private key file used for the restricted SSH login |
| `OMV_SSH_USER` / `OMV_SSH_PORT` | `root` / `22` | SSH login on OMV |
| `OMV_KNOWN_HOSTS` | – | `known_hosts` file (use `/opt/netguard/data/known_hosts`, `/etc` is read-only for the service) |
| `OMV_TIMEOUT` | `150` | seconds before one OMV call is given up |

## API

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/profiles` | list / create-or-update profile (`name`, `kind`, `ips`, `daily_limit_min`) |
| DELETE | `/profiles/{name}` | delete profile |
| POST | `/profiles/{name}/grant` | add minutes (`{"minutes": 30}`, range 1-1440) |
| POST | `/profiles/{name}/subtract` | take minutes back (`{"minutes": 15}`, range 1-1440); 409 if blocked or unlimited |
| POST | `/profiles/{name}/block` · `/unblock` | manual block / unblock |
| GET | `/events` | history |
| GET | `/diagnostics` | expected vs actual AdGuard state, last DNS query per IP, expected vs actual state of services (`services`) |
| GET | `/healthz` | health check |

`kind` is `device` (default, needs `ips`) or `minidlna` (no `ips`; only one such profile). The kind of an
existing profile cannot be changed – delete and recreate it. Profiles of kind `minidlna` additionally return a
`service` object (`desired`, `actual`, `error`, `checked_at`).

Example (Home Assistant `rest_command` or curl):

```bash
curl -X POST http://netguard:8088/profiles/TV/grant -H 'Content-Type: application/json' -d '{"minutes": 30}'

# one-time: create the MiniDLNA profile, then give it an hour of DLNA
curl -X POST http://netguard:8088/profiles -H 'Content-Type: application/json' -d '{"name": "DLNA", "kind": "minidlna"}'
curl -X POST http://netguard:8088/profiles/DLNA/grant -H 'Content-Type: application/json' -d '{"minutes": 60}'
```

## Notes

- netguard owns the line `/.*/$client=...` (marked with `! netguard-managed`) in AdGuard's custom rules.
  Don't edit it by hand. In `disallow` mode it owns the whole *disallowed clients* list.
- Fail-open: if AdGuard is unreachable, nothing is blocked and the failure is shown in the panel and history.
- If OMV is unreachable, the service keeps its last state and netguard keeps retrying every tick (the failure is
  shown in the panel and written to the history once). Deleting the MiniDLNA profile switches the service off.
- Disable MiniDLNA in OMV once (Services → DLNA) so it is off by default after an OMV reboot; netguard then only
  turns it on while time is granted.

## Tests

```bash
python3 tests/test_netguard.py
```

No dependencies needed (FastAPI/httpx are stubbed; SQLite logic is tested for real; the OMV client is tested
against a fake). `scripts/omv/dlna.sh` is exercised separately with stubbed `omv-rpc`/`systemctl` (see the docs).

## License

MIT
