"""Разведка трёх карт-сервисов: govmap, iplan (мавъат) и nadlan.

Те же грабли, что с рм"י: имена слоёв и полей нельзя угадать, их надо
увидеть. Модуль ничего не сохраняет и никуда не отправляет — только печатает,
что порталы реально отдают, чтобы источники писались по фактам.

Знание об эндпоинтах взято из двух MIT-проектов:
  * ``ags.iplan.gov.il/arcgisiplan`` — meirim-org/meirim (server/api/lib/iplanApi.js);
  * ``data.nadlan.gov.il/api`` — Etelis/nadlan-mcp.
WFS govmap описан его же GetCapabilities.
"""

from __future__ import annotations

import json
from typing import Any

from .http import HttpClient, HttpError

# ---------------------------------------------------------------- govmap ----

#: Открытый WFS Геосервер govmap: участки, гуши, нахалот, границы муниципалитетов.
#: Отдаёт GeoJSON без авторизации, координаты в EPSG:2039 (израильская сеть ITM).
GOVMAP_WFS = "https://open.govmap.gov.il/geoserver/opendata/wfs"

#: Автодополнение адресов и «гуш/хелка» — резолвер для человеческого ввода.
GOVMAP_SEARCH = "https://es.govmap.gov.il/TldSearch/api/AutoComplete"

# ----------------------------------------------------------------- iplan ----

#: Национальный реестр планов (מבא"ת) как обычный ArcGIS REST.
#: Слой 1 — границы планов, слой 2 — справочник округов; полный список
#: печатает разведка, гадать по номерам не нужно.
IPLAN_XPLAN = (
    "https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/Xplan/MapServer"
)

# ---------------------------------------------------------------- nadlan ----

#: Статические JSON рынка недвижимости. Без авторизации.
#: Динамический api.nadlan.gov.il/deal-data закрыт reCAPTCHA Enterprise —
#: он сознательно не трогается: обход защиты доступа нам не нужен.
NADLAN_DATA = "https://data.nadlan.gov.il/api"

MAX_JSON_CHARS = 2500


def _fmt(value: Any, limit: int = MAX_JSON_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        return text[:limit] + f"\n… (обрезано, всего {len(text)} символов)"
    return text


def _head(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ============================================================== iplan =======


#: Куда ещё мог переехать сервис. Путь ArcGIS у ведомств отличается
#: (``/arcgisiplan/`` против обычного ``/arcgis/``), а имя сервиса менялось.
IPLAN_VARIANTS: tuple[str, ...] = (
    IPLAN_XPLAN,
    "https://ags.iplan.gov.il/arcgis/rest/services/PlanningPublic/Xplan/MapServer",
    "https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/XplanPublic/MapServer",
    "https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/Xplan/FeatureServer",
)


def _reach_iplan(http: HttpClient) -> tuple[str, Any] | None:
    """Находит рабочий адрес сервиса, пробуя обычный TLS и старый набор шифров.

    Сервер отвечал ``SSLV3_ALERT_HANDSHAKE_FAILURE``, поэтому проверяем не
    только адреса, но и настройку TLS — иначе непонятно, что именно сломано:
    адрес, шифры или сам сервис.
    """
    for legacy in (False, True):
        if legacy:
            print("\n… обычный TLS не прошёл, пробую старый набор шифров")
            http.use_legacy_tls("https://ags.iplan.gov.il")
        for url in IPLAN_VARIANTS:
            try:
                data = http.get_json(url, params={"f": "json"})
            except HttpError as exc:
                print(f"  ✗ {url}\n      {exc}")
                continue
            if isinstance(data, dict) and data.get("error"):
                print(f"  ✗ {url} → ошибка сервиса: {data['error']}")
                continue
            print(f"  ✓ {url}" + (" (старый TLS)" if legacy else ""))
            return url, data
    return None


def inspect_iplan(http: HttpClient, gush: str | None = None) -> int:
    """Слои реестра планов и настоящие имена их полей.

    Ради этого всё и затевается: план, меняющий назначение участка с
    сельхоза на жильё, — единственное, что превращает дешёвую землю в дорогую.
    """
    _head("РАЗВЕДКА IPLAN (реестр планов, ArcGIS REST)")

    reached = _reach_iplan(http)
    if reached is None:
        print("\n✗ Ни один адрес сервиса не ответил.")
        return 1
    base, service = reached

    layers = (service or {}).get("layers") or []
    print(f"\nСлоёв в сервисе: {len(layers)}")
    for layer in layers:
        print(f"  [{layer.get('id')}] {layer.get('name')} — {layer.get('geometryType')}")

    if not layers:
        print("✗ Список слоёв пуст — схема сервиса изменилась.")
        return 1

    for layer in layers:
        layer_id = layer.get("id")
        print("\n" + "-" * 72)
        print(f"Слой {layer_id}: {layer.get('name')}")
        try:
            meta = http.get_json(f"{base}/{layer_id}", params={"f": "json"})
        except HttpError as exc:
            print(f"  ✗ описание недоступно: {exc}")
            continue

        fields = [(f.get("name"), f.get("type"), f.get("alias")) for f in (meta.get("fields") or [])]
        print(f"  Полей: {len(fields)}")
        for name, ftype, alias in fields:
            print(f"    {name:<28} {str(ftype).replace('esriFieldType', ''):<12} {alias or ''}")

    # Одна живая запись из слоя планов: по ней видно и формат значений,
    # и что вообще лежит в статусе и назначении.
    print("\n" + "-" * 72)
    print("Пример записи из слоя планов")
    params = {
        "f": "json",
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": 2,
        "where": f"gush_num={gush}" if gush else "objectid>0",
    }
    try:
        sample = http.get_json(f"{base}/1/query", params=params)
    except HttpError as exc:
        print(f"  ✗ запрос не прошёл: {exc}")
        return 1

    features = (sample or {}).get("features") or []
    print(f"  Найдено: {len(features)}")
    for feature in features[:2]:
        print(_fmt(feature.get("attributes")))
    return 0


# ============================================================== govmap ======


def inspect_govmap(http: HttpClient, gush: str | None = None, helka: str | None = None) -> int:
    """Типы объектов открытого WFS и поля участка.

    Участок по гуш/хелка даёт полигон и площадь — то, чего нет в тендере,
    когда портал рм"י оставляет ``Shetach`` пустым.
    """
    _head("РАЗВЕДКА GOVMAP (открытый WFS)")

    try:
        caps = http.get_text(
            GOVMAP_WFS,
            params={"service": "wfs", "version": "2.0.0", "request": "GetCapabilities"},
        )
    except HttpError as exc:
        print(f"✗ GetCapabilities не отвечает: {exc}")
        return 1

    # Разбирать XML целиком незачем — нужны только имена типов объектов.
    names = _feature_type_names(caps)
    print(f"\nТипов объектов: {len(names)}")
    for name in names:
        print(f"  {name}")

    target = "opendata:PARCEL_ALL"
    where = None
    if gush and helka:
        where = f"GUSH_NUM={gush} AND PARCEL={helka}"
    print("\n" + "-" * 72)
    print(f"Пример записи: {target}" + (f" ({where})" if where else ""))

    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": target,
        "count": "2",
        "outputFormat": "application/json",
    }
    if where:
        params["cql_filter"] = where

    try:
        data = http.get_json(GOVMAP_WFS, params=params)
    except HttpError as exc:
        print(f"  ✗ запрос не прошёл: {exc}")
        return 1

    features = (data or {}).get("features") or []
    print(f"  Найдено: {len(features)} · CRS: {(data or {}).get('crs')}")
    for feature in features[:2]:
        print("  Поля:", list((feature.get("properties") or {}).keys()))
        print(_fmt(feature.get("properties"), 1200))
    return 0


def _feature_type_names(capabilities: str) -> list[str]:
    """Имена типов объектов из GetCapabilities без разбора всего XML."""
    import re

    return sorted(set(re.findall(r"<Name>([^<]+)</Name>", capabilities)))


# ============================================================== nadlan ======


def inspect_nadlan(http: HttpClient, settlement_code: str = "5000") -> int:
    """Статические справочники рынка. По умолчанию — Тель-Авив (код 5000)."""
    _head("РАЗВЕДКА NADLAN (статические JSON)")
    print(
        "\nДинамический api.nadlan.gov.il/deal-data закрыт reCAPTCHA Enterprise "
        "и здесь сознательно не трогается."
    )

    probes = (
        ("справочник населённых пунктов", f"{NADLAN_DATA}/index/setl_types.json"),
        ("коды типов недвижимости", f"{NADLAN_DATA}/index/dealNatureIndex.json"),
        ("страница НП (покупка)", f"{NADLAN_DATA}/pages/settlement/buy/{settlement_code}.json"),
    )

    ok = 0
    for title, url in probes:
        print("\n" + "-" * 72)
        print(f"{title}: {url}")
        try:
            data = http.get_json(url)
        except HttpError as exc:
            print(f"  ✗ {exc}")
            continue
        ok += 1
        if isinstance(data, dict):
            print(f"  Ключи верхнего уровня: {sorted(data.keys())[:40]}")
            first = next(iter(data.items()), None)
            if first is not None:
                print(f"  Первый элемент [{first[0]}]:")
                print(_fmt(first[1], 1200))
        elif isinstance(data, list):
            print(f"  Записей: {len(data)}")
            if data:
                print(_fmt(data[0], 1200))
    return 0 if ok else 1
