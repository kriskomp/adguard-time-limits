"""
netguard - serwis limitów czasu per urządzenie (IP) i per usługa.
Urządzenia: blokada DNS w AdGuard Home. Usługi: MiniDLNA na OpenMediaVault
(włączane na przyznany czas przez API OMV).

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
from omv_client import OmvClient, OmvError

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

# OpenMediaVault (opcjonalnie): sterowanie MiniDLNA przez SSH -> scripts/omv/dlna.sh
OMV_HOST = os.environ.get("OMV_HOST", "")
OMV_SSH_USER = os.environ.get("OMV_SSH_USER", "root")
OMV_SSH_KEY = os.environ.get("OMV_SSH_KEY", "")
OMV_SSH_PORT = int(os.environ.get("OMV_SSH_PORT", "22"))
OMV_KNOWN_HOSTS = os.environ.get("OMV_KNOWN_HOSTS", "")
OMV_TIMEOUT = float(os.environ.get("OMV_TIMEOUT", "150"))

adguard = AdGuardClient(ADGUARD_URL, ADGUARD_USER, ADGUARD_PASS)
omv = OmvClient(OMV_HOST, OMV_SSH_USER, OMV_SSH_KEY, OMV_SSH_PORT, OMV_KNOWN_HOSTS, OMV_TIMEOUT)
app = FastAPI(title="netguard")


class ProfileIn(BaseModel):
    name: str
    kind: str | None = None  # device (domyślnie) | minidlna; None = zostaw rodzaj istniejącego profilu
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


MAX_GRANT_MIN = 1440               # maks. jednorazowa zmiana czasu (doba)
UNLIMITED_THRESHOLD_MIN = 100000   # powyżej = "bez limitu" (zob. /unblock)


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
# Usługi (MiniDLNA na OMV) - uzgadnianie stanu w tle
# --------------------------------------------------------------------------
# Wywołania SSH/OMV trwają długo (wdrożenie zmian w OMV to kilka-kilkanaście sekund),
# więc nigdy nie blokują schedulera (inaczej odliczanie czasu by się spóźniało)
# ani odpowiedzi API. Zmiany robi zadanie w tle; scheduler ponawia je, gdy się nie udały.

_service_state: dict[str, dict] = {}   # name -> {desired, actual, error, ts}
_service_lock = asyncio.Lock()
_service_task = None


def _service_profiles():
    return [p for p in db.list_profiles() if p["kind"] in db.SERVICE_KINDS]


def _service_pending() -> bool:
    """Czy któraś usługa nie ma jeszcze zastosowanego oczekiwanego stanu."""
    names = set()
    for p in _service_profiles():
        names.add(p["name"])
        st = _service_state.get(p["name"])
        if st is None or st["actual"] != (p["status"] == "allowed"):
            return True
    return any(n not in names for n in _service_state)   # usunięte, jeszcze do wyłączenia


async def sync_services(reason: str = "reconcile", force: bool = False):
    """
    Doprowadza usługi do stanu z bazy: dozwolony -> włączona, zablokowany -> wyłączona.
    Bez force wywołuje OMV tylko tam, gdzie stan zastosowany != oczekiwany.
    Usunięty profil usługi powoduje jej wyłączenie (netguard przestaje ją pilnować,
    więc nie zostawiamy jej włączonej).
    """
    async with _service_lock:
        wanted = {p["name"]: p["status"] == "allowed" for p in _service_profiles()}
        for gone in [n for n in _service_state if n not in wanted]:
            wanted[gone] = False
        for name, want in wanted.items():
            st = _service_state.setdefault(name, {"desired": want, "actual": None, "error": None, "ts": 0.0})
            st["desired"] = want
            if st["actual"] == want and not force:
                continue
            try:
                res = await omv.set_enabled(want)
            except (OmvError, OSError) as exc:
                msg = str(exc)[:300]
                if st["error"] != msg:  # nie zaśmiecamy historii powtarzającym się błędem
                    db.add_event(name, "service_FAIL", f"powód={reason}; {msg}")
                st["error"], st["ts"] = msg, time.time()
                logger.error("OMV: nie udało się ustawić %s=%s: %s", name, want, msg)
                continue
            changed = st["actual"] != res["enabled"]
            st.update(actual=res["enabled"], error=None, ts=time.time())
            if res["enabled"] != want:
                st["error"] = f"OMV zgłasza enabled={res['enabled']}, oczekiwano {want}"
            if changed or reason != "reconcile":
                db.add_event(name, "service_on" if res["enabled"] else "service_off",
                             f"powód={reason}; enabled={res['enabled']} active={res['active']}")
            if name not in [p["name"] for p in _service_profiles()] and res["enabled"] == want:
                _service_state.pop(name, None)  # usunięty profil, usługa już wyłączona


_service_rerun = None   # (reason, force) zgłoszone, gdy zadanie jeszcze trwało


async def _service_runner(reason: str, force: bool):
    global _service_rerun
    while True:
        await sync_services(reason, force)
        if _service_rerun is None:
            return
        (reason, force), _service_rerun = _service_rerun, None


def schedule_service_sync(reason: str, force: bool = False):
    """Uruchamia sync_services w tle (jedno zadanie naraz; zgłoszenia w trakcie są scalane)."""
    global _service_task, _service_rerun
    if not _service_profiles() and not _service_state:
        return
    if _service_task is not None and not _service_task.done():
        prev = _service_rerun
        _service_rerun = (reason, force or bool(prev and prev[1]))
        return
    _service_task = asyncio.create_task(_service_runner(reason, force))


async def flush_services():
    """Czeka na zakończenie zadania w tle (używane w testach)."""
    while _service_task is not None and not _service_task.done():
        await asyncio.wait([_service_task])


async def apply_changes(reason: str, devices: bool = True, services: bool = True):
    """Po zmianie stanu: AdGuard od razu (szybkie), usługi w tle."""
    if devices:
        await sync_adguard_from_db(reason)
    if services:
        schedule_service_sync(reason)


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


def _mark(p: dict, changed: bool, svc_changed: bool):
    """Zaznacza, który backend (AdGuard / OMV) wymaga synchronizacji po zmianie profilu p."""
    if p["kind"] in db.SERVICE_KINDS:
        return changed, True
    return True, svc_changed


def _block_title(p: dict) -> str:
    return "Usługa wyłączona" if p["kind"] in db.SERVICE_KINDS else "Internet zablokowany"


def _active_title(p: dict) -> str:
    return "Usługa aktywna" if p["kind"] in db.SERVICE_KINDS else "Internet aktywny"


async def scheduler_tick(force_reconcile: bool = False):
    elapsed_min = TICK_SECONDS / 60.0
    changed = False          # zmiana dla urządzeń -> AdGuard
    svc_changed = False      # zmiana dla usług -> OMV

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
            changed, svc_changed = _mark(fresh, changed, svc_changed)
            await notify_ha(
                _block_title(fresh),
                f"{fresh['name']}: czas się skończył, dostęp zablokowany.",
            )
            continue

        # dzienny limit (jeśli ustawiony) się skończył -> blokujemy
        if fresh["daily_limit_min"] and fresh["used_today_min"] >= fresh["daily_limit_min"]:
            db.set_status(p["name"], "blocked", reset_session=True)
            db.add_event(p["name"], "daily_limit", f"dzienny limit {fresh['daily_limit_min']} min -> blokada")
            changed, svc_changed = _mark(fresh, changed, svc_changed)
            await notify_ha(
                _block_title(fresh),
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
                    _active_title(fresh),
                    f"{fresh['name']}: minęło {threshold} min od startu okresu.",
                )

    if changed:
        await sync_adguard_from_db("scheduler")
    elif force_reconcile:
        await sync_adguard_from_db("reconcile")

    # usługi: w tle; ponawiamy, jeśli poprzednia próba się nie udała
    if svc_changed:
        schedule_service_sync("scheduler")
    elif force_reconcile:
        schedule_service_sync("reconcile", force=True)
    elif _service_pending():
        schedule_service_sync("retry")


@app.on_event("startup")
async def on_startup():
    db.init_db()
    db.add_event("*", "service_start", "netguard wystartował")
    await sync_adguard_from_db("start")  # odbuduj stan AdGuard po restarcie kontenera
    schedule_service_sync("start", force=True)  # i usługi (np. MiniDLNA po restarcie OMV)
    start_scheduler()
    logger.info("netguard wystartował, tick=%ss", TICK_SECONDS)


# --------------------------------------------------------------------------
# REST API
# --------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "time": time.time()}


def _is_service(kind: str) -> bool:
    return kind in db.SERVICE_KINDS


async def apply_for(kind: str, reason: str):
    """Synchronizuje tylko ten backend, którego dotyczy zmiana profilu."""
    svc = _is_service(kind)
    await apply_changes(reason, devices=not svc, services=svc)


def _service_view(p: dict) -> dict | None:
    """Stan usługi widoczny w panelu: oczekiwany, zastosowany w OMV, ostatni błąd."""
    if not _is_service(p["kind"]):
        return None
    st = _service_state.get(p["name"]) or {}
    return {"desired": p["status"] == "allowed", "actual": st.get("actual"),
            "error": st.get("error") if st else None,
            "checked_at": st.get("ts") or None,
            "configured": omv.configured}


def _present(p: dict | None):
    if p is not None:
        p["service"] = _service_view(p)
    return p


@app.get("/profiles")
async def api_list_profiles():
    return [_present(p) for p in db.list_profiles()]


@app.post("/profiles")
async def api_upsert_profile(p: ProfileIn):
    name = p.name.strip()
    if not name:
        raise HTTPException(422, "Nazwa profilu nie może być pusta")
    existing = db.get_profile(name)
    kind = p.kind or (existing["kind"] if existing else "device")
    if kind not in db.KINDS:
        raise HTTPException(422, f"Nieznany rodzaj profilu: {kind} (dozwolone: {', '.join(db.KINDS)})")
    if existing and existing["kind"] != kind:
        raise HTTPException(409, f"Profil '{name}' ma rodzaj {existing['kind']} - usuń go i utwórz ponownie")
    if _is_service(kind):
        if p.ips or p.ip:
            raise HTTPException(422, "Profil usługi nie ma adresów IP")
        other = [x["name"] for x in db.list_profiles() if x["kind"] == kind and x["name"] != name]
        if other:
            raise HTTPException(409, f"Usługa {kind} jest już sterowana przez profil '{other[0]}'")
        ips: list[str] = []
    else:
        ips = _clean_ips(p)
    limit = max(0, p.daily_limit_min)
    try:
        db.upsert_profile(name, ips, limit, kind)
    except db.IpInUseError as exc:
        raise HTTPException(409, f"Adres {exc.ip} należy już do profilu '{exc.owner}'")
    db.add_event(name, "profile_saved", f"rodzaj={kind}; ips={ips}; dzienny_limit={limit}")
    # stan w AdGuard / OMV ma od razu odpowiadać temu, co pokazuje panel
    await apply_for(kind, "profile_saved")
    return _present(db.get_profile(name))


@app.delete("/profiles/{name}")
async def api_delete_profile(name: str):
    p = db.get_profile(name)
    if not p:
        raise HTTPException(404, "brak takiego profilu")
    db.delete_profile(name)
    db.add_event(name, "profile_deleted", "")
    await apply_for(p["kind"], "profile_deleted")
    return {"ok": True}


@app.post("/profiles/{name}/grant")
async def api_grant(name: str, body: GrantIn):
    p = db.get_profile(name)
    if not p:
        raise HTTPException(404, "brak takiego profilu")
    if not 1 <= body.minutes <= MAX_GRANT_MIN:
        raise HTTPException(400, f"minuty muszą być w zakresie 1-{MAX_GRANT_MIN}")
    db.grant_minutes(name, body.minutes)
    db.add_event(name, "grant", f"+{body.minutes} min -> dostęp przyznany")
    await apply_for(p["kind"], "grant")
    return _present(db.get_profile(name))


@app.post("/profiles/{name}/subtract")
async def api_subtract(name: str, body: GrantIn):
    """Odejmuje minuty od przyznanego czasu (korekta po dodaniu za dużo)."""
    p = db.get_profile(name)
    if not p:
        raise HTTPException(404, "brak takiego profilu")
    if not 1 <= body.minutes <= MAX_GRANT_MIN:
        raise HTTPException(400, f"minuty muszą być w zakresie 1-{MAX_GRANT_MIN}")
    if p["status"] != "allowed":
        raise HTTPException(409, "profil jest zablokowany - nie ma czasu do odjęcia")
    if p["remaining_grant_min"] > UNLIMITED_THRESHOLD_MIN:
        raise HTTPException(409, "dostęp bez limitu - użyj Blokuj")
    remaining = db.subtract_minutes(name, body.minutes)
    if remaining <= 0:
        db.set_status(name, "blocked", reset_session=True)
        db.add_event(name, "subtract", f"-{body.minutes} min -> czas wyczerpany, blokada")
        await apply_for(p["kind"], "subtract")
        await notify_ha(_block_title(p), f"{name}: czas wyczerpany po korekcie (-{body.minutes} min).")
    else:
        db.add_event(name, "subtract", f"-{body.minutes} min -> zostało {round(remaining)} min")
    return _present(db.get_profile(name))


@app.post("/profiles/{name}/block")
async def api_block(name: str):
    p = db.get_profile(name)
    if not p:
        raise HTTPException(404, "brak takiego profilu")
    db.set_status(name, "blocked", reset_session=True)
    db.add_event(name, "manual_block", "ręczna blokada z panelu/API")
    await apply_for(p["kind"], "manual_block")
    return _present(db.get_profile(name))


@app.post("/profiles/{name}/unblock")
async def api_unblock(name: str):
    """Odblokowanie bez określonego limitu czasu - dostęp 'do odwołania'."""
    p = db.get_profile(name)
    if not p:
        raise HTTPException(404, "brak takiego profilu")
    db.grant_minutes(name, 10**6)  # praktycznie "bez limitu"; użyj /block żeby zdjąć
    db.add_event(name, "unblock_unlimited", "odblokowanie bez limitu czasu")
    await apply_for(p["kind"], "unblock_unlimited")
    return _present(db.get_profile(name))


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
        "services": [],
    }
    for sp in _service_profiles():
        v = _service_view(sp)
        out["services"].append({
            "name": sp["name"], "kind": sp["kind"], "desired": v["desired"], "actual": v["actual"],
            "in_sync": v["actual"] == v["desired"] and not v["error"],
            "error": v["error"], "configured": v["configured"],
            "checked_age_s": round(time.time() - v["checked_at"]) if v["checked_at"] else None,
        })
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

    managed = {ip for p in db.list_profiles() for ip in p["ips"]}  # profile usług nie mają IP
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
