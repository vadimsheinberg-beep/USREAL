"""Импорт файлов, хранилище, пересчёт валют, REST-клиент и CLI."""

import json
from datetime import date

import pytest

from expenses.cli import main
from expenses.config import Config, parse_env_file
from expenses.fx import convert, currencies
from expenses.models import Transaction
from expenses.report import render_csv, render_json, render_markdown, render_text
from expenses.sources.rest import RestClient, RestConfig, RestError
from expenses.sources.base import FieldMap, SourceError
from expenses.sources.files import guess_mapping, load_file
from expenses.storage import Store

CSV_HEBREW = (
    "תאריך עסקה,שם בית עסק,סכום חיוב,מטבע\n"
    "15/03/2026,שופרסל דיל,-247.80,ILS\n"
    "16/03/2026,AROMA ESPRESSO,-32.00,ILS\n"
)

CSV_ENGLISH = (
    "Date,Description,Amount,Currency\n"
    "2026-03-15,RAMI LEVY,-247.80,ILS\n"
    "2026-03-16,SALARY,20000.00,ILS\n"
)


class TestFileImport:
    def test_hebrew_headers_are_recognised(self, tmp_path):
        path = tmp_path / "outcome.csv"
        path.write_text(CSV_HEBREW, encoding="utf-8")
        items = load_file(path)
        assert len(items) == 2
        assert items[0].date == date(2026, 3, 15)
        assert items[0].amount == pytest.approx(247.80)
        assert items[0].currency == "ILS"

    def test_english_headers_and_income(self, tmp_path):
        path = tmp_path / "statement.csv"
        path.write_text(CSV_ENGLISH, encoding="utf-8")
        items = load_file(path)
        assert [t.direction for t in items] == ["expense", "income"]

    def test_semicolon_delimiter(self, tmp_path):
        path = tmp_path / "eu.csv"
        path.write_text("Date;Description;Amount\n2026-03-15;CAFE;-12,50\n", encoding="utf-8")
        assert load_file(path)[0].amount == pytest.approx(12.5)

    def test_unrecognised_headers_explain_themselves(self):
        with pytest.raises(SourceError, match="expenses.toml"):
            guess_mapping(["col1", "col2"])

    def test_json_array(self, tmp_path):
        path = tmp_path / "tx.json"
        path.write_text(
            json.dumps([{"id": "1", "date": "2026-03-15", "amount": -10, "description": "X"}]),
            encoding="utf-8",
        )
        assert load_file(path)[0].source_id == "1"

    def test_jsonl(self, tmp_path):
        path = tmp_path / "tx.jsonl"
        path.write_text(
            '{"id":"1","date":"2026-03-15","amount":-10,"description":"X"}\n'
            '{"id":"2","date":"2026-03-16","amount":-20,"description":"Y"}\n',
            encoding="utf-8",
        )
        assert len(load_file(path)) == 2

    def test_missing_file(self, tmp_path):
        with pytest.raises(SourceError, match="не найден"):
            load_file(tmp_path / "нет.csv")

    def test_unknown_extension(self, tmp_path):
        path = tmp_path / "statement.xlsx"
        path.write_bytes(b"PK")
        with pytest.raises(SourceError, match="не знаю"):
            load_file(path)


class TestStore:
    def test_roundtrip(self, tmp_path):
        store = Store(tmp_path / "data.jsonl")
        store.add([Transaction(date=date(2026, 3, 1), amount=10, description="A", source="t")])
        store.save()

        reopened = Store(tmp_path / "data.jsonl").load()
        assert len(reopened) == 1
        assert reopened[0].description == "A"

    def test_duplicates_are_not_added_twice(self, tmp_path):
        item = Transaction(
            date=date(2026, 3, 1), amount=10, description="A", source="bybit", source_id="x1"
        )
        store = Store(tmp_path / "data.jsonl")
        assert store.add([item]) == (1, 0)
        assert store.add([item]) == (0, 1)
        assert len(store) == 1

    def test_broken_line_does_not_kill_the_file(self, tmp_path):
        path = tmp_path / "data.jsonl"
        path.write_text(
            '{"date":"2026-03-01","amount":10,"description":"A","source":"t"}\n'
            "{битая строка}\n",
            encoding="utf-8",
        )
        assert len(Store(path).load()) == 1

    def test_missing_file_reads_as_empty(self, tmp_path):
        assert Store(tmp_path / "нет.jsonl").load() == []


class TestFx:
    def test_converts_foreign_currency(self):
        items = [
            Transaction(date=date(2026, 3, 1), amount=100, description="A", currency="USD"),
            Transaction(date=date(2026, 3, 1), amount=50, description="B", currency="ILS"),
        ]
        convert(items, "ILS", {"USD": 3.7})
        assert items[0].report_amount == pytest.approx(370)
        assert items[1].report_amount == pytest.approx(50)

    def test_missing_rate_leaves_amount_untouched(self, caplog):
        items = [Transaction(date=date(2026, 3, 1), amount=100, description="A", currency="EUR")]
        convert(items, "ILS", {})
        assert items[0].report_amount == pytest.approx(100)
        assert "EUR" in caplog.text

    def test_currency_census(self):
        items = [
            Transaction(date=date(2026, 3, 1), amount=1, description="A", currency="ILS"),
            Transaction(date=date(2026, 3, 1), amount=1, description="B", currency="USD"),
            Transaction(date=date(2026, 3, 2), amount=1, description="C", currency="ILS"),
        ]
        assert currencies(items) == {"ILS": 2, "USD": 1}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class TestRestClient:
    @pytest.fixture
    def config(self):
        return RestConfig(
            base_url="https://api.example.test",
            transactions_path="/v1/transactions",
            page_size=2,
            fields={"records_path": "data"},
        )

    def test_requires_token(self, config, monkeypatch):
        monkeypatch.delenv("EXPENSES_API_TOKEN", raising=False)
        with pytest.raises(RestError, match="токен"):
            RestClient(config)

    def test_requires_base_url(self):
        with pytest.raises(RestError, match="base_url"):
            RestClient(RestConfig())

    def test_paginates_until_short_page(self, config, monkeypatch):
        monkeypatch.setenv("EXPENSES_API_TOKEN", "secret")
        pages = [
            {"data": [_rec("1"), _rec("2")]},
            {"data": [_rec("3")]},
        ]
        client = RestClient(config)
        calls: list[dict] = []

        def fake_request(method, url, **kwargs):
            calls.append(kwargs.get("params") or {})
            return FakeResponse(pages[len(calls) - 1])

        monkeypatch.setattr(client.session, "request", fake_request)
        items = client.fetch(date(2026, 3, 1), date(2026, 3, 31))

        assert [t.source_id for t in items] == ["1", "2", "3"]
        assert calls[0]["page"] == 1 and calls[1]["page"] == 2
        assert calls[0]["from"] == "2026-03-01" and calls[0]["to"] == "2026-03-31"

    def test_sends_bearer_token(self, config, monkeypatch):
        monkeypatch.setenv("EXPENSES_API_TOKEN", "secret")
        assert RestClient(config).session.headers["Authorization"] == "Bearer secret"

    def test_auth_failure_is_explained(self, config, monkeypatch):
        monkeypatch.setenv("EXPENSES_API_TOKEN", "secret")
        client = RestClient(config)
        monkeypatch.setattr(
            client.session, "request", lambda *a, **k: FakeResponse({"error": "no"}, 401)
        )
        with pytest.raises(RestError, match="авторизацию"):
            client.fetch()

    def test_cursor_pagination(self, monkeypatch):
        monkeypatch.setenv("EXPENSES_API_TOKEN", "secret")
        config = RestConfig(
            base_url="https://api.example.test",
            pagination="cursor",
            cursor_path="meta.next",
            fields={"records_path": "data"},
        )
        client = RestClient(config)
        pages = [
            {"data": [_rec("1")], "meta": {"next": "abc"}},
            {"data": [_rec("2")], "meta": {"next": None}},
        ]
        seen: list[dict] = []

        def fake_request(method, url, **kwargs):
            seen.append(kwargs.get("params") or {})
            return FakeResponse(pages[len(seen) - 1])

        monkeypatch.setattr(client.session, "request", fake_request)
        assert [t.source_id for t in client.fetch()] == ["1", "2"]
        assert seen[1]["cursor"] == "abc"

    def test_probe_shows_fields(self, config, monkeypatch):
        monkeypatch.setenv("EXPENSES_API_TOKEN", "secret")
        client = RestClient(config)
        monkeypatch.setattr(
            client.session, "request", lambda *a, **k: FakeResponse({"data": [_rec("1")]})
        )
        output = client.probe()
        assert "description" in output and "amount" in output


def _rec(tx_id: str) -> dict:
    return {
        "id": tx_id,
        "date": "2026-03-15",
        "amount": -100,
        "description": "SHUFERSAL",
        "currency": "ILS",
    }


class TestReports:
    @pytest.fixture
    def items(self):
        from expenses.categories import Categorizer

        raw = [
            Transaction(date=date(2026, 2, 5), amount=6000, description="שכר דירה", source="t"),
            Transaction(date=date(2026, 3, 5), amount=6000, description="שכר דירה", source="t"),
            Transaction(date=date(2026, 3, 7), amount=54.9, description="NETFLIX.COM", source="t"),
        ]
        return Categorizer().categorize_all(raw)

    def test_text_report_has_sections(self, items):
        text = render_text(items)
        assert "ПО МЕСЯЦАМ" in text and "ПО КАТЕГОРИЯМ" in text and "₪" in text

    def test_text_report_on_empty_input(self):
        assert "Нет операций" in render_text([])

    def test_markdown_is_a_table(self, items):
        md = render_markdown(items)
        assert md.startswith("# Расходы") and "| 2026-03 |" in md

    def test_csv_has_header_and_rows(self, items):
        rows = render_csv(items).strip().splitlines()
        assert rows[0].startswith("date,month,amount")
        assert len(rows) == 4

    def test_json_is_machine_readable(self, items):
        payload = json.loads(render_json(items))
        assert payload["totals"]["months"] == 2
        assert payload["months"][0]["month"] == "2026-02"


class TestConfig:
    def test_env_file_parsing(self):
        parsed = parse_env_file('# comment\nexport A=1\nB="два"\nBAD\n')
        assert parsed == {"A": "1", "B": "два"}

    def test_defaults_are_available_without_file(self):
        config = Config()
        assert config.currency == "ILS"
        assert config.source_enabled("bybit") is False

    def test_fx_rates_are_normalised(self):
        config = Config()
        config.data["fx"]["rates"] = {"usd": "3.7", "eur": "плохо"}
        assert config.fx_rates == {"USD": 3.7}


class TestCli:
    def test_import_then_report(self, tmp_path, capsys):
        statement = tmp_path / "s.csv"
        statement.write_text(CSV_ENGLISH, encoding="utf-8")
        data = tmp_path / "data.jsonl"

        assert main(["--data", str(data), "import", str(statement)]) == 0
        assert "Добавлено новых: 2" in capsys.readouterr().out

        assert main(["--data", str(data), "report", "--months", "12"]) == 0
        out = capsys.readouterr().out
        assert "Продукты" in out and "РАСХОДЫ" in out

    def test_import_is_idempotent(self, tmp_path, capsys):
        statement = tmp_path / "s.csv"
        statement.write_text(CSV_ENGLISH, encoding="utf-8")
        data = tmp_path / "data.jsonl"

        main(["--data", str(data), "import", str(statement)])
        capsys.readouterr()
        main(["--data", str(data), "import", str(statement)])
        assert "Добавлено новых: 0" in capsys.readouterr().out

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        statement = tmp_path / "s.csv"
        statement.write_text(CSV_ENGLISH, encoding="utf-8")
        data = tmp_path / "data.jsonl"

        main(["--data", str(data), "import", str(statement), "--dry-run"])
        assert not data.exists()

    def test_report_on_empty_storage_suggests_next_step(self, tmp_path, capsys):
        assert main(["--data", str(tmp_path / "нет.jsonl"), "report"]) == 0
        assert "expenses import" in capsys.readouterr().out

    def test_test_rule_command(self, capsys):
        assert main(["test-rule", "SHUFERSAL DEAL 4821"]) == 0
        assert "Продукты" in capsys.readouterr().out

    def test_demo_runs_without_storage(self, capsys):
        assert main(["demo", "--months", "3"]) == 0
        assert "РАСХОДЫ" in capsys.readouterr().out

    def test_fetch_without_config_is_a_clear_error(self, tmp_path, capsys):
        assert main(["--data", str(tmp_path / "d.jsonl"), "fetch"]) == 2
        assert "bybit" in capsys.readouterr().err

    def test_report_json_to_file(self, tmp_path, capsys):
        statement = tmp_path / "s.csv"
        statement.write_text(CSV_ENGLISH, encoding="utf-8")
        data = tmp_path / "data.jsonl"
        out = tmp_path / "report.json"

        main(["--data", str(data), "import", str(statement)])
        main(["--data", str(data), "report", "--months", "12", "--format", "json", "--out", str(out)])
        assert json.loads(out.read_text("utf-8"))["currency"] == "ILS"

    def test_unknown_command_prints_help(self, capsys):
        assert main([]) == 0
        assert "usage" in capsys.readouterr().out
