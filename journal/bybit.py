"""Клиент Bybit v5 — только чтение, только stdlib.

Никакого CCXT: нужны сырые поля (closedSize, positionIdx, execType=Funding),
которые unified-слой прячет, а зависимость тянула бы сотни бирж ради одной.
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import keychain

BASE_URL = "https://api.bybit.com"

# По документации Bybit (/v5/user/query-api):
#   readOnly: 0 = Read and Write, 1 = Read only  <- настоящий гейт
#   permissions: ОБЛАСТИ доступа (к каким разделам ключ обращается), НЕ права записи.
# Поэтому "ContractTrade": ["Order", "Position"] у ключа с readOnly=1 означает
# «может читать ордера и позиции», а не «может торговать». Ранняя версия этой
# проверки считала имена групп правами на запись и отвергала исправные ключи.
RECV_WINDOW = "5000"
WINDOW_MS = 7 * 24 * 60 * 60 * 1000  # окно запроса истории у Bybit: <= 7 дней


class BybitError(RuntimeError):
    pass


class UnsafeKeyError(RuntimeError):
    """Ключ имеет права на торговлю или вывод — сохранять и использовать нельзя."""


class Bybit:
    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        self.api_key = api_key or keychain.get("bybit-key")
        self.api_secret = api_secret or keychain.get("bybit-secret")

    # --- транспорт ---------------------------------------------------------

    def _sign(self, timestamp: str, query: str) -> str:
        payload = f"{timestamp}{self.api_key}{RECV_WINDOW}{query}"
        return hmac.new(
            self.api_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        timestamp = str(int(time.time() * 1000))
        request = urllib.request.Request(
            f"{BASE_URL}{path}?{query}" if query else f"{BASE_URL}{path}",
            headers={
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                "X-BAPI-SIGN": self._sign(timestamp, query),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
        except urllib.error.URLError as exc:
            # текст ошибки может содержать URL с параметрами — чистим на всякий случай
            raise BybitError(
                keychain.redact(str(exc), self.api_key, self.api_secret)
            ) from None

        if body.get("retCode") != 0:
            raise BybitError(f"{path}: retCode={body.get('retCode')} {body.get('retMsg')}")
        return body["result"]

    def paginate(self, path: str, **params):
        """Идёт по курсору до конца страницы. Отдаёт записи по одной."""
        cursor = None
        while True:
            result = self.get(path, cursor=cursor, limit=100, **params)
            rows = result.get("list") or []
            yield from rows
            cursor = result.get("nextPageCursor")
            if not cursor or not rows:
                return

    # --- проверка безопасности ключа --------------------------------------

    def assert_read_only(self) -> dict:
        """Блокирующая проверка: ключ не должен уметь писать.

        Совет назвал это условием до первой строки работы с ключами: галочки в
        интерфейсе биржи ставит человек и ошибается. Гейт — поле readOnly,
        единственное, что у Bybit отражает право записи (см. комментарий выше).
        """
        info = self.get("/v5/user/query-api")
        if info.get("readOnly") != 1:
            raise UnsafeKeyError(
                "Ключ умеет писать (readOnly=0) — использовать нельзя.\n"
                f"  области доступа: {info.get('permissions')}\n"
                "Создай на Bybit ключ с переключателем Read-Only."
            )
        return info

    def key_warnings(self, info: dict) -> list[str]:
        """Некритичные замечания по ключу. Не блокируют работу, но их надо видеть."""
        warnings = []
        if info.get("ips") in (None, [], ["*"]):
            warnings.append(
                "IP-whitelist не настроен: ключ примут с любого адреса, "
                "и Bybit ограничивает срок его жизни 3 месяцами"
            )
        if expires := info.get("expiredAt"):
            warnings.append(f"ключ истекает {expires} — после этого бэкфилл встанет")
        return warnings

    # --- данные ------------------------------------------------------------

    def executions(self, start_ms: int, end_ms: int, category: str = "linear"):
        """Все fills за период. Режет период на окна по 7 дней (лимит Bybit)."""
        window_start = start_ms
        while window_start < end_ms:
            window_end = min(window_start + WINDOW_MS, end_ms)
            yield from self.paginate(
                "/v5/execution/list",
                category=category,
                startTime=window_start,
                endTime=window_end,
            )
            window_start = window_end

    def positions(self, category: str = "linear") -> list[dict]:
        """Открытые позиции прямо сейчас.

        Единственное место, где дневник спрашивает у биржи текущее, а не
        прошлое. Нереализованный P&L, плечо и цена ликвидации считаются
        биржей, и пересчитывать их самим незачем: своя оценка разошлась бы с
        той, по которой позицию реально ликвидируют.

        `settleCoin` обязателен, когда символ не указан. Позиции с нулевым
        объёмом биржа отдаёт наравне с живыми — это уже закрытые, они здесь
        отсеиваются.
        """
        rows = self.paginate("/v5/position/list", category=category,
                             settleCoin="USDT")
        return [row for row in rows if (row.get("size") or "0") not in ("0", "")]

    def closed_pnl(self, start_ms: int, end_ms: int, category: str = "linear"):
        """Расчёт закрытых сделок самой биржей. Только для сверки, не источник данных."""
        window_start = start_ms
        while window_start < end_ms:
            window_end = min(window_start + WINDOW_MS, end_ms)
            yield from self.paginate(
                "/v5/position/closed-pnl",
                category=category,
                startTime=window_start,
                endTime=window_end,
            )
            window_start = window_end
