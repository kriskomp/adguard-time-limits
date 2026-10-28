"""
Tests for adguard-time-limits (a.k.a. netguard): db.py naprawdę (stdlib), logika app.py na atrapach fastapi/pydantic/httpx.
Run from the repo root: python3 tests/test_netguard.py   (no dependencies needed; fastapi/httpx are stubbed)
"""
import asyncio
import copy
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

PROJ = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
os.environ["TICK_SECONDS"] = "60"  # jeden tick = 1 minuta, żeby test był szybki
os.environ.pop("HA_URL", None)
os.environ.pop("HA_TOKEN", None)


# ---------------------------------------------------------------- atrapy bibliotek
class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FastAPI:
    def __init__(self, *a, **k):
        pass

    def _deco(self, *a, **k):
        return lambda fn: fn

    get = post = delete = on_event = _deco

    def mount(self, *a, **k):
        pass


class BaseModel:
    def __init__(self, **kw):
        names = {}
        for klass in reversed(type(self).__mro__):
            names.update(getattr(klass, "__annotations__", {}))
        for name in names:
            if name in kw:
                setattr(self, name, kw[name])
            elif hasattr(type(self), name):
                setattr(self, name, copy.copy(getattr(type(self), name)))
            else:
                raise TypeError(f"brak pola {name}")


def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


mod("fastapi", FastAPI=FastAPI, HTTPException=HTTPException)
mod("fastapi.responses", FileResponse=lambda *a, **k: None)
mod("fastapi.staticfiles", StaticFiles=lambda *a, **k: None)
mod("pydantic", BaseModel=BaseModel)
mod("httpx", AsyncClient=object)

import db  # noqa: E402
import app  # noqa: E402

passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError("NIE PRZESZŁO: " + label)
    passed += 1
    print("  ok  -", label)


def fresh_db():
    d = tempfile.mkdtemp()
    db.DB_PATH = Path(d) / "t.db"
    return db.DB_PATH


TV = ["192.168.8.207", "192.168.8.208"]

# ---------------------------------------------------------------- db: nowa baza
print("T1 nowa baza, profil z dwoma IP")
fresh_db()
db.init_db()
db.upsert_profile("telewizor", TV, 0)
p = db.get_profile("telewizor")
check(p["ips"] == TV, "profil ma oba adresy")
check(p["status"] == "blocked", "nowy profil jest zablokowany")
check(db.blocked_ips() == set(TV), "blocked_ips zwraca oba adresy")
db.init_db()
check(db.get_profile("telewizor")["ips"] == TV, "powtórne init_db niczego nie psuje")

# ---------------------------------------------------------------- db: migracja
print("T2 migracja starego schematu (kolumna ip)")
path = fresh_db()
conn = sqlite3.connect(path)
conn.executescript(
    """
    CREATE TABLE profiles (
        name TEXT PRIMARY KEY, ip TEXT NOT NULL UNIQUE,
        daily_limit_min INTEGER NOT NULL DEFAULT 0,
        used_today_min REAL NOT NULL DEFAULT 0,
        remaining_grant_min REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'blocked',
        session_started_at REAL, day_stamp TEXT,
        last_notified_min INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0);
    INSERT INTO profiles (name, ip, daily_limit_min, used_today_min, remaining_grant_min, status, session_started_at, day_stamp, last_notified_min, updated_at)
      VALUES ('telewizor', '192.168.8.207', 120, 7.5, 12.5, 'allowed', 1000.0, '2026-10-08', 30, 5.0);
    INSERT INTO profiles (name, ip) VALUES ('tablet', '192.168.8.50');
    """
)
conn.commit()
conn.close()
db.init_db()
p = db.get_profile("telewizor")
check(p["ips"] == ["192.168.8.207"], "adres przeniesiony do profile_ips")
check(p["status"] == "allowed" and p["remaining_grant_min"] == 12.5 and p["used_today_min"] == 7.5, "stan czasu zachowany")
check(p["daily_limit_min"] == 120 and p["last_notified_min"] == 30 and p["session_started_at"] == 1000.0, "limit i sesja zachowane")
check(db.get_profile("tablet")["ips"] == ["192.168.8.50"], "drugi profil też zmigrowany")
with db.get_conn() as c:
    cols = [r["name"] for r in c.execute("PRAGMA table_info(profiles)")]
    left = c.execute("SELECT name FROM sqlite_master WHERE name='profiles_old'").fetchone()
check("ip" not in cols and left is None, "kolumna ip zniknęła, brak tabeli tymczasowej")
db.upsert_profile("telewizor", TV, 120)
check(db.get_profile("telewizor")["ips"] == TV and db.get_profile("telewizor")["remaining_grant_min"] == 12.5,
      "po migracji można dodać drugi adres, stan czasu bez zmian")

# ---------------------------------------------------------------- db: konflikt
print("T3 konflikt adresu między profilami")
fresh_db()
db.init_db()
db.upsert_profile("A", ["10.0.0.1"], 0)
try:
    db.upsert_profile("B", ["10.0.0.2", "10.0.0.1"], 0)
    raised = None
except db.IpInUseError as e:
    raised = e
check(raised is not None and raised.owner == "A" and raised.ip == "10.0.0.1", "IpInUseError wskazuje właściciela")
check(db.get_profile("B") is None, "nieudany zapis wycofany (profil B nie powstał)")
check(db.get_profile("A")["ips"] == ["10.0.0.1"], "profil A bez zmian")
check(db.blocked_ips() == {"10.0.0.1"}, "10.0.0.2 nie zostało po nieudanym zapisie")

# ---------------------------------------------------------------- db: edycja i usuwanie
print("T4/T5 edycja listy adresów i usuwanie")
fresh_db()
db.init_db()
db.upsert_profile("telewizor", TV, 0)
db.grant_minutes("telewizor", 30)
db.upsert_profile("telewizor", ["192.168.8.207"], 90)
p = db.get_profile("telewizor")
check(p["ips"] == ["192.168.8.207"], "edycja zastępuje listę adresów")
check(p["status"] == "allowed" and p["remaining_grant_min"] == 30 and p["daily_limit_min"] == 90,
      "edycja nie rusza statusu i pozostałego czasu, zmienia limit dzienny")
check(db.blocked_ips() == set(), "profil z czasem nie jest na liście blokad")
db.delete_profile("telewizor")
check(db.get_profile("telewizor") is None and db.blocked_ips() == set(), "usunięcie czyści profil i adresy")
with db.get_conn() as c:
    check(c.execute("SELECT COUNT(*) AS n FROM profile_ips").fetchone()["n"] == 0, "brak osieroconych adresów")

# ---------------------------------------------------------------- app: logika
print("T6 API (na atrapach): walidacja, synchronizacja z AdGuard")
fresh_db()
db.init_db()
calls = []


async def fake_sync(ips, retries=3):
    calls.append(set(ips))
    return True


app.adguard.sync_block_rule = fake_sync


async def fake_noop(ips, retries=3):
    return True


app.adguard.sync_disallowed = fake_noop


def run(coro):
    return asyncio.run(coro)


res = run(app.api_upsert_profile(app.ProfileIn(name="  telewizor ", ips=["192.168.8.207, 192.168.8.208"], daily_limit_min=0)))
check(res["name"] == "telewizor" and res["ips"] == TV, "nazwa przycięta, adresy rozdzielone po przecinku")
check(calls[-1] == set(TV), "nowy profil od razu zablokowany w AdGuard (oba adresy)")

run(app.api_grant("telewizor", app.GrantIn(minutes=30)))
check(calls[-1] == set(), "po dodaniu czasu oba adresy zdjęte z blokady")
run(app.api_block("telewizor"))
check(calls[-1] == set(TV), "ręczna blokada blokuje oba adresy")

res = run(app.api_upsert_profile(app.ProfileIn(name="stary", ip="192.168.8.99")))
check(res["ips"] == ["192.168.8.99"], "stare pole ip nadal działa")


def expect(status, coro, label):
    try:
        run(coro)
    except HTTPException as e:
        check(e.status_code == status, label)
        return
    raise AssertionError("NIE PRZESZŁO (brak wyjątku): " + label)


expect(422, app.api_upsert_profile(app.ProfileIn(name="x", ips=["192.168.8.300"])), "nieprawidłowy adres -> 422")
expect(422, app.api_upsert_profile(app.ProfileIn(name="x", ips=[])), "brak adresów -> 422")
expect(422, app.api_upsert_profile(app.ProfileIn(name="   ", ips=["10.1.1.1"])), "pusta nazwa -> 422")
expect(409, app.api_upsert_profile(app.ProfileIn(name="inny", ips=["192.168.8.207"])), "adres z innego profilu -> 409")
res = run(app.api_upsert_profile(app.ProfileIn(name="v6", ips=["fe80::1", "fe80::1"])))
check(res["ips"] == ["fe80::1"], "duplikaty usuwane, IPv6 akceptowane")

print("T7 harmonogram: po wyczerpaniu czasu blokuje wszystkie adresy profilu")
fresh_db()
db.init_db()
calls.clear()
run(app.api_upsert_profile(app.ProfileIn(name="telewizor", ips=TV)))
run(app.api_grant("telewizor", app.GrantIn(minutes=1)))
check(db.get_profile("telewizor")["status"] == "allowed", "profil z czasem jest dozwolony")
run(app.scheduler_tick())
p = db.get_profile("telewizor")
check(p["status"] == "blocked" and p["remaining_grant_min"] == 0, "po ticku czas = 0, status blocked")
check(calls[-1] == set(TV), "AdGuard dostał oba adresy do zablokowania")

print("T8 historia zdarzeń i diagnostyka")
fresh_db()
db.init_db()
calls.clear()
run(app.api_upsert_profile(app.ProfileIn(name="telewizor", ips=TV)))
run(app.api_grant("telewizor", app.GrantIn(minutes=1)))
run(app.scheduler_tick())
run(app.api_grant("telewizor", app.GrantIn(minutes=30)))
run(app.api_block("telewizor"))
ev = run(app.api_events(50, None))
names = [e["event"] for e in ev]
check(names[0] == "adguard_sync_ok" and "manual_block" in names and "time_expired" in names and "grant" in names
      and "profile_saved" in names, "historia zawiera zapis profilu, przyznanie czasu, wygaśnięcie i ręczną blokadę")
check(ev[0]["ts"] >= ev[-1]["ts"], "zdarzenia posortowane od najnowszego")
check(run(app.api_events(1, None))[0]["event"] == "adguard_sync_ok", "limit zdarzeń działa")
check(len(run(app.api_events(50, "telewizor"))) >= 4, "filtr po profilu działa")

class FakeAG:
    def __init__(self, disallowed, log, fail=False):
        self.disallowed, self.log, self.fail = disallowed, log, fail

    async def get_access_list(self):
        if self.fail:
            raise RuntimeError("połączenie odrzucone")
        return {"allowed_clients": [], "disallowed_clients": self.disallowed, "blocked_hosts": []}

    rule_ips = staticmethod(app.AdGuardClient.rule_ips)

    async def get_user_rules(self):
        if self.fail:
            raise RuntimeError("połączenie odrzucone")
        return (["/.*/$client=" + "|".join(self.disallowed)] if self.disallowed else [])

    async def get_querylog(self, limit=500):
        return {"data": self.log}

real_ag = app.adguard
log = [
    {"client": "192.168.8.208", "time": "2026-10-09T21:17:21+02:00", "question": {"name": "nrdp.logs.netflix.com"}, "reason": "FilteredBlackList"},
    {"client": "192.168.8.208", "time": "2026-10-09T21:00:00+02:00", "question": {"name": "old.example"}, "reason": "NotFilteredNotFound"},
    {"client": "192.168.8.99", "time": "2026-10-09T21:10:00+02:00", "question": {"name": "x"}, "reason": "NotFilteredNotFound"},
]
app.adguard = FakeAG(sorted(TV), log)
d = run(app.api_diagnostics())
check(d["in_sync"] is True and d["db_blocked"] == sorted(TV), "diagnostyka: AdGuard zgodny z panelem")
check(d["last_queries"]["192.168.8.208"]["domain"] == "nrdp.logs.netflix.com", "ostatnie zapytanie = najnowszy wpis klienta")
check("192.168.8.99" not in d["last_queries"], "obce adresy pomijane")
app.adguard = FakeAG([], log)
d = run(app.api_diagnostics())
check(d["in_sync"] is False and d["adguard_disallowed"] == [], "diagnostyka wykrywa rozjazd (AdGuard pusty, panel blokuje)")
app.adguard = FakeAG([], log, fail=True)
d = run(app.api_diagnostics())
check(d["adguard_error"] and d["in_sync"] is None, "diagnostyka zgłasza błąd połączenia z AdGuard")
app.adguard = real_ag

print("T9 heartbeat i restart schedulera")
fresh_db()
db.init_db()


async def scheduler_scenario():
    app.TICK_SECONDS = 0.01
    app.adguard = FakeAG([], [])
    app.start_scheduler()
    await asyncio.sleep(0.15)
    d = await app.api_diagnostics()
    hb_ok = d["scheduler_last_tick_age_s"] is not None and d["scheduler_last_tick_age_s"] <= 1
    first = app._scheduler_task
    first.cancel()  # symulacja: pętla znika -> cancel NIE restartuje (to świadome zamknięcie)
    await asyncio.sleep(0.05)
    return hb_ok, first


hb_ok, first = asyncio.run(scheduler_scenario())
check(hb_ok, "heartbeat: diagnostyka pokazuje świeży obieg schedulera")
check(first is not None, "referencja do zadania schedulera jest trzymana")


async def restart_scenario():
    calls_n = {"n": 0}
    real_loop = app.scheduler_loop

    async def flaky():
        calls_n["n"] += 1
        if calls_n["n"] == 1:
            raise RuntimeError("awaria testowa")
        await real_loop()

    app.scheduler_loop = flaky
    try:
        app.start_scheduler()
        await asyncio.sleep(0.2)
    finally:
        app.scheduler_loop = real_loop
    task = app._scheduler_task
    task.cancel()
    return calls_n["n"]


check(asyncio.run(restart_scenario()) >= 2, "scheduler restartuje się po awarii pętli")
ev = db.list_events(20)
check(any(e["event"] == "scheduler_restart" for e in ev), "restart schedulera zapisany w historii")

print("T10 klient AdGuard: zapis tylko przy zmianie, reszta list nietknięta")
import adguard_client as agc


class RecordingClient(agc.AdGuardClient):
    def __init__(self, state):
        super().__init__("http://x", "u", "p")
        self.state, self.posts = state, []

    async def _get(self, path):
        return dict(self.state)

    async def _post(self, path, payload):
        self.posts.append((path, payload))
        self.state = dict(payload)


rc = RecordingClient({"allowed_clients": [], "disallowed_clients": ["192.168.8.207"], "blocked_hosts": ["version.bind"]})
ok = asyncio.run(rc.sync_disallowed({"192.168.8.207"}))
check(ok and rc.posts == [], "lista zgodna -> brak zapisu do AdGuard")
ok = asyncio.run(rc.sync_disallowed({"192.168.8.207", "192.168.8.208"}))
check(ok and len(rc.posts) == 1 and rc.posts[0][0] == "/control/access/set", "zmiana listy -> dokładnie jeden zapis")
check(rc.posts[0][1]["blocked_hosts"] == ["version.bind"] and rc.posts[0][1]["allowed_clients"] == [],
      "blocked_hosts i allowed_clients przesłane bez zmian")
ok = asyncio.run(rc.sync_disallowed(set()))
check(ok and rc.state["disallowed_clients"] == [], "odblokowanie czyści tylko disallowed_clients")

print("T11 odejmowanie minut (korekta) i walidacja zakresu")
app.adguard = real_ag  # atrapa z wcześniejszych testów podmieniona na klienta ze stubem zapisu
fresh_db()
db.init_db()
calls.clear()
run(app.api_upsert_profile(app.ProfileIn(name="telewizor", ips=TV)))
expect(409, app.api_subtract("telewizor", app.GrantIn(minutes=30)), "odejmowanie od zablokowanego profilu -> 409")
expect(404, app.api_subtract("nie-ma", app.GrantIn(minutes=30)), "nieistniejący profil -> 404")
run(app.api_grant("telewizor", app.GrantIn(minutes=60)))
res = run(app.api_subtract("telewizor", app.GrantIn(minutes=30)))
check(res["status"] == "allowed" and abs(res["remaining_grant_min"] - 30) < 1e-6, "60 - 30 = 30 min, nadal dozwolony")
check(res["used_today_min"] == 0, "odjęcie nie zmienia czasu zużytego dzisiaj")
check(db.list_events(1)[0]["event"] == "subtract", "zdarzenie 'subtract' w historii")
app.TICK_SECONDS = 60.0  # jeden tick = 1 minuta (T9 zmieniło wartość)
run(app.scheduler_tick())
res = run(app.api_subtract("telewizor", app.GrantIn(minutes=10)))
check(abs(res["remaining_grant_min"] - 19) < 1e-6, "po ticku zostało 29, minus 10 = 19")
calls.clear()
res = run(app.api_subtract("telewizor", app.GrantIn(minutes=500)))
check(res["status"] == "blocked" and res["remaining_grant_min"] == 0, "odjęcie więcej niż zostało -> 0 i blokada")
check(calls and calls[-1] == set(TV), "AdGuard dostał adresy do zablokowania od razu")
for bad in (0, -5, 1441):
    expect(400, app.api_subtract("telewizor", app.GrantIn(minutes=bad)), f"subtract minutes={bad} -> 400")
    expect(400, app.api_grant("telewizor", app.GrantIn(minutes=bad)), f"grant minutes={bad} -> 400")
run(app.api_unblock("telewizor"))
expect(409, app.api_subtract("telewizor", app.GrantIn(minutes=30)), "tryb bez limitu -> 409 (użyj Blokuj)")

print("T12 migracja kolumny kind (baza sprzed profili usług)")
path = fresh_db()
conn = sqlite3.connect(path)
conn.executescript(
    """
    CREATE TABLE profiles (
        name TEXT PRIMARY KEY, daily_limit_min INTEGER NOT NULL DEFAULT 0,
        used_today_min REAL NOT NULL DEFAULT 0, remaining_grant_min REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'blocked', session_started_at REAL, day_stamp TEXT,
        last_notified_min INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0);
    CREATE TABLE profile_ips (ip TEXT PRIMARY KEY, profile_name TEXT NOT NULL);
    INSERT INTO profiles (name, remaining_grant_min, status) VALUES ('telewizor', 20, 'allowed');
    INSERT INTO profile_ips VALUES ('192.168.8.207', 'telewizor');
    """
)
conn.commit()
conn.close()
db.init_db()
p = db.get_profile("telewizor")
check(p["kind"] == "device" and p["remaining_grant_min"] == 20 and p["status"] == "allowed",
      "istniejący profil dostał kind=device, stan czasu nietknięty")
db.init_db()
check(db.get_profile("telewizor")["kind"] == "device", "powtórne init_db nic nie psuje")

print("T13 profil usługi MiniDLNA (OMV) na atrapie klienta")


class FakeOmv:
    configured = True

    def __init__(self):
        self.enabled = None
        self.calls = []
        self.fail = None

    async def set_enabled(self, enabled):
        self.calls.append(enabled)
        if self.fail:
            raise app.OmvError(self.fail)
        self.enabled = enabled
        return {"enabled": enabled, "active": enabled}


def reset_services():
    app._service_state.clear()
    app._service_task = None
    app._service_rerun = None


async def service_scenario():
    fresh_db()
    db.init_db()
    reset_services()
    fo = FakeOmv()
    app.omv = fo
    calls.clear()
    app.TICK_SECONDS = 60.0

    async def settle():
        await app.flush_services()

    async def expect_async(status, coro, label):
        try:
            await coro
        except HTTPException as e:
            check(e.status_code == status, label)
            return
        raise AssertionError("NIE PRZESZŁO (brak wyjątku): " + label)

    res = await app.api_upsert_profile(app.ProfileIn(name="dlna", kind="minidlna"))
    await settle()
    check(res["kind"] == "minidlna" and res["ips"] == [] and res["status"] == "blocked", "profil usługi: bez IP, domyślnie zablokowany")
    check(fo.calls == [False] and calls == [], "utworzenie wyłącza usługę w OMV i nie rusza AdGuard")
    check(db.blocked_ips() == set(), "profil usługi nie trafia na listę blokad DNS")
    await expect_async(409, app.api_upsert_profile(app.ProfileIn(name="dlna2", kind="minidlna")), "drugi profil MiniDLNA -> 409")
    await expect_async(422, app.api_upsert_profile(app.ProfileIn(name="x", kind="minidlna", ips=["10.0.0.5"])), "profil usługi z IP -> 422")
    await expect_async(422, app.api_upsert_profile(app.ProfileIn(name="x", kind="coś")), "nieznany rodzaj -> 422")
    await expect_async(409, app.api_upsert_profile(app.ProfileIn(name="dlna", kind="device", ips=["10.0.0.5"])), "zmiana rodzaju istniejącego profilu -> 409")
    res = await app.api_upsert_profile(app.ProfileIn(name="dlna", daily_limit_min=90))
    check(res["kind"] == "minidlna" and res["daily_limit_min"] == 90, "edycja bez podania rodzaju zachowuje rodzaj")
    await settle()

    fo.calls.clear()
    res = await app.api_grant("dlna", app.GrantIn(minutes=30))
    await settle()
    check(fo.calls == [True] and fo.enabled is True, "grant włącza MiniDLNA")
    check(calls == [], "grant usługi nie dotyka AdGuard")
    check(res["service"]["desired"] is True, "odpowiedź API niesie stan usługi")
    await app.api_grant("dlna", app.GrantIn(minutes=15))
    await settle()
    check(fo.calls == [True], "kolejny grant nie wdraża zmian ponownie (stan już zgodny)")

    await app.api_upsert_profile(app.ProfileIn(name="tv", ips=["10.0.0.9"]))
    await app.api_grant("tv", app.GrantIn(minutes=10))
    await settle()
    check(fo.calls == [True], "operacje na urządzeniu nie wywołują OMV")

    await app.api_subtract("dlna", app.GrantIn(minutes=15))
    await settle()
    check(fo.enabled is True, "odjęcie części czasu nie wyłącza usługi")
    await app.api_subtract("dlna", app.GrantIn(minutes=500))
    await settle()
    check(fo.enabled is False and db.get_profile("dlna")["status"] == "blocked", "odjęcie całego czasu wyłącza usługę")

    await app.api_grant("dlna", app.GrantIn(minutes=1))
    await settle()
    check(fo.enabled is True, "ponowny grant włącza usługę")
    await app.scheduler_tick()      # 1 minuta -> czas się kończy
    await settle()
    check(db.get_profile("dlna")["status"] == "blocked" and fo.enabled is False, "koniec czasu w schedulerze wyłącza usługę")
    ev = [e["event"] for e in db.list_events(30, "dlna")]
    check("time_expired" in ev and "service_off" in ev and "service_on" in ev, "historia: time_expired, service_on, service_off")

    # awaria OMV: błąd widoczny raz w historii, ponawianie w kolejnych tickach, powrót do zgodności
    fo.fail = "ssh: connection refused"
    await app.api_grant("dlna", app.GrantIn(minutes=30))
    await settle()
    check(fo.enabled is False and app._service_state["dlna"]["error"], "OMV niedostępny: błąd zapisany, usługa nie zmieniona")
    for _ in range(3):
        await app.scheduler_tick()
        await settle()
    fails = [e for e in db.list_events(50, "dlna") if e["event"] == "service_FAIL"]
    check(len(fails) == 1, "powtarzający się ten sam błąd zapisany w historii tylko raz")
    d = await app.api_diagnostics()
    check(d["services"][0]["in_sync"] is False and d["services"][0]["error"], "diagnostyka pokazuje rozjazd usługi")
    fo.fail = None
    await app.scheduler_tick()
    await settle()
    check(fo.enabled is True and not app._service_state["dlna"]["error"], "po powrocie OMV scheduler sam doprowadza usługę do stanu")
    d = await app.api_diagnostics()
    check(d["services"][0]["in_sync"] is True and d["services"][0]["actual"] is True, "diagnostyka: usługa zgodna")

    # rozjazd po stronie OMV (ktoś wyłączył ręcznie) naprawia okresowy reconcile
    fo.enabled = False
    fo.calls.clear()
    await app.scheduler_tick(force_reconcile=True)
    await settle()
    check(fo.enabled is True and fo.calls == [True], "reconcile przywraca stan usługi")

    # usunięcie profilu wyłącza usługę
    await app.api_delete_profile("dlna")
    await settle()
    check(fo.enabled is False and "dlna" not in app._service_state, "usunięcie profilu wyłącza usługę i czyści stan")

    # restart netguard: stan z bazy wymusza wyłączenie usługi (OMV mógł wstać z włączoną)
    await app.api_upsert_profile(app.ProfileIn(name="dlna", kind="minidlna"))
    await settle()
    reset_services()
    fo.enabled = True
    app.schedule_service_sync("start", force=True)
    await settle()
    check(fo.enabled is False, "po restarcie zablokowany profil => MiniDLNA wyłączone")

    # nieskonfigurowany OMV: czytelny błąd zamiast wyjątku
    import omv_client
    app.omv = omv_client.OmvClient("", "root", "")
    reset_services()
    app.schedule_service_sync("test")
    await settle()
    check("nie jest skonfigurowany" in (app._service_state["dlna"]["error"] or ""), "brak konfiguracji OMV -> komunikat o błędzie")


asyncio.run(service_scenario())

print("T14 klient OMV: parsowanie odpowiedzi skryptu i komendy SSH")
import omv_client


class StubOmv(omv_client.OmvClient):
    def __init__(self, rc, out, err=""):
        super().__init__("192.168.8.8", "root", "/etc/netguard/omv_key", known_hosts="/opt/netguard/data/known_hosts")
        self.res, self.words = (rc, out, err), []

    async def _run(self, word):
        self.words.append(word)
        return self.res


c = StubOmv(0, "enabled=true active=active")
st = asyncio.run(c.set_enabled(True))
check(st == {"enabled": True, "active": True} and c.words == ["start"], "set_enabled(True) -> komenda start")
c = StubOmv(0, "enabled=false active=inactive")
check(asyncio.run(c.set_enabled(False)) == {"enabled": False, "active": False} and c.words == ["stop"], "set_enabled(False) -> komenda stop")
check(asyncio.run(c.status())["enabled"] is False and c.words[-1] == "status", "status -> komenda status")
for rc, out in ((3, "enabled=true active=inactive"), (0, "śmieci"), (255, "")):
    try:
        asyncio.run(StubOmv(rc, out, "boom").status())
        raised = False
    except omv_client.OmvError:
        raised = True
    check(raised, f"kod={rc} / niepoprawny wynik -> OmvError")
argv = omv_client.OmvClient("h", "root", "/k", 2222, "/kh")._argv("stop")
check(argv[0] == "ssh" and argv[-1] == "stop" and "root@h" in argv and "BatchMode=yes" in argv and "2222" in argv
      and "UserKnownHostsFile=/kh" in argv, "wiersz poleceń ssh: klucz, port, BatchMode, known_hosts, tylko słowo-komenda")

print(f"\nWSZYSTKIE TESTY PRZESZŁY ({passed} sprawdzeń)")
