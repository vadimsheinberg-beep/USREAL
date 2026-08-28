"""Общий HTTP-слой: сессия с ретраями, троттлингом и вменяемыми ошибками."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    """Сетевой сбой источника — ловится на уровне источника, запуск не падает."""


class HttpClient:
    """Тонкая обёртка над ``requests.Session``.

    Держит паузу между запросами (израильские госпорталы легко отдают 429),
    повторяет 5xx и уважает общий таймаут.
    """

    def __init__(
        self,
        user_agent: str = "landtender/0.1",
        timeout: int = 45,
        rate_limit_delay: float = 1.0,
        retries: int = 3,
    ) -> None:
        self.timeout = timeout
        self.rate_limit_delay = rate_limit_delay
        self._last_request = 0.0

        self.session = requests.Session()
        retry = Retry(
            total=retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
            }
        )

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_request = time.time()

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self._throttle()
        kwargs.setdefault("timeout", self.timeout)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise HttpError(f"{method} {url}: {exc}") from exc
        if response.status_code >= 400:
            raise HttpError(f"{method} {url}: HTTP {response.status_code}")
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def get_text(self, url: str, **kwargs: Any) -> str:
        """Ответ как текст — для XML вроде WFS GetCapabilities."""
        return self.get(url, **kwargs).text

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.get(url, **kwargs)
        return _decode_json(response, url)

    def post_json(self, url: str, **kwargs: Any) -> Any:
        response = self.post(url, **kwargs)
        return _decode_json(response, url)


def _decode_json(response: requests.Response, url: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        preview = response.text[:200].replace("\n", " ")
        raise HttpError(f"{url}: ответ не JSON ({preview!r})") from exc
