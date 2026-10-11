"""
Cienka, ale bezpieczna warstwa nad AdGuard Home REST API.

Ważne założenie projektowe: ten serwis JEST właścicielem sekcji
"disallowed_clients" w AdGuard. Nie edytuj tej listy ręcznie w UI
AdGuard, bo przy najbliższej synchronizacji netguard ją nadpisze
swoim stanem. Listę "allowed_clients" i "blocked_hosts" netguard
zostawia nietkniętą - odczytuje i odsyła bez zmian.

AdGuard Home nie ma endpointu "dodaj jeden adres" - trzeba wysłać
całą listę na raz, dlatego cała operacja jest chroniona asyncio.Lock,
żeby dwa równoległe requesty (np. dwa profile blokowane w tej samej
chwili) się nie nadpisały nawzajem.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger("netguard.adguard")


class AdGuardClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password)
        self.timeout = timeout
        self._lock = asyncio.Lock()

    async def _get(self, path: str):
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}{path}", auth=self.auth)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, payload: dict):
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base_url}{path}", auth=self.auth, json=payload)
            r.raise_for_status()

    async def get_access_list(self) -> dict:
        return await self._get("/control/access/list")

    async def get_querylog(self, limit: int = 500) -> dict:
        """Ostatnie wpisy dziennika zapytań (najnowsze pierwsze). Tylko odczyt."""
        return await self._get(f"/control/querylog?limit={int(limit)}&response_status=all")

    # ---- blokada regułą filtrującą (zalecana) -------------------------------
    # Zamiast ignorować zapytania (disallowed_clients), odpowiadamy od razu
    # odmową. Telewizory (np. Samsung) przy braku odpowiedzi przełączają się
    # na zapasowy DNS i omijają blokadę; przy szybkiej odmowie tego nie robią.
    RULE_MARK = "! netguard-managed (nie edytuj tej i następnej linii)"
    RULE_PREFIX = "/.*/$client="

    @classmethod
    def _build_rules(cls, user_rules: list[str], ips: set[str]) -> list[str]:
        """Usuwa dotychczasowe reguły netguarda i (jeśli są adresy) dopisuje aktualną."""
        out = []
        for line in user_rules:
            s = line.strip()
            if s == cls.RULE_MARK or s.startswith(cls.RULE_PREFIX):
                continue
            out.append(line)
        if ips:
            out += [cls.RULE_MARK, cls.RULE_PREFIX + "|".join(sorted(ips))]
        return out

    @classmethod
    def rule_ips(cls, user_rules: list[str]) -> set[str]:
        for line in user_rules:
            s = line.strip()
            if s.startswith(cls.RULE_PREFIX):
                return {x for x in s[len(cls.RULE_PREFIX):].split("|") if x}
        return set()

    async def get_user_rules(self) -> list[str]:
        status = await self._get("/control/filtering/status")
        return list(status.get("user_rules") or [])

    async def sync_block_rule(self, managed_ips: set[str], retries: int = 3) -> bool:
        """
        Ustawia regułę `/.*/$client=ip1|ip2` (odmowa dla wszystkich zapytań
        tych klientów). Inne własne reguły użytkownika zostają bez zmian.
        Zapisuje tylko przy zmianie, potem weryfikuje odczytem.
        """
        managed_ips = set(managed_ips)
        async with self._lock:
            last_err = None
            for attempt in range(1, retries + 1):
                try:
                    current = await self.get_user_rules()
                    if self.rule_ips(current) == managed_ips:
                        return True
                    await self._post("/control/filtering/set_rules",
                                     {"rules": self._build_rules(current, managed_ips)})
                    if self.rule_ips(await self.get_user_rules()) == managed_ips:
                        return True
                    last_err = "weryfikacja po zapisie reguły nie zgadza się"
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                logger.warning("Próba %s/%s zapisu reguły blokady nieudana: %s", attempt, retries, last_err)
                await asyncio.sleep(1.5 * attempt)
            logger.error("Nie udało się zsynchronizować reguły blokady z AdGuard: %s", last_err)
            return False

    async def sync_disallowed(self, managed_ips: set[str], retries: int = 3) -> bool:
        """
        Nadpisuje disallowed_clients tak, żeby zawierało dokładnie
        managed_ips, zachowując wszystko inne (allowed_clients,
        blocked_hosts) bez zmian. Zwraca True, jeśli się udało.
        """
        async with self._lock:
            last_err = None
            for attempt in range(1, retries + 1):
                try:
                    current = await self.get_access_list()
                    if set(current.get("disallowed_clients", [])) == set(managed_ips):
                        return True  # już zgodne - nie zapisujemy konfiguracji AdGuard bez potrzeby
                    payload = {
                        "allowed_clients": current.get("allowed_clients", []),
                        "disallowed_clients": sorted(managed_ips),
                        "blocked_hosts": current.get("blocked_hosts", []),
                    }
                    await self._post("/control/access/set", payload)

                    # weryfikacja odczytem - self-healing, upewniamy się, że zapis wszedł
                    verify = await self.get_access_list()
                    if set(verify.get("disallowed_clients", [])) == managed_ips:
                        return True
                    last_err = "weryfikacja po zapisie nie zgadza się z oczekiwanym stanem"
                except Exception as exc:  # noqa: BLE001 - chcemy złapać i zalogować, nie wywrócić serwisu
                    last_err = exc
                logger.warning("Próba %s/%s synchronizacji z AdGuard nieudana: %s", attempt, retries, last_err)
                await asyncio.sleep(1.5 * attempt)

            logger.error("Nie udało się zsynchronizować listy disallowed_clients z AdGuard: %s", last_err)
            return False
