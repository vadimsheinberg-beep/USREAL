"""Разведка API портала рм"י.

Нужна, потому что вслепую гадать бесполезно: в живом запуске детали тендеров
отдавали 404 и обрывы соединения, а минимальная цена так и не появилась.
Команда берёт несколько действующих тендеров и печатает всё, что портал о них
рассказывает: какие варианты эндпоинта отвечают, что лежит в ответе, где в
дереве встречаются числа, похожие на цены, и какие приложены документы.

Ничего не сохраняет и никуда не отправляет — только печатает.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .extract import (
    PRICE_APPRAISAL_KEYS,
    PRICE_FINAL_KEYS,
    PRICE_MIN_KEYS,
    UNITS_KEYS,
    as_list,
    pick,
    to_float,
    walk_dicts,
)
from .http import HttpClient, HttpError
from .sources.rmi_michrazim import BASE_URL, SEARCH_URL, SITE_HEADERS

#: Варианты эндпоинта деталей: имя пути и имя параметра могли измениться.
DETAIL_VARIANTS: tuple[tuple[str, str], ...] = (
    ("/api/MichrazDetailsApi/Get", "michrazID"),
    ("/api/MichrazDetailsApi/Get", "MichrazID"),
    ("/api/MichrazDetailsApi/GetMichraz", "michrazID"),
    ("/api/MichrazDetailsApi/GetMichrazDetails", "michrazID"),
    ("/api/MichrazApi/Get", "michrazID"),
    ("/api/MichrazDetailsApi/GetMichrazMapaDetails", "michrazID"),
)

#: Ниже этой суммы в шекелях число вряд ли является ценой участка.
PRICE_FLOOR = 50_000

#: Поля-идентификаторы бывают крупнее любой цены (номер тендера — 8 цифр),
#: поэтому их приходится исключать по имени, иначе разведка тонет в шуме.
_ID_MARKERS = (
    "id", "kod", "code", "mis", "number", "num", "tik", "gush", "chelka",
    "helka", "date", "year", "shana", "phone", "zip", "index",
)

MAX_JSON_CHARS = 4000


def _fmt(value: Any, limit: int = MAX_JSON_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        return text[:limit] + f"\n… (обрезано, всего {len(text)} символов)"
    return text


def price_candidates(payload: Any) -> list[tuple[str, float]]:
    """Все числовые поля, похожие на денежные суммы, с их именами.

    Именно это и нужно выяснить: как портал называет минимальную цену.
    """
    found: dict[str, float] = {}
    for node in walk_dicts(payload):
        for key, value in node.items():
            if looks_like_identifier(str(key)):
                continue
            number = to_float(value)
            if number is not None and number >= PRICE_FLOOR:
                found.setdefault(str(key), number)
    return sorted(found.items(), key=lambda item: -item[1])


def looks_like_identifier(key: str) -> bool:
    """Похоже ли имя поля на идентификатор, а не на денежную сумму."""
    normalized = key.replace("_", "").replace(" ", "").lower()
    return any(marker in normalized for marker in _ID_MARKERS)


def document_list(payload: Any) -> list[dict[str, Any]]:
    """Приложенные к тендеру документы — там может лежать חוברת המכרז."""
    docs: list[dict[str, Any]] = []
    for node in walk_dicts(payload):
        for key in ("MichrazDocList", "DocList", "Documents", "Mismachim"):
            value = node.get(key)
            if isinstance(value, list):
                docs.extend(item for item in value if isinstance(item, dict))
    return docs


def inspect_tenders(
    http: HttpClient,
    limit: int = 3,
    tender_ids: Iterable[str] | None = None,
    active_only: bool = True,
) -> int:
    """Печатает разведданные. Возвращает код возврата для CLI."""
    print("=" * 72)
    print("РАЗВЕДКА API рм\"י")
    print("=" * 72)

    # --- поиск -------------------------------------------------------------
    try:
        payload = {"ActiveQuickSearch": False, "ActiveMichraz": active_only}
        results = [item for item in as_list(http.post_json(SEARCH_URL, json=payload, headers=SITE_HEADERS))
                   if isinstance(item, dict)]
    except HttpError as exc:
        print(f"✗ Поиск не отвечает: {exc}")
        return 1

    print(f"\nПоиск вернул тендеров: {len(results)}")
    if not results:
        return 1

    print("\n--- Поля записи поиска (первый тендер) ---")
    print(_fmt(results[0]))

    search_prices = price_candidates(results[0])
    print("\n--- Денежные поля в записи поиска ---")
    print(_fmt(search_prices) if search_prices else "нет чисел от 50 000 ₪ и выше")

    # --- какие тендеры смотреть -------------------------------------------
    if tender_ids:
        chosen = list(tender_ids)
    else:
        chosen = []
        for item in results:
            value = pick(item, ("MichrazID", "MichrazId"))
            if value is not None:
                chosen.append(str(int(value)) if isinstance(value, (int, float)) else str(value))
            if len(chosen) >= limit:
                break

    print(f"\nСмотрим детали тендеров: {', '.join(chosen)}")

    # --- варианты эндпоинта ------------------------------------------------
    working: tuple[str, str] | None = None
    for path, param in DETAIL_VARIANTS:
        url = f"{BASE_URL}{path}"
        try:
            data = http.get_json(url, params={param: chosen[0]}, headers=SITE_HEADERS)
        except HttpError as exc:
            print(f"  ✗ {path}?{param}= → {str(exc)[:120]}")
            continue
        size = len(json.dumps(data, default=str)) if data else 0
        print(f"  ✓ {path}?{param}= → ответ {size} символов")
        if working is None and size > 2:
            working = (path, param)

    if working is None:
        print("\n✗ Ни один вариант эндпоинта деталей не отвечает содержимым.")
        print("  Значит, цены надо брать из брошюры тендера, а не из API.")
        return 1

    path, param = working
    print(f"\nРабочий вариант: {path}?{param}=")

    # --- разбор деталей ----------------------------------------------------
    for tender_id in chosen:
        print("\n" + "=" * 72)
        print(f"ТЕНДЕР {tender_id}")
        print("=" * 72)
        try:
            details = http.get_json(f"{BASE_URL}{path}", params={param: tender_id}, headers=SITE_HEADERS)
        except HttpError as exc:
            print(f"✗ детали недоступны: {exc}")
            continue

        if isinstance(details, dict):
            print(f"\nКлючи верхнего уровня: {', '.join(sorted(details.keys()))}")

        print("\n--- Денежные поля во всём дереве ответа ---")
        candidates = price_candidates(details)
        print(_fmt(candidates) if candidates else "нет чисел от 50 000 ₪ и выше")

        print("\n--- Знакомые поля цены ---")
        for label, keys in (
            ("минимальная цена", PRICE_MIN_KEYS),
            ("шума (оценка)", PRICE_APPRAISAL_KEYS),
            ("цена сделки", PRICE_FINAL_KEYS),
            ("единицы жилья", UNITS_KEYS),
        ):
            hits = [pick(node, keys) for node in walk_dicts(details) if pick(node, keys) is not None]
            print(f"  {label}: {hits[:5] if hits else 'не найдено'}")

        docs = document_list(details)
        print(f"\n--- Документы тендера ({len(docs)}) ---")
        print(_fmt(docs[:5]) if docs else "список документов пуст")

        print("\n--- Начало сырого ответа ---")
        print(_fmt(details))

    return 0
