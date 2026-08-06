"""Коннектор к Bybit V5.

Биржа — не банк: «расход» здесь не поход в магазин, а вывод средств,
комиссия или отрицательный фандинг. Что именно считать расходом,
описано в :func:`_from_transaction_log` и настраивается в конфиге.

Забираются три источника записей:

* ``/v5/asset/deposit/query-record``  — вводы средств;
* ``/v5/asset/withdraw/query-record`` — выводы и комиссии за них;
* ``/v5/account/transaction-log``     — движения по единому счёту:
  комиссии, фандинг, переводы, результаты сделок.

Ключу достаточно прав только на чтение. Торговые права и права на
перевод средств анализатору не нужны — не выдавайте их.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..models import DIRECTION_EXPENSE, DIRECTION_INCOME, Transaction

log = logging.getLogger(__name__)

SOURCE_NAME = "bybit"

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"

DEPOSIT_PATH = "/v5/asset/deposit/query-record"
WITHDRAW_PATH = "/v5/asset/withdraw/query-record"
TX_LOG_PATH = "/v5/account/transaction-log"

#: Машинные метки операций. По ним работают правила категоризации,
#: поэтому они стабильны и не зависят от языка отчёта.
TAG_DEPOSIT = "bybit:deposit"
TAG_WITHDRAW = "bybit:withdraw"
TAG_FEE = "bybit:fee"
TAG_FUNDING = "bybit:funding"
TAG_TRADE = "bybit:trade"
TAG_TRANSFER = "bybit:transfer"
TAG_INTEREST = "bybit:interest"
TAG_OTHER = "bybit:other"

#: Как типы из transaction-log ложатся на метки.
_LOG_TYPE_TAGS: dict[str, str] = {
    "TRADE": TAG_TRADE,
    "SETTLEMENT": TAG_FUNDING,
    "DELIVERY": TAG_TRADE,
    "LIQUIDATION": TAG_TRADE,
    "ADL": TAG_TRADE,
    "BONUS": TAG_OTHER,
    "BONUS_RECOLLECT": TAG_OTHER,
    "FEE_REFUND": TAG_FEE,
    "INTEREST": TAG_INTEREST,
    "CURRENCY_BUY": TAG_TRADE,
    "CURRENCY_SELL": TAG_TRADE,
    "TRANSFER_IN": TAG_TRANSFER,
    "TRANSFER_OUT": TAG_TRANSFER,
    "AIRDROP": TAG_OTHER,
}


class BybitError(RuntimeError):
    """Ошибка обращения к Bybit: сеть, авторизация или ненулевой retCode."""


@dataclass
class BybitConfig:
    """Настройки подключения к бирже."""

    #: Ключи читаются из окружения — в конфиг и в git они не попадают.
    api_key_env: str = "BYBIT_API_KEY"
    api_secret_env: str = "BYBIT_API_SECRET"
    testnet: bool = False
    base_url: str = ""

    #: UNIFIED — единый торговый счёт, у старых аккаунтов бывает CONTRACT.
    account_type: str = "UNIFIED"

    #: Какие наборы записей забирать.
    endpoints: tuple[str, ...] = ("deposits", "withdrawals", "transaction_log")

    #: Отдельные сделки — это шум в отчёте о расходах: они не тратят
    #: деньги, а меняют одну монету на другую. По умолчанию берём из
    #: сделок только комиссию.
    include_trades: bool = False
    #: Переводы между своими счетами деньгами не являются, но иногда
    #: полезно их видеть.
    include_transfers: bool = False

    #: Bybit ограничивает окно выборки; длинный период режем на куски.
    log_window_days: int = 7
    asset_window_days: int = 30

    recv_window: int = 5000
    page_limit: int = 50
    max_pages: int = 200
    timeout: int = 30
    retries: int = 3
    #: Пауза между запросами: у Bybit пожёсткие лимиты на приватные ручки.
    rate_limit_delay: float = 0.2

    #: Сколько дней истории брать, если период не задан явно.
    default_days: int = 180

    fields: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BybitConfig":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known - {"enabled"}
        if unknown:
            log.warning("неизвестные настройки Bybit: %s", ", ".join(sorted(unknown)))
        payload = {k: v for k, v in raw.items() if k in known}
        if "endpoints" in payload:
            payload["endpoints"] = tuple(payload["endpoints"])
        return cls(**payload)

    @property
    def url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return TESTNET_URL if self.testnet else MAINNET_URL


def _ms(day: date, *, end_of_day: bool = False) -> int:
    """Дата → unix-время в миллисекундах, как того хочет Bybit."""
    moment = datetime.combine(day, datetime.max.time() if end_of_day else datetime.min.time())
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _windows(since: date, until: date, days: int) -> Iterator[tuple[date, date]]:
    """Режет период на окна допустимой длины."""
    start = since
    while start <= until:
        end = min(start + timedelta(days=days - 1), until)
        yield start, end
        start = end + timedelta(days=1)


class BybitClient:
    """Клиент Bybit V5: подпись запросов, пагинация по курсору, нарезка периода."""

    def __init__(self, config: BybitConfig) -> None:
        self.config = config
        self.api_key = os.environ.get(config.api_key_env, "").strip()
        self.api_secret = os.environ.get(config.api_secret_env, "").strip()
        if not self.api_key or not self.api_secret:
            raise BybitError(
                f"нет ключей: заполните {config.api_key_env} и {config.api_secret_env} "
                "в .env (в git этот файл не попадает). Ключу нужны только права на чтение."
            )
        self.session = self._build_session()
        self._last_request = 0.0

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        #: Ретраим только сетевые сбои и 5xx: ошибку подписи повторять смысла нет.
        retry = Retry(
            total=self.config.retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"Accept": "application/json", "User-Agent": "expenses/0.1"})
        return session

    def _sign(self, timestamp: str, query: str) -> str:
        """Подпись Bybit V5: HMAC-SHA256 от ``ts + key + recv_window + query``."""
        payload = f"{timestamp}{self.api_key}{self.config.recv_window}{query}"
        return hmac.new(
            self.api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.config.rate_limit_delay:
            time.sleep(self.config.rate_limit_delay - elapsed)
        self._last_request = time.monotonic()

    def request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Один подписанный GET. Возвращает ``result`` из ответа."""
        #: Подписывается ровно та строка запроса, которая уйдёт в URL,
        #: поэтому собираем её сами и не отдаём params в requests.
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        query = urlencode(clean)
        timestamp = str(int(time.time() * 1000))

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": str(self.config.recv_window),
            "X-BAPI-SIGN": self._sign(timestamp, query),
            "X-BAPI-SIGN-TYPE": "2",
        }
        url = f"{self.config.url}{path}" + (f"?{query}" if query else "")

        self._throttle()
        try:
            response = self.session.get(url, headers=headers, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise BybitError(f"запрос к Bybit не прошёл: {exc}") from exc

        if response.status_code >= 400:
            raise BybitError(f"Bybit ответил {response.status_code}: {response.text[:200]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BybitError(f"ответ Bybit не JSON: {response.text[:200]}") from exc

        code = payload.get("retCode")
        if code:
            raise BybitError(_explain(code, payload.get("retMsg", "")))
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def paginate(self, path: str, params: dict[str, Any], key: str) -> Iterator[dict[str, Any]]:
        """Идёт по страницам курсором, пока Bybit его отдаёт."""
        cursor: str | None = None
        for _ in range(self.config.max_pages):
            page_params = dict(params, limit=self.config.page_limit)
            if cursor:
                page_params["cursor"] = cursor
            result = self.request(path, page_params)

            rows = result.get(key) or []
            for row in rows:
                if isinstance(row, dict):
                    yield row

            cursor = result.get("nextPageCursor") or None
            if not cursor or not rows:
                return
        log.warning("достигнут предел в %d страниц для %s", self.config.max_pages, path)

    # ------------------------------------------------------------------
    # Отдельные наборы записей
    # ------------------------------------------------------------------

    def deposits(self, since: date, until: date) -> list[Transaction]:
        items: list[Transaction] = []
        for start, end in _windows(since, until, self.config.asset_window_days):
            params = {"startTime": _ms(start), "endTime": _ms(end, end_of_day=True)}
            for row in self.paginate(DEPOSIT_PATH, params, "rows"):
                tx = _from_deposit(row)
                if tx:
                    items.append(tx)
        return items

    def withdrawals(self, since: date, until: date) -> list[Transaction]:
        items: list[Transaction] = []
        for start, end in _windows(since, until, self.config.asset_window_days):
            params = {"startTime": _ms(start), "endTime": _ms(end, end_of_day=True)}
            for row in self.paginate(WITHDRAW_PATH, params, "rows"):
                items.extend(_from_withdrawal(row))
        return items

    def transaction_log(self, since: date, until: date) -> list[Transaction]:
        items: list[Transaction] = []
        for start, end in _windows(since, until, self.config.log_window_days):
            params = {
                "accountType": self.config.account_type,
                "startTime": _ms(start),
                "endTime": _ms(end, end_of_day=True),
            }
            for row in self.paginate(TX_LOG_PATH, params, "list"):
                items.extend(
                    _from_transaction_log(
                        row,
                        include_trades=self.config.include_trades,
                        include_transfers=self.config.include_transfers,
                    )
                )
        return items

    def fetch(self, since: date | None = None, until: date | None = None) -> list[Transaction]:
        """Забирает все включённые наборы за период."""
        until = until or date.today()
        since = since or until - timedelta(days=self.config.default_days)
        if since > until:
            raise BybitError(f"начало периода ({since}) позже конца ({until})")

        handlers = {
            "deposits": self.deposits,
            "withdrawals": self.withdrawals,
            "transaction_log": self.transaction_log,
        }
        result: list[Transaction] = []
        for name in self.config.endpoints:
            handler = handlers.get(name)
            if handler is None:
                log.warning("неизвестный набор данных Bybit: %s", name)
                continue
            items = handler(since, until)
            log.info("Bybit/%s: операций — %d", name, len(items))
            result += items
        return sorted(result, key=lambda t: t.date)

    def probe(self, since: date | None = None, until: date | None = None) -> str:
        """Проверяет ключ и показывает, что доступно, ничего не сохраняя."""
        until = until or date.today()
        since = since or until - timedelta(days=self.config.asset_window_days - 1)

        lines = [f"Bybit {self.config.url}, счёт {self.config.account_type}", "-" * 56]
        checks = (
            ("вводы", DEPOSIT_PATH, {"startTime": _ms(since), "endTime": _ms(until, end_of_day=True)}, "rows"),
            ("выводы", WITHDRAW_PATH, {"startTime": _ms(since), "endTime": _ms(until, end_of_day=True)}, "rows"),
            (
                "журнал операций",
                TX_LOG_PATH,
                {
                    "accountType": self.config.account_type,
                    "startTime": _ms(max(since, until - timedelta(days=self.config.log_window_days - 1))),
                    "endTime": _ms(until, end_of_day=True),
                },
                "list",
            ),
        )
        for title, path, params, key in checks:
            try:
                result = self.request(path, dict(params, limit=5))
            except BybitError as exc:
                lines.append(f"  {title:<18} ошибка: {exc}")
                continue
            rows = result.get(key) or []
            lines.append(f"  {title:<18} записей за период: {len(rows)}")
            if rows:
                lines.append("    поля: " + ", ".join(sorted(rows[0].keys())))
        return "\n".join(lines)


def _explain(code: Any, message: str) -> str:
    """Переводит коды Bybit в понятную причину отказа."""
    hints = {
        10003: "неверный API-ключ (или он от другой среды: mainnet/testnet)",
        10004: "не сошлась подпись — проверьте API secret",
        10005: "у ключа нет нужных прав. Хватит прав только на чтение",
        10006: "превышен лимит запросов, увеличьте rate_limit_delay",
        10010: "запрос не с разрешённого IP",
        10016: "сервис Bybit временно недоступен",
        10018: "превышен лимит частоты запросов по IP",
    }
    try:
        hint = hints.get(int(code))
    except (TypeError, ValueError):
        hint = None
    tail = f" — {hint}" if hint else ""
    return f"Bybit вернул retCode={code} ({message}){tail}"


def _to_float(value: Any) -> float:
    """Bybit отдаёт числа строками, а пустое значение — пустой строкой."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_date(value: Any) -> date | None:
    if value in (None, "", "0"):
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).date()


def _from_deposit(row: dict[str, Any]) -> Transaction | None:
    """Ввод средств — поступление."""
    when = _to_date(row.get("successAt") or row.get("createTime"))
    amount = _to_float(row.get("amount"))
    if when is None or amount == 0:
        return None
    coin = str(row.get("coin") or "USDT").upper()
    chain = row.get("chain") or ""
    return Transaction(
        date=when,
        amount=amount,
        description=f"Ввод {coin}" + (f" ({chain})" if chain else ""),
        currency=coin,
        direction=DIRECTION_INCOME,
        source=SOURCE_NAME,
        source_id=str(row.get("txID") or "") or None,
        merchant="bybit deposit",
        source_category=TAG_DEPOSIT,
        account="bybit",
    )


def _from_withdrawal(row: dict[str, Any]) -> list[Transaction]:
    """Вывод средств: сама сумма и отдельной строкой комиссия сети."""
    when = _to_date(row.get("updateTime") or row.get("createTime"))
    if when is None:
        return []
    coin = str(row.get("coin") or "USDT").upper()
    chain = row.get("chain") or ""
    withdraw_id = str(row.get("withdrawId") or row.get("txID") or "") or None
    items: list[Transaction] = []

    amount = _to_float(row.get("amount"))
    if amount:
        items.append(
            Transaction(
                date=when,
                amount=amount,
                description=f"Вывод {coin}" + (f" ({chain})" if chain else ""),
                currency=coin,
                direction=DIRECTION_EXPENSE,
                source=SOURCE_NAME,
                source_id=withdraw_id,
                merchant="bybit withdraw",
                source_category=TAG_WITHDRAW,
                account="bybit",
            )
        )

    fee = _to_float(row.get("withdrawFee"))
    if fee:
        items.append(
            Transaction(
                date=when,
                amount=fee,
                description=f"Комиссия за вывод {coin}",
                currency=coin,
                direction=DIRECTION_EXPENSE,
                source=SOURCE_NAME,
                source_id=f"{withdraw_id}:fee" if withdraw_id else None,
                merchant="bybit fee",
                source_category=TAG_FEE,
                account="bybit",
            )
        )
    return items


def _from_transaction_log(
    row: dict[str, Any], *, include_trades: bool = False, include_transfers: bool = False
) -> list[Transaction]:
    """Разбирает запись журнала операций единого счёта.

    Одна запись даёт до двух транзакций: движение средств (``cashFlow``,
    например фандинг) и комиссию (``fee``). Комиссия — всегда расход, она
    и есть настоящая трата. Сама сделка деньги не тратит, а меняет одну
    монету на другую, поэтому по умолчанию в отчёт не попадает.
    """
    when = _to_date(row.get("transactionTime"))
    if when is None:
        return []

    coin = str(row.get("currency") or "USDT").upper()
    log_type = str(row.get("type") or "").upper()
    tag = _LOG_TYPE_TAGS.get(log_type, TAG_OTHER)
    symbol = row.get("symbol") or ""
    items: list[Transaction] = []

    if tag == TAG_TRANSFER and not include_transfers:
        return []

    cash_flow = _to_float(row.get("cashFlow"))
    if cash_flow and (tag != TAG_TRADE or include_trades):
        items.append(
            Transaction(
                date=when,
                amount=abs(cash_flow),
                description=f"{log_type} {symbol} {coin}".strip(),
                currency=coin,
                direction=DIRECTION_EXPENSE if cash_flow < 0 else DIRECTION_INCOME,
                source=SOURCE_NAME,
                source_id=str(row.get("id") or "") or None,
                merchant=f"bybit {tag.split(':')[-1]}",
                source_category=tag,
                account="bybit",
            )
        )

    fee = _to_float(row.get("fee"))
    if fee:
        #: Положительная fee — списанная комиссия, отрицательная — возврат.
        items.append(
            Transaction(
                date=when,
                amount=abs(fee),
                description=f"Комиссия {log_type} {symbol}".strip(),
                currency=coin,
                direction=DIRECTION_EXPENSE if fee > 0 else DIRECTION_INCOME,
                source=SOURCE_NAME,
                source_id=(f"{row.get('id')}:fee" if row.get("id") else None),
                merchant="bybit fee",
                source_category=TAG_FEE,
                account="bybit",
            )
        )
    return items


def fetch_bybit(
    raw_config: dict[str, Any], since: date | None = None, until: date | None = None
) -> list[Transaction]:
    """Удобная обёртка: конфиг → список транзакций."""
    return BybitClient(BybitConfig.from_dict(raw_config)).fetch(since, until)
