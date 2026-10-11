"""
Warstwa trwałego stanu (SQLite). Wszystko co dotyczy profili
urządzeń żyje tutaj - żeby restart procesu/kontenera nie gubił
informacji o tym, kto jest zablokowany i ile czasu zużył.

Profil może mieć wiele adresów IP (np. telewizor: kabel LAN i WiFi).
Adresy trzymane są w osobnej tabeli profile_ips, a każdy adres może
należeć tylko do jednego profilu.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "netguard.db"
DB_PATH.parent.mkdir(exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY,
    daily_limit_min INTEGER NOT NULL DEFAULT 0,   -- 0 = brak dziennego limitu, steruje tylko "grant"
    used_today_min REAL NOT NULL DEFAULT 0,
    remaining_grant_min REAL NOT NULL DEFAULT 0,   -- ile minut "doładowanego" czasu zostało
    status TEXT NOT NULL DEFAULT 'blocked',        -- allowed | blocked
    session_started_at REAL,                       -- unix ts, gdy status='allowed'
    day_stamp TEXT,                                 -- YYYY-MM-DD, do resetu used_today_min o północy
    last_notified_min INTEGER NOT NULL DEFAULT 0,   -- ostatni próg (30/60/90...) o którym powiadomiono
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS profile_ips (
    ip TEXT PRIMARY KEY,                            -- jeden adres = jeden profil
    profile_name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profile_ips_name ON profile_ips(profile_name);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,                               -- unix ts zdarzenia
    profile TEXT NOT NULL,                          -- nazwa profilu albo '*' dla zdarzeń ogólnych
    event TEXT NOT NULL,                            -- np. grant, block, time_expired, adguard_sync
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

EVENTS_KEEP_DAYS = 90


class IpInUseError(Exception):
    """Adres IP jest już przypisany do innego profilu."""

    def __init__(self, ip: str, owner: str):
        super().__init__(f"{ip} należy już do profilu {owner}")
        self.ip = ip
        self.owner = owner


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")  # bezpieczniejsze przy równoległym dostępie
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_single_ip_schema(conn):
    """
    Stara wersja miała kolumnę profiles.ip (jeden adres na profil).
    Jeśli ją znajdziemy, przenosimy adresy do profile_ips i przebudowujemy
    tabelę profiles bez tej kolumny - wszystko w jednej transakcji.
    """
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(profiles)")]
    if "ip" not in cols:
        return
    try:
        conn.executescript(
            """
            BEGIN;
            ALTER TABLE profiles RENAME TO profiles_old;
            CREATE TABLE profiles (
                name TEXT PRIMARY KEY,
                daily_limit_min INTEGER NOT NULL DEFAULT 0,
                used_today_min REAL NOT NULL DEFAULT 0,
                remaining_grant_min REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'blocked',
                session_started_at REAL,
                day_stamp TEXT,
                last_notified_min INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS profile_ips (
                ip TEXT PRIMARY KEY,
                profile_name TEXT NOT NULL
            );
            INSERT INTO profiles
                (name, daily_limit_min, used_today_min, remaining_grant_min,
                 status, session_started_at, day_stamp, last_notified_min, updated_at)
            SELECT name, daily_limit_min, used_today_min, remaining_grant_min,
                   status, session_started_at, day_stamp, last_notified_min, updated_at
            FROM profiles_old;
            INSERT OR IGNORE INTO profile_ips (ip, profile_name)
            SELECT ip, name FROM profiles_old;
            DROP TABLE profiles_old;
            COMMIT;
            """
        )
    except Exception:
        conn.rollback()
        raise


def init_db():
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='profiles'"
        ).fetchone()
        if exists:
            _migrate_single_ip_schema(conn)
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - EVENTS_KEEP_DAYS * 86400,))


def today_stamp() -> str:
    return time.strftime("%Y-%m-%d")


def _with_ips(conn, rows):
    ips_by_name: dict[str, list[str]] = {}
    for r in conn.execute("SELECT profile_name, ip FROM profile_ips ORDER BY ip"):
        ips_by_name.setdefault(r["profile_name"], []).append(r["ip"])
    result = []
    for r in rows:
        d = dict(r)
        d["ips"] = ips_by_name.get(d["name"], [])
        result.append(d)
    return result


def upsert_profile(name: str, ips: list[str], daily_limit_min: int):
    """
    Tworzy profil albo aktualizuje istniejący (limit dzienny i CAŁĄ listę adresów).
    Stan czasu (status, pozostały czas) istniejącego profilu zostaje bez zmian.
    Rzuca IpInUseError, jeśli któryś adres należy do innego profilu
    (wtedy cała operacja jest wycofana).
    """
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO profiles (name, daily_limit_min, day_stamp, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                daily_limit_min=excluded.daily_limit_min,
                updated_at=excluded.updated_at
            """,
            (name, daily_limit_min, today_stamp(), now),
        )
        conn.execute("DELETE FROM profile_ips WHERE profile_name=?", (name,))
        for ip in ips:
            try:
                conn.execute(
                    "INSERT INTO profile_ips (ip, profile_name) VALUES (?, ?)", (ip, name)
                )
            except sqlite3.IntegrityError:
                owner = conn.execute(
                    "SELECT profile_name FROM profile_ips WHERE ip=?", (ip,)
                ).fetchone()
                raise IpInUseError(ip, owner["profile_name"] if owner else "?")


def delete_profile(name: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM profile_ips WHERE profile_name=?", (name,))
        conn.execute("DELETE FROM profiles WHERE name=?", (name,))


def list_profiles():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
        return _with_ips(conn, rows)


def get_profile(name: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        return _with_ips(conn, [row])[0]


def blocked_ips() -> set[str]:
    """Wszystkie adresy IP profili, które mają teraz być zablokowane."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.ip FROM profile_ips i
            JOIN profiles p ON p.name = i.profile_name
            WHERE p.status = 'blocked'
            """
        )
        return {r["ip"] for r in rows}


def grant_minutes(name: str, minutes: int):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE profiles
            SET remaining_grant_min = remaining_grant_min + ?,
                status = 'allowed',
                session_started_at = COALESCE(session_started_at, ?),
                last_notified_min = 0,
                updated_at = ?
            WHERE name = ?
            """,
            (minutes, time.time(), time.time(), name),
        )


def set_status(name: str, status: str, reset_session: bool = False):
    with get_conn() as conn:
        if reset_session:
            conn.execute(
                "UPDATE profiles SET status=?, session_started_at=NULL, "
                "remaining_grant_min=0, last_notified_min=0, updated_at=? WHERE name=?",
                (status, time.time(), name),
            )
        else:
            conn.execute(
                "UPDATE profiles SET status=?, updated_at=? WHERE name=?",
                (status, time.time(), name),
            )


def apply_tick(name: str, elapsed_min: float):
    """Woła scheduler co interwał: zużywa remaining_grant_min, aktualizuje used_today_min."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE profiles
            SET remaining_grant_min = MAX(0, remaining_grant_min - ?),
                used_today_min = used_today_min + ?,
                updated_at = ?
            WHERE name = ? AND status = 'allowed'
            """,
            (elapsed_min, elapsed_min, time.time(), name),
        )


def reset_daily_if_needed(name: str):
    stamp = today_stamp()
    with get_conn() as conn:
        conn.execute(
            "UPDATE profiles SET used_today_min=0, day_stamp=? WHERE name=? AND day_stamp<>?",
            (stamp, name, stamp),
        )


def add_event(profile: str, event: str, detail: str = ""):
    """Dopisuje wpis do historii (kto/kiedy/dlaczego zmienił stan)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO events (ts, profile, event, detail) VALUES (?, ?, ?, ?)",
            (time.time(), profile, event, detail[:500]),
        )


def list_events(limit: int = 100, profile: str | None = None):
    """Najnowsze zdarzenia (od najnowszego)."""
    limit = max(1, min(int(limit), 1000))
    with get_conn() as conn:
        if profile:
            rows = conn.execute(
                "SELECT ts, profile, event, detail FROM events WHERE profile=? "
                "ORDER BY id DESC LIMIT ?", (profile, limit))
        else:
            rows = conn.execute(
                "SELECT ts, profile, event, detail FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def set_last_notified(name: str, threshold: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE profiles SET last_notified_min=?, updated_at=? WHERE name=?",
            (threshold, time.time(), name),
        )
