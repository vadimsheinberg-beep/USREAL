"""Командная строка: коды возврата и выгрузки."""

import json

import pytest

from landtender import cli, pipeline
from landtender.models import Lot, RunResult, SourceReport
from landtender.money import FxRate
from landtender.sources.base import Source


class OneLotSource(Source):
    name = "fake"
    title = "тестовый источник"

    def fetch(self):
        return [Lot(source="fake", source_id="1", tender_name="חי/142", units=60, price_nis=18_500_000.0)]


class BrokenSource(Source):
    name = "broken"
    title = "падающий источник"

    def fetch(self):
        raise RuntimeError("портал недоступен")


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "landtender.toml"
    path.write_text(
        f'[general]\ndb_path = "{tmp_path / "cli.sqlite3"}"\n'
        "[sources.fake]\nenabled = true\n"
        "[telegram]\nenabled = false\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def wire_fakes(monkeypatch):
    monkeypatch.setattr(pipeline, "SOURCES_BY_NAME", {"fake": OneLotSource, "broken": BrokenSource})
    monkeypatch.setattr(cli, "SOURCES_BY_NAME", {"fake": OneLotSource, "broken": BrokenSource})
    monkeypatch.setattr(pipeline, "get_fx", lambda *a, **k: FxRate(3.6412, "2026-07-27", "test"))
    monkeypatch.setattr(pipeline, "build_http", lambda config: object())


class TestRun:
    def test_successful_run_returns_zero(self, config_file, capsys):
        code = cli.main(["--config", str(config_file), "run", "--sources", "fake", "--no-notify"])
        assert code == 0
        assert "ЗЕМЕЛЬНЫЕ ТЕНДЕРЫ ИЗРАИЛЯ" in capsys.readouterr().out

    def test_all_sources_failing_returns_one(self, config_file):
        code = cli.main(["--config", str(config_file), "run", "--sources", "broken", "--no-notify"])
        assert code == 1

    def test_threshold_override_is_applied(self, config_file, capsys):
        cli.main(
            ["--config", str(config_file), "run", "--sources", "fake",
             "--threshold-usd", "10000000", "--no-notify"]
        )
        assert "Дешевле порога" in capsys.readouterr().out

    def test_export_csv_flag_writes_file(self, config_file, tmp_path):
        out = tmp_path / "new.csv"
        cli.main(
            ["--config", str(config_file), "run", "--sources", "fake", "--no-notify",
             "--export-csv", str(out)]
        )
        assert out.exists()
        assert "price_usd" in out.read_text("utf-8-sig")


class TestExportCommand:
    def test_exports_accumulated_lots_to_json(self, config_file, tmp_path):
        cli.main(["--config", str(config_file), "run", "--sources", "fake", "--no-notify"])
        out = tmp_path / "all.json"
        code = cli.main(["--config", str(config_file), "export", "--format", "json", "--out", str(out)])

        assert code == 0
        rows = json.loads(out.read_text("utf-8"))
        assert rows[0]["units"] == 60
        assert rows[0]["tier"] == "premium"

    def test_tier_filter(self, config_file, tmp_path):
        cli.main(["--config", str(config_file), "run", "--sources", "fake", "--no-notify"])
        out = tmp_path / "standard.json"
        cli.main(
            ["--config", str(config_file), "export", "--format", "json",
             "--tier", "standard", "--out", str(out)]
        )
        assert json.loads(out.read_text("utf-8")) == []


class TestCheckCommand:
    def test_reports_ok_and_failed_sources(self, config_file, capsys, monkeypatch):
        code = cli.main(["--config", str(config_file), "check", "--sources", "fake", "broken"])
        out = capsys.readouterr().out
        assert "[ ok ] fake" in out
        assert "[FAIL] broken" in out
        assert code == 1


class TestTelegramTestCommand:
    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    def test_missing_token_explains_what_to_set(self, config_file, capsys):
        assert cli.main(["--config", str(config_file), "telegram-test"]) == 2
        assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().out

    def test_missing_chat_id_points_to_discover(self, config_file, capsys, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:AA")
        assert cli.main(["--config", str(config_file), "telegram-test"]) == 2
        assert "--discover" in capsys.readouterr().out

    def test_happy_path_checks_token_channel_and_sends(self, config_file, capsys, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:AA")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001")

        class FakeNotifier:
            def __init__(self, **kwargs):
                pass

            def get_me(self):
                return {"username": "land_bot", "first_name": "Land"}

            def get_chat(self):
                return {"id": -1001, "title": "Тендеры"}

            def send_blocks(self, blocks):
                return 1

        monkeypatch.setattr("landtender.notify.TelegramNotifier", FakeNotifier)

        assert cli.main(["--config", str(config_file), "telegram-test"]) == 0
        out = capsys.readouterr().out
        assert "@land_bot" in out
        assert "Тендеры" in out
        assert "Пробное сообщение отправлено" in out

    def test_bad_token_returns_one(self, config_file, capsys, monkeypatch):
        from landtender.notify import TelegramError

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:AA")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001")

        class FakeNotifier:
            def __init__(self, **kwargs):
                pass

            def get_me(self):
                raise TelegramError("Unauthorized")

        monkeypatch.setattr("landtender.notify.TelegramNotifier", FakeNotifier)

        assert cli.main(["--config", str(config_file), "telegram-test"]) == 1
        assert "Токен отклонён" in capsys.readouterr().out

    def test_discover_lists_chat_ids(self, config_file, capsys, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:AA")

        class FakeNotifier:
            def __init__(self, **kwargs):
                pass

            def discover_chats(self):
                return [{"chat_id": -1001, "title": "Тендеры", "type": "channel"}]

        monkeypatch.setattr("landtender.notify.TelegramNotifier", FakeNotifier)

        assert cli.main(["--config", str(config_file), "telegram-test", "--discover"]) == 0
        assert "-1001" in capsys.readouterr().out

    def test_no_send_skips_the_test_message(self, config_file, capsys, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:AA")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001")
        sent = []

        class FakeNotifier:
            def __init__(self, **kwargs):
                pass

            def get_me(self):
                return {"username": "land_bot"}

            def get_chat(self):
                return {"id": -1001, "title": "Тендеры"}

            def send_blocks(self, blocks):
                sent.append(blocks)
                return 1

        monkeypatch.setattr("landtender.notify.TelegramNotifier", FakeNotifier)

        assert cli.main(["--config", str(config_file), "telegram-test", "--no-send"]) == 0
        assert sent == []


class FarmSource(Source):
    name = "fake"
    title = "тестовый источник"

    def fetch(self):
        return [
            Lot(source="fake", source_id="1", tender_name="מכרז חקלאי",
                purpose="חקלאות", area_sqm=145_000.0, price_nis=2_900_000.0,
                closing_date="2099-01-01"),
            Lot(source="fake", source_id="2", tender_name="מכרז מגורים",
                purpose="מגורים", price_nis=18_500_000.0, closing_date="2099-01-01"),
        ]


class TestFarmlandCommand:
    """«Покажи всю сельхозземлю» — срез базы, а не дневная сводка."""

    @pytest.fixture(autouse=True)
    def farm_source(self, monkeypatch):
        monkeypatch.setattr(pipeline, "SOURCES_BY_NAME", {"fake": FarmSource})
        monkeypatch.setattr(cli, "SOURCES_BY_NAME", {"fake": FarmSource})

    def fill(self, config_file):
        cli.main(["--config", str(config_file), "run", "--sources", "fake", "--no-notify"])

    def test_lists_only_agricultural_lots(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        assert cli.main(["--config", str(config_file), "farmland"]) == 0
        out = capsys.readouterr().out
        assert "Лотов: 1" in out
        assert "מכרז חקלאי" in out
        assert "מכרז מגורים" not in out

    def test_reports_total_area(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        cli.main(["--config", str(config_file), "farmland"])
        assert "площадь: 14.5 га" in capsys.readouterr().out

    def test_empty_database_says_nothing_found(self, config_file, capsys):
        assert cli.main(["--config", str(config_file), "farmland"]) == 0
        assert "Ничего не найдено" in capsys.readouterr().out

    def test_csv_export(self, config_file, tmp_path, capsys):
        self.fill(config_file)
        out = tmp_path / "farm.csv"
        cli.main(["--config", str(config_file), "farmland", "--out", str(out)])
        text = out.read_text("utf-8")
        assert "agriculture" in text
        assert "מכרז מגורים" not in text


def test_stats_command_reports_empty_database(config_file, capsys):
    assert cli.main(["--config", str(config_file), "stats"]) == 0
    assert "Запусков ещё не было" in capsys.readouterr().out


def test_help_is_available():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
