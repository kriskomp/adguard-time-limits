# adguard-time-limits

Per-device internet time limits for [AdGuard Home](https://github.com/AdguardTeam/AdGuardHome):
grant a device N minutes, and when the time runs out its DNS queries are refused until you add more.
Self-hosted, no subscription, no cloud. Small web panel + REST API, optional Home Assistant notifications.

> Unofficial community project. Not affiliated with or endorsed by AdGuard Software Ltd.

Built as a free replacement for the paid parental-control time limits of consumer routers
(e.g. TP-Link HomeShield) when you already run AdGuard Home as your LAN DNS.

## How it works

- You define **profiles** (e.g. `TV`) with one or more IP addresses (a TV often has a wired and a Wi-Fi address).
- You **grant minutes** (`+30`, `+60`) in the panel or via the API. The profile becomes *allowed* and the clock runs.
- Every 30 s a scheduler counts the time. When the granted time is used up, the profile is **blocked**.
- Blocking is done by one managed AdGuard **custom filtering rule**:
  `/.*/$client=192.168.1.20|192.168.1.21` – every DNS query from those clients gets an instant refusal.
  Your own custom rules are left untouched; the rule is changed only when the set of blocked IPs changes, and
  no AdGuard restart is ever needed.
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

This is DNS-level blocking. It is a *soft* control:

- It does **not** terminate connections that are already open (a running stream may continue for a while).
- It does not stop devices using hardcoded IPs, DNS-over-HTTPS/TLS to external resolvers, or a VPN.
- Make sure the router hands out **only** your AdGuard as DNS (no secondary resolver, and check IPv6 DNS
  advertisement), otherwise clients can fall back around it.
- Clients are matched by IP – give devices static DHCP leases. A device reaching AdGuard via another
  address (e.g. Tailscale) is a different client.
- The panel has **no authentication**. Run it on a trusted LAN or behind a reverse proxy with login.

For hard enforcement you need a router/firewall-level block in addition to this.

## Requirements

- AdGuard Home (tested with v0.107.x) with an admin user
- Python 3.11+ (tested on Debian 13)
- Optional: Home Assistant with a long-lived access token

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
| `RECONCILE_EVERY_TICKS` | `40` | how often AdGuard is re-synced |

## API

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/profiles` | list / create-or-update profile (`name`, `ips`, `daily_limit_min`) |
| DELETE | `/profiles/{name}` | delete profile |
| POST | `/profiles/{name}/grant` | add minutes (`{"minutes": 30}`) |
| POST | `/profiles/{name}/block` · `/unblock` | manual block / unblock |
| GET | `/events` | history |
| GET | `/diagnostics` | expected vs actual AdGuard state, last DNS query per IP |
| GET | `/healthz` | health check |

Example (Home Assistant `rest_command` or curl):

```bash
curl -X POST http://netguard:8088/profiles/TV/grant -H 'Content-Type: application/json' -d '{"minutes": 30}'
```

## Notes

- netguard owns the line `/.*/$client=...` (marked with `! netguard-managed`) in AdGuard's custom rules.
  Don't edit it by hand. In `disallow` mode it owns the whole *disallowed clients* list.
- Fail-open: if AdGuard is unreachable, nothing is blocked and the failure is shown in the panel and history.

## Tests

```bash
python3 tests/test_netguard.py
```

No dependencies needed (FastAPI/httpx are stubbed; SQLite logic is tested for real).

## License

MIT
