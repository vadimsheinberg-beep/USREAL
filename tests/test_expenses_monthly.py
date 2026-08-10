"""Месячный запуск по расписанию: сбор, сводка, отправка в Telegram."""

import json
from datetime import date
from pathlib import Path

import pytest

from expenses.categories import Categorizer
from expenses.cli import main
from expenses.models import DIRECTION_INCOME, Transaction
from expenses.notify import TelegramError, TelegramNotifier, build_summary, month_name

CONFIG = """
[general]
currency = "ILS"
data_path = "data/expenses.jsonl"
inbox_path = "inbox"
months = 3

[telegram]
bot_token = "env:TELEGRAM_BOT_TOKEN"
chat_id = "env:TELEGRAM_CHAT_ID"
"""


def tx(day: str, amount: float, description: str, **kwargs) -> Transaction:
    return Transaction(
        date=date.fromisoformat(day), amount=amount, description=description, source="t", **kwargs
    )


@pytest.fixture
def items():
    raw = []
    for month in ("01", "02", "03"):
        raw.append(tx(f"2026-{month}-05", 6000, "שכר דירה"))
        raw.append(tx(f"2026-{month}-07", 54.9, "NETFLIX.COM"))
        raw.append(tx(f"2026-{month}-10", 20000, "SALARY", direction=DIRECTION_INCOME))
    raw.append(tx("2026-03-12", 900, "SHUFERSAL DEAL"))
    raw.append(tx("2026-02-12", 300, "SHUFERSAL DEAL"))
    return Categorizer().categorize_all(sorted(raw, key=lambda t: t.date))


class TestSummary:
    def test_names_the_last_month(self, items):
        assert "март 2026" in build_summary(items)

    def test_shows_total_and_categories(self, items):
        text = build_summary(items)
        assert "Жильё" in text and "Подписки" in text and "₪" in text

    def test_shows_change_against_previous_month(self, items):
        # Продукты выросли с 300 до 900 — это должно быть видно.
        text = build_summary(items)
        assert "февраль 2026" in text and "Выросло к прошлому месяцу" in text

    def test_lists_recurring(self, items):
        assert "Регулярных списаний" in build_summary(items)

    def test_fits_telegram_limit(self, items):
        assert len(build_summary(items)) <= 3900

    def test_handles_empty_input(self):
        assert "не нашлось" in build_summary([])

    def test_month_name(self):
        assert month_name("2026-08") == "август 2026"
        assert month_name("мусор") == "мусор"


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class TestNotifier:
    def test_requires_token_and_chat(self):
        with pytest.raises(TelegramError, match="TELEGRAM_BOT_TOKEN"):
            TelegramNotifier("", "123")

    def test_sends_message_with_html(self, monkeypatch):
        sent = {}

        def fake_post(url, data=None, files=None, timeout=None):
            sent.update({"url": url, "data": data})
            return FakeResponse()

        monkeypatch.setattr("expenses.notify.requests.post", fake_post)
        TelegramNotifier("token", "-100").send_message("<b>Привет</b>")

        assert "sendMessage" in sent["url"]
        assert sent["data"]["parse_mode"] == "HTML"
        assert sent["data"]["chat_id"] == "-100"

    def test_sends_document(self, tmp_path, monkeypatch):
        report = tmp_path / "report.html"
        report.write_text("<html></html>", encoding="utf-8")
        seen = {}

        def fake_post(url, data=None, files=None, timeout=None):
            seen.update({"url": url, "files": files})
            return FakeResponse()

        monkeypatch.setattr("expenses.notify.requests.post", fake_post)
        TelegramNotifier("token", "-100").send_document(report, caption="Отчёт")

        assert "sendDocument" in seen["url"]
        assert seen["files"]["document"][0] == "report.html"

    def test_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(TelegramError, match="не найден"):
            TelegramNotifier("token", "-100").send_document(tmp_path / "нет.html")

    def test_api_error_is_explained(self, monkeypatch):
        monkeypatch.setattr(
            "expenses.notify.requests.post",
            lambda *a, **k: FakeResponse({"description": "chat not found"}, 400),
        )
        with pytest.raises(TelegramError, match="400"):
            TelegramNotifier("token", "-100").send_message("текст")


@pytest.fixture
def project(tmp_path, items):
    """Разложенный на диске проект: конфиг, хранилище, inbox."""
    (tmp_path / "expenses.toml").write_text(CONFIG, encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "expenses.jsonl").write_text(
        "\n".join(json.dumps(t.to_dict(), ensure_ascii=False) for t in items),
        encoding="utf-8",
    )
    (tmp_path / "inbox").mkdir()
    return tmp_path


class TestMonthlyCommand:
    def _run(self, project, extra, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
        code = main(["--config", str(project / "expenses.toml"), "monthly", *extra])
        return code, capsys.readouterr()

    def test_dry_run_sends_nothing(self, project, monkeypatch, capsys):
        def explode(*args, **kwargs):
            raise AssertionError("сеть не должна использоваться при --dry-run")

        monkeypatch.setattr("expenses.notify.requests.post", explode)
        code, out = self._run(project, ["--dry-run", "--no-fetch"], monkeypatch, capsys)

        assert code == 0
        assert "март 2026" in out.out
        assert "ничего не отправлено" in out.out

    def test_sends_summary_and_report(self, project, monkeypatch, capsys):
        calls = []

        def fake_post(url, data=None, files=None, timeout=None):
            calls.append(("document" if files else "message", data))
            return FakeResponse()

        monkeypatch.setattr("expenses.notify.requests.post", fake_post)
        code, out = self._run(project, ["--no-fetch"], monkeypatch, capsys)

        assert code == 0
        assert [kind for kind, _ in calls] == ["message", "document"]
        assert (project / "data" / "expenses-2026-03.html").exists()

    def test_picks_up_statements_from_inbox(self, project, monkeypatch, capsys):
        statement = project / "inbox" / "bank.csv"
        statement.write_text(
            "Date,Description,Amount\n2026-03-20,RAMI LEVY,-450.00\n", encoding="utf-8"
        )
        monkeypatch.setattr("expenses.notify.requests.post", lambda *a, **k: FakeResponse())
        code, _ = self._run(project, ["--no-fetch"], monkeypatch, capsys)

        assert code == 0
        # Файл разобран и убран в архив, чтобы не импортироваться повторно.
        assert not statement.exists()
        assert (project / "inbox" / "archive" / "bank.csv").exists()
        assert "RAMI LEVY" in (project / "data" / "expenses.jsonl").read_text("utf-8")

    def test_broken_statement_does_not_stop_the_run(self, project, monkeypatch, capsys):
        (project / "inbox" / "мусор.csv").write_text("не выписка\n", encoding="utf-8")
        monkeypatch.setattr("expenses.notify.requests.post", lambda *a, **k: FakeResponse())
        code, _ = self._run(project, ["--no-fetch"], monkeypatch, capsys)
        assert code == 0

    def test_api_failure_still_sends_stored_data(self, project, monkeypatch, capsys):
        """Свежесть данных важна, но отчёт по накопленному важнее молчания."""
        config = (project / "expenses.toml").read_text("utf-8")
        (project / "expenses.toml").write_text(
            config + '\n[sources.bybit]\nenabled = true\n', encoding="utf-8"
        )
        monkeypatch.delenv("BYBIT_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        monkeypatch.setattr("expenses.notify.requests.post", lambda *a, **k: FakeResponse())

        code, _ = self._run(project, [], monkeypatch, capsys)
        assert code == 0

    def test_telegram_failure_is_a_nonzero_exit(self, project, monkeypatch, capsys):
        # systemd должен увидеть провал, а не «успех» без доставки.
        monkeypatch.setattr(
            "expenses.notify.requests.post",
            lambda *a, **k: FakeResponse({"description": "forbidden"}, 403),
        )
        code, out = self._run(project, ["--no-fetch"], monkeypatch, capsys)
        assert code == 2
        assert "Telegram" in out.err

    def test_empty_storage_is_a_nonzero_exit(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "expenses.toml").write_text(CONFIG, encoding="utf-8")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
        code = main(["--config", str(tmp_path / "expenses.toml"), "monthly", "--no-fetch"])
        assert code == 1
        assert "пустое" in capsys.readouterr().err


class TestTelegramTest:
    def test_reports_bot_and_chat(self, project, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")

        def fake_get(url, params=None, timeout=None):
            if "getMe" in url:
                return FakeResponse({"ok": True, "result": {"username": "money_bot"}})
            return FakeResponse({"ok": True, "result": {"title": "Мои расходы"}})

        monkeypatch.setattr("expenses.notify.requests.get", fake_get)
        code = main(["--config", str(project / "expenses.toml"), "telegram-test"])

        assert code == 0
        out = capsys.readouterr().out
        assert "money_bot" in out and "Мои расходы" in out

    def test_bad_token_is_explained(self, project, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
        monkeypatch.setattr(
            "expenses.notify.requests.get",
            lambda *a, **k: FakeResponse({"ok": False, "description": "Unauthorized"}),
        )
        code = main(["--config", str(project / "expenses.toml"), "telegram-test"])
        assert code == 2
        assert "токен не принят" in capsys.readouterr().err


class TestDeployAssets:
    """Юниты и скрипт лежат в репозитории и согласованы между собой."""

    @pytest.fixture
    def deploy(self):
        return Path(__file__).resolve().parent.parent / "deploy"

    def test_units_exist(self, deploy):
        assert (deploy / "expenses.service").exists()
        assert (deploy / "expenses.timer").exists()

    def test_timer_fires_monthly(self, deploy):
        assert "OnCalendar=*-*-01" in (deploy / "expenses.timer").read_text("utf-8")

    def test_service_runs_the_monthly_command(self, deploy):
        unit = (deploy / "expenses.service").read_text("utf-8")
        assert "monthly" in unit
        assert "EnvironmentFile=/etc/expenses.env" in unit

    def test_install_script_is_executable(self, deploy):
        script = deploy / "install-expenses.sh"
        assert script.stat().st_mode & 0o111

    def test_env_example_has_no_real_secrets(self, deploy):
        text = (deploy / "expenses.env.example").read_text("utf-8")
        assert "BYBIT_API_KEY=\n" in text and "BYBIT_API_SECRET=\n" in text
