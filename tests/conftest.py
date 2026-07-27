"""Общие приспособления для тестов: фикстуры JSON и поддельный HTTP-клиент."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text("utf-8"))


class FakeHttp:
    """Подменяет :class:`landtender.http.HttpClient` заранее заданными ответами.

    Ключ маршрута — подстрока URL. Значение — либо готовый объект, либо
    вызываемое ``(params, json_body) -> ответ``, либо исключение.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _resolve(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        for fragment, response in self.routes.items():
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(kwargs.get("params") or {}, kwargs.get("json") or {})
                return response
        raise AssertionError(f"В тесте нет маршрута для {method} {url}")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self._resolve("GET", url, **kwargs)

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self._resolve("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._resolve("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._resolve("POST", url, **kwargs)


@pytest.fixture
def fx_rate():
    from landtender.money import FxRate

    # 3.6412 ₪ за доллар — порог в 1 млн $ = 3 641 200 ₪
    return FxRate(rate=3.6412, as_of="2026-07-27", source="test")
