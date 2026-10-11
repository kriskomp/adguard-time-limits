"""
netguard - serwis limitów czasu internetu per urządzenie (IP),
egzekwowany przez blokadę DNS w AdGuard Home.

Konfiguracja przez zmienne środowiskowe (patrz .env.example).
Uruchomienie: uvicorn app:app --host 0.0.0.0 --port 8088
"""
import asyncio
import ipaddress
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from adguard_client import AdGuardClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("netguard")

ADGUARD_URL = os.environ.get("ADGUARD_URL", "http://127.0.0.1:3000")
ADGUARD_USER = os.environ.get("ADGUARD_USER", "")
ADGUARD_PASS = os.environ.get("ADGUARD_PASS", "")
# "rule" (domyślnie): reguła filtrująca, natychmiastowa odmowa; "disallow": stara lista disallowed_clients
BLOCK_MODE = os.environ.get("BLOCK_MODE", "rule").strip().lower()

HA_URL = os.environ.get("HA_URL", "")                # np. http://homeassistant.local:8123
HA_TOKEN = os.environ.get("HA_TOKEN", "")             # Long-Lived Access Token
HA_NOTIFY_SERVICE = os.environ.get("HA_NOTIFY_SERVICE", "notify.notify")

TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "30"))
NOTIFY_EVERY_MIN = int(os.environ.get("NOTIFY_EVERY_MIN", "30"))
RECONCILE_EVERY_TICKS = int(os.environ.get("RECONCILE_EVERY_TICKS", "40"))  # ~co 20 min przy tick=30s

adguard = AdGuardClient(ADGUARD_URL, ADGUARD_USER, ADGUARD_PASS)
app = FastAPI(title="netguard")


class ProfileIn(BaseModel):
    name: str
    ips: list[str] = []
    ip: str | None = None  # zgodność wsteczna ze starym API (pojedynczy adres)
    daily_limit_min: int = 0


def _clean_ips(p: ProfileIn) -> list[str]:
    """Łączy ips + ip, rozdziela po przecinkach/spacjach, waliduje i usuwa duplikaty."""
    raw = list(p.ips)
    if p.ip:
        raw.append(p.ip)
    cleaned: list[str] = []
    for item in raw:
        for part in item.replace(",", " ").replace(";", " ").split():
            try:
                addr = str(ipaddress.ip_address(part))
            except ValueError:
                raise HTTPException(422, f"Nieprawidłowy adres IP: {part}")
            if addr not in cleaned:
                cleaned.append(addr)
    if not cleaned:
        raise HTTPException(422, "Podaj co najmniej jeden adres IP")
    return cleaned


class GrantIn(BaseModel):
    minutes: int = 30


# --------------------------------------------------------------------------
# Pomocnicze
# --------------------------------------------------------------------------

async def notify_ha(title: str, message: str):
    if not HA_URL or not HA_TOKEN:
        logger.info("HA_URL/HA_TOKEN nieustawione - pomijam powiadomienie: %s", message)
        return
    domain, service = HA_NOTIFY_SERVICE.split(".", 1)
    url = f"{HA_URL}/api/services/{domain}/{service}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, headers=headers, json={"title": title, "message": message})
    except Exception:  # noqa: BLE001
        logger.exception("Nie udało się wysłać powiadomienia do HA")


async def sync_adguard_from_db(reason: str = "reconcile"):
    """Odbudowuje listę disallowed_clients na podstawie stanu w SQLite (self-healing)."""
    ips = db.blocked_ips()
    if BLOCK_MODE == "disallow":
        ok = await adguard.sync_disallowed(ips)
    else:
        # tryb "rule": szybka odmowa regułą; stara lista disallowed_clients zostaje opróżniona
        ok = await adguard.sync_block_rule(ips)
        if ok:
            ok = await adguard.sync_disallowed(set())
    if not ok:
        logger.error("Reconcile z AdGuard nieudany - stan lokalny i AdGuard mogą być niespójne")
    # zwykły okresowy reconcile zapisujemy tylko gdy się nie udał; zmiany zawsze
    if reason != "reconcile" or not ok:
        db.add_event("*", "adguard_sync_ok" if ok else "adguard_sync_FAIL",
                     f"powód={reason}; blokowane={sorted(ips) or 'brak'}")
    return ok


# --------------------------------------------------------------------------
# Scheduler w tle
# --------------------------------------------------------------------------

_last_tick_ts = 0.0          # kiedy scheduler ostatnio przeszedł pętlę (heartbeat)
_scheduler_task = None      # silna referencja - bez niej asyncio może zgubić zadanie


async def scheduler_loop():
    global _last_tick_ts
    tick_no = 0
    _last_tick_ts = time.time()
    while True:
        await asyncio.sleep(TICK_SECONDS)
        tick_no += 1
        try:
            await scheduler_tick(force_reconcile=(tick_no % RECONCILE_EVERY_TICKS == 0))
        except Exception:  # noqa: BLE001 - scheduler nie może się wywrócić na stałe
            logger.exception("Błąd w pętli schedulera")
        _last_tick_ts = time.time()


def start_scheduler():
    """Uruchamia pętlę schedulera i restartuje ją, gdyby kiedykolwiek się zakończyła."""
    global _scheduler_task
    task = asyncio.create_task(scheduler_loop())
    _scheduler_task = task

    def _on_done(t):
        if t.cancelled():
            return
        exc = t.exception()
        logger.error("Pętla schedulera zakończyła się (%r) - restartuję", exc)
        try:
            db.add_event("*", "scheduler_restart", repr(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
        start_scheduler()

    task.add_done_callback(_on_done)


async def scheduler_tick(force_reconcile: bool = False):
    elapsed_min = TICK_SECONDS / 60.0
    changed = False

    for p in db.list_profiles():
        db.reset_daily_if_needed(p["name"])
        if p["status"] != "allowed":
            continue

        db.apply_tick(p["name"], elapsed_min)
        fresh = db.get_profile(p["name"])

        # limit "doładowanego" czasu się skończył -> blokujemy
        if fresh["remaining_grant_min"] <= 0:
            db.set_status(p["name"], "blocked", reset_session=True)
            db.add_event(p["name"], "time_expired", "skończył się przyznany czas -> blokada")
            changed = True
            await notify_ha(
                "Internet zablokowany",
                f"{fresh['name']}: czas się skończył, dostęp zablokowany.",
            )
            continue

        # dzienny limit (jeśli ustawiony) się skończył -> blokujemy
        if fresh["daily_limit_min"] and fresh["used_today_min"] >= fresh["daily_limit_min"]:
            db.set_status(p["name"], "blocked", reset_session=True)
            db.add_event(p["name"], "daily_limit", f"dzienny limit {fresh['daily_limit_min']} min -> blokada")
            changed = True
            await notify_ha(
                "Internet zablokowany",
                f"{fresh['name']}: wykorzystano dzienny limit {fresh['daily_limit_min']} min.",
            )
            continue

        # powiadomienia co NOTIFY_EVERY_MIN minut sesji
        if fresh["session_started_at"]:
            session_min = (time.time() - fresh["session_started_at"]) / 60.0
            threshold = int(session_min // NOTIFY_EVERY_MIN) * NOTIFY_EVERY_MIN
            if threshold > 0 and threshold > fresh["last_notified_min"]:
                db.set_last_notified(p["name"], threshold)
                await notify_ha(
                    "Internet aktywny",
                    f"{fresh['name']}: minęło {threshold} min od startu okresu.",
                )

    if changed:
        await sync_adguard_from_db("scheduler")
    elif force_reconcile:
        await sync_adguard_from_db("reconcile")


@app.on_event("startup")
async def on_startup():
    db.init_db()
    db.add_event("*", "service_start", "netguard wystartował")
    await sync_adguard_from_db("start")  # odbuduj stan AdGuard po restarcie kontenera
    start_scheduler()
    logger.info("netguard wystartował, tick=%ss", TICK_SECONDS)


# --------------------------------------------------------------------------
# REST API
# --------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "time": time.time()}


@app.get("/profiles")
async def api_list_profiles():
    return db.list_profiles()


@app.post("/profiles")
async def api_upsert_profile(p: ProfileIn):
    name = p.name.strip()
    if not name:
        raise HTTPException(422, "Nazwa profilu nie może być pusta")
    ips = _clean_ips(p)
    try:
        db.upsert_profile(name, ips, max(0, p.daily_limit_min))
    except db.IpInUseError as exc:
        raise HTTPException(409, f"Adres {exc.ip} należy już do profilu '{exc.owner}'")
    db.add_event(name, "profile_saved", f"ips={ips}; dzienny_limit={max(0, p.daily_limit_min)}")
    # stan w AdGuard ma od razu odpowiadać temu, co pokazuje panel
    await sync_adguard_from_db("profile_saved")
    return db.get_profile(name)


@app.delete("/profiles/{name}")
async def api_delete_profile(name: str):
    if not db.get_profile(name):
        raise HTTPException(404, "brak takiego profilu")
    db.delete_profile(name)
    db.add_event(name, "profile_deleted", "")
    await sync_adguard_from_db("profile_deleted")
    return {"ok": True}


@app.post("/profiles/{name}/grant")
async def api_grant(name: str, body: GrantIn):
    if not db.get_profile(name):
        raise HTTPException(404, "brak takiego profilu")
    db.grant_minutes(name, body.minutes)
    db.add_event(name, "grant", f"+{body.minutes} min -> dostęp przyznany")
    await sync_adguard_from_db("grant")
    return db.get_profile(name)


@app.post("/profiles/{name}/block")
async def api_block(name: str):
    if not db.get_profile(name):
        raise HTTPException(404, "brak takiego profilu")
    db.set_status(name, "blocked", reset_session=True)
    db.add_event(name, "manual_block", "ręczna blokada z panelu/API")
    await sync_adguard_from_db("manual_block")
    return db.get_profile(name)


@app.post("/profiles/{name}/unblock")
async def api_unblock(name: str):
    """Odblokowanie bez określonego limitu czasu - dostęp 'do odwołania'."""
    if not db.get_profile(name):
        raise HTTPException(404, "brak takiego profilu")
    db.grant_minutes(name, 10**6)  # praktycznie "bez limitu"; użyj /block żeby zdjąć
    db.add_event(name, "unblock_unlimited", "odblokowanie bez limitu czasu")
    await sync_adguard_from_db("unblock_unlimited")
    return db.get_profile(name)


@app.get("/events")
async def api_events(limit: int = 100, profile: str | None = None):
    """Historia zmian stanu (blokady, przyznany czas, synchronizacje z AdGuard)."""
    return db.list_events(limit, profile)


@app.get("/diagnostics")
async def api_diagnostics():
    """
    Porównuje to, co netguard UWAŻA za zablokowane, z tym, co faktycznie jest
    w AdGuard, i pokazuje kiedy dany adres ostatnio pytał o DNS.
    Tylko odczyt - niczego nie zmienia.
    """
    expected = db.blocked_ips()
    out = {
        "db_blocked": sorted(expected),
        "adguard_disallowed": None,
        "in_sync": None,
        "adguard_error": None,
        "last_queries": {},
        "querylog_error": None,
        "scheduler_last_tick_age_s": round(time.time() - _last_tick_ts) if _last_tick_ts else None,
    }
    try:
        if BLOCK_MODE == "disallow":
            access = await adguard.get_access_list()
            actual = set(access.get("disallowed_clients", []))
        else:
            actual = adguard.rule_ips(await adguard.get_user_rules())
        out["adguard_disallowed"] = sorted(actual)
        out["in_sync"] = actual == expected
    except Exception as exc:  # noqa: BLE001
        out["adguard_error"] = str(exc)[:200]

    managed = {ip for p in db.list_profiles() for ip in p["ips"]}
    if managed:
        try:
            log = await adguard.get_querylog(500)
            for entry in log.get("data", []):
                client = entry.get("client")
                if client in managed and client not in out["last_queries"]:
                    out["last_queries"][client] = {
                        "time": entry.get("time"),
                        "domain": (entry.get("question") or {}).get("name"),
                        "reason": entry.get("reason"),
                    }
        except Exception as exc:  # noqa: BLE001
            out["querylog_error"] = str(exc)[:200]
    return out


# --------------------------------------------------------------------------
# Prosty web UI
# --------------------------------------------------------------------------

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")
