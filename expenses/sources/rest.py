"""Универсальный коннектор к REST API банка или платёжного сервиса.

Клиент ничего не знает о конкретном API: адрес, способ авторизации,
параметры периода, схема пагинации и имена полей задаются в
``expenses.toml``. Для API с обычной авторизацией по токену этого
достаточно — код править не нужно. Если API требует подпись запроса
(как Bybit), нужен отдельный модуль-источник, см. :mod:`expenses.sources.bybit`.

Порядок подключения:

1. ``expenses probe --source rest`` — покажет сырой ответ и угаданные поля;
2. переносим имена полей в ``[sources.rest.fields]``;
3. ``expenses fetch --source rest`` — операции сохраняются в хранилище.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..models import Transaction
from .base import FieldMap, extract_records, normalize_records, pick

log = logging.getLogger(__name__)

SOURCE_NAME = "rest"


class RestError(RuntimeError):
    """Сетевая или конфигурационная проблема при обращении к источнику."""


@dataclass
class RestConfig:
    """Настройки подключения. Всё, кроме ``base_url``, имеет разумный дефолт."""

    base_url: str = ""
    transactions_path: str = "/transactions"
    method: str = "GET"

    #: ``bearer`` | ``header`` | ``query`` | ``basic`` | ``none``.
    auth: str = "bearer"
    #: Имя переменной окружения с токеном. Токен в конфиг не кладём.
    token_env: str = "EXPENSES_API_TOKEN"
    auth_header: str = "Authorization"
    auth_query_param: str = "api_key"
    basic_user_env: str = "EXPENSES_API_USER"

    #: Статические query-параметры (id счёта и подобное).
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    #: Имена параметров периода и формат даты для них.
    date_from_param: str = "from"
    date_to_param: str = "to"
    date_format: str = "%Y-%m-%d"

    #: ``page`` | ``offset`` | ``cursor`` | ``none``.
    pagination: str = "page"
    page_param: str = "page"
    offset_param: str = "offset"
    size_param: str = "limit"
    cursor_param: str = "cursor"
    #: Где в ответе лежит курсор следующей страницы.
    cursor_path: str = "meta.next_cursor"
    page_size: int = 200
    #: Страховка от бесконечного цикла, если API игнорирует пагинацию.
    max_pages: int = 100
    #: С какой страницы нумерация: у одних API с 0, у других с 1.
    first_page: int = 1

    timeout: int = 30
    retries: int = 3
    verify_ssl: bool = True

    #: Маппинг полей ответа на модель транзакции.
    fields: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RestConfig":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known - {"enabled"}
        if unknown:
            log.warning("неизвестные настройки источника: %s", ", ".join(sorted(unknown)))
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def field_map(self) -> FieldMap:
        return FieldMap.from_config(self.fields)


class RestClient:
    """HTTP-клиент к произвольному REST API: авторизация, пагинация, ретраи."""

    def __init__(self, config: RestConfig) -> None:
        if not config.base_url:
            raise RestError(
                "не задан base_url для источника — заполните [sources.rest] в expenses.toml"
            )
        self.config = config
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=self.config.retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({"Accept": "application/json", "User-Agent": "expenses/0.1"})
        session.headers.update(self.config.headers)

        token = self._token()
        auth = self.config.auth.lower()
        if auth == "bearer":
            session.headers[self.config.auth_header] = f"Bearer {token}"
        elif auth == "header":
            session.headers[self.config.auth_header] = token
        elif auth == "basic":
            user = os.environ.get(self.config.basic_user_env, "")
            session.auth = (user, token)
        elif auth not in {"query", "none"}:
            raise RestError(f"неизвестный способ авторизации: {self.config.auth}")
        return session

    def _token(self) -> str:
        if self.config.auth.lower() == "none":
            return ""
        token = os.environ.get(self.config.token_env, "").strip()
        if not token:
            raise RestError(
                f"нет токена: переменная окружения {self.config.token_env} пуста. "
                "Положите токен в .env — в git он не попадёт."
            )
        return token

    def _url(self) -> str:
        return self.config.base_url.rstrip("/") + "/" + self.config.transactions_path.lstrip("/")

    def _base_params(self, since: date | None, until: date | None) -> dict[str, Any]:
        params: dict[str, Any] = dict(self.config.params)
        if since and self.config.date_from_param:
            params[self.config.date_from_param] = since.strftime(self.config.date_format)
        if until and self.config.date_to_param:
            params[self.config.date_to_param] = until.strftime(self.config.date_format)
        if self.config.auth.lower() == "query":
            params[self.config.auth_query_param] = self._token()
        if self.config.pagination != "none" and self.config.size_param:
            params[self.config.size_param] = self.config.page_size
        return params

    def _request(self, params: dict[str, Any]) -> Any:
        url = self._url()
        try:
            response = self.session.request(
                self.config.method.upper(),
                url,
                params=params if self.config.method.upper() == "GET" else None,
                json=params if self.config.method.upper() != "GET" else None,
                timeout=self.config.timeout,
                verify=self.config.verify_ssl,
            )
        except requests.RequestException as exc:
            raise RestError(f"запрос к API не прошёл: {exc}") from exc

        if response.status_code in (401, 403):
            raise RestError(
                f"API отверг авторизацию ({response.status_code}). "
                f"Проверьте {self.config.token_env} и способ auth={self.config.auth}."
            )
        if response.status_code >= 400:
            raise RestError(
                f"API ответил {response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise RestError(f"ответ API не JSON: {response.text[:200]}") from exc

    def pages(self, since: date | None = None, until: date | None = None) -> Iterator[Any]:
        """Отдаёт ответы постранично, пока страницы не кончатся."""
        mode = self.config.pagination.lower()
        params = self._base_params(since, until)

        if mode == "none":
            yield self._request(params)
            return

        cursor: Any = None
        for index in range(self.config.max_pages):
            page_params = dict(params)
            if mode == "page":
                page_params[self.config.page_param] = self.config.first_page + index
            elif mode == "offset":
                page_params[self.config.offset_param] = index * self.config.page_size
            elif mode == "cursor":
                if index and not cursor:
                    return
                if cursor:
                    page_params[self.config.cursor_param] = cursor
            else:
                raise RestError(f"неизвестный режим пагинации: {self.config.pagination}")

            payload = self._request(page_params)
            yield payload

            records = _safe_records(payload, self.config.field_map.records_path)
            if mode == "cursor":
                cursor = pick(payload, self.config.cursor_path) if isinstance(payload, dict) else None
                if not cursor:
                    return
            elif len(records) < self.config.page_size:
                #: Неполная страница — значит она последняя.
                return
        log.warning("достигнут предел в %d страниц, часть данных могла не попасть", self.config.max_pages)

    def fetch(self, since: date | None = None, until: date | None = None) -> list[Transaction]:
        """Забирает операции за период и нормализует их."""
        mapping = self.config.field_map
        transactions: list[Transaction] = []
        for payload in self.pages(since, until):
            records = extract_records(payload, mapping.records_path)
            transactions += normalize_records(records, mapping, SOURCE_NAME)
        log.info("%s: получено операций — %d", SOURCE_NAME, len(transactions))
        return transactions

    def probe(self, since: date | None = None, until: date | None = None) -> str:
        """Показывает сырой ответ — чтобы составить маппинг полей.

        Ничего не нормализует и не падает на неизвестной схеме: задача
        команды — дать увидеть, что именно отдаёт API.
        """
        payload = next(iter(self.pages(since, until)), None)
        if payload is None:
            return "API ничего не вернул."

        lines = ["Ответ API (первая страница):", "-" * 56]
        lines.append(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
        lines.append("-" * 56)

        try:
            records = extract_records(payload, self.config.field_map.records_path)
        except Exception as exc:  # схема неизвестна — это ожидаемо
            lines.append(f"Список операций не найден автоматически: {exc}")
            return "\n".join(lines)

        lines.append(f"Найдено записей на странице: {len(records)}")
        if records:
            keys = sorted(records[0].keys())
            lines.append("Поля первой записи: " + ", ".join(keys))
            lines.append("")
            lines.append("Перенесите нужные в [sources.rest.fields], например:")
            lines.append("  date = \"" + _guess(keys, ("date", "time", "created")) + "\"")
            lines.append("  amount = \"" + _guess(keys, ("amount", "sum", "value", "price")) + "\"")
            lines.append("  description = \"" + _guess(keys, ("desc", "merchant", "name", "title")) + "\"")
        return "\n".join(lines)


def _guess(keys: list[str], hints: tuple[str, ...]) -> str:
    for key in keys:
        if any(hint in key.lower() for hint in hints):
            return key
    return "???"


def _safe_records(payload: Any, path: Any) -> list[dict[str, Any]]:
    try:
        return extract_records(payload, path)
    except Exception:
        return []


def fetch_rest(
    raw_config: dict[str, Any],
    since: date | None = None,
    until: date | None = None,
) -> list[Transaction]:
    """Удобная обёртка: конфиг → список транзакций."""
    return RestClient(RestConfig.from_dict(raw_config)).fetch(since, until)
