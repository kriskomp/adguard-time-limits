"""
Klient OpenMediaVault: włącza/wyłącza MiniDLNA przez SSH z kluczem ograniczonym
do jednego polecenia (forced command w authorized_keys -> scripts/omv/dlna.sh).

Po stronie OMV skrypt zmienia ustawienie "Włączone" przez omv-rpc (API OMV) i wdraża
zmianę modułu minidlna, więc panel OMV zawsze pokazuje prawdziwy stan i nie ma
"oczekujących zmian". Przez SSH przechodzą tylko trzy słowa: status | start | stop.
"""
import asyncio
import re

_STATE_RE = re.compile(r"enabled=(true|false)\s+active=(active|inactive)")


class OmvError(Exception):
    pass


class OmvClient:
    def __init__(self, host: str, user: str = "root", key: str = "", port: int = 22,
                 known_hosts: str = "", timeout: float = 150.0):
        self.host, self.user, self.key, self.port = host, user, key, port
        self.known_hosts, self.timeout = known_hosts, timeout

    @property
    def configured(self) -> bool:
        return bool(self.host and self.key)

    def _argv(self, word: str) -> list[str]:
        argv = ["ssh", "-i", self.key, "-p", str(self.port),
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=accept-new", "-o", "IdentitiesOnly=yes"]
        if self.known_hosts:
            argv += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        return argv + [f"{self.user}@{self.host}", word]

    async def _run(self, word: str) -> tuple[int, str, str]:
        if not self.configured:
            raise OmvError("OMV nie jest skonfigurowany (OMV_HOST / OMV_SSH_KEY)")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(word), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            raise OmvError("brak polecenia ssh na serwerze netguard (apt install openssh-client)")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise OmvError(f"przekroczono czas {self.timeout:.0f} s ({word})")
        return proc.returncode, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()

    async def _state(self, word: str) -> dict:
        rc, out, err = await self._run(word)
        m = _STATE_RE.search(out)
        if rc != 0 or not m:
            raise OmvError(f"{word}: kod={rc} {(err or out)[:200]}")
        return {"enabled": m.group(1) == "true", "active": m.group(2) == "active"}

    async def status(self) -> dict:
        return await self._state("status")

    async def set_enabled(self, enabled: bool) -> dict:
        """Idempotentne: jeśli stan już zgodny, skrypt niczego nie wdraża."""
        return await self._state("start" if enabled else "stop")
