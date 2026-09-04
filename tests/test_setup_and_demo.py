"""Мастер настройки и демонстрационная сводка."""

import stat

import pytest

from landtender import cli, setup_wizard
from landtender.demo import DEMO_WARNING, demo_blocks, demo_result
from landtender.models import TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN
from landtender.notify import TelegramError
from landtender.notify.telegram import chunk_blocks
from landtender.setup_wizard import run_wizard, write_env_file


class FakeNotifier:
    """Подменяет TelegramNotifier: настраиваемые ответы и запись отправленного."""

    me = {"username": "my_land_bot", "first_name": "Land Tenders"}
    chat = {"id": -1001234567890, "title": "Земельные тендеры"}
    chats = [{"chat_id": -1001234567890, "title": "Земельные тендеры", "type": "channel"}]
    bad_tokens: set[str] = set()
    bad_chats: set[str] = set()
    sent: list[list[str]] = []

    def __init__(self, bot_token, chat_id, **kwargs):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def get_me(self):
        if self.bot_token in self.bad_tokens:
            raise TelegramError("Unauthorized")
        return self.me

    def get_chat(self):
        if self.chat_id in self.bad_chats:
            raise TelegramError("chat not found")
        return self.chat

    def discover_chats(self):
        return self.chats

    def send_blocks(self, blocks):
        FakeNotifier.sent.append(list(blocks))
        return 1


@pytest.fixture(autouse=True)
def fake_telegram(monkeypatch):
    FakeNotifier.bad_tokens = set()
    FakeNotifier.bad_chats = set()
    FakeNotifier.sent = []
    FakeNotifier.chats = [
        {"chat_id": -1001234567890, "title": "Земельные тендеры", "type": "channel"}
    ]
    monkeypatch.setattr(setup_wizard, "TelegramNotifier", FakeNotifier)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return FakeNotifier


def scripted(answers):
    """Возвращает функцию ввода, отдающую заготовленные ответы по очереди."""
    queue = list(answers)

    def ask(_prompt):
        return queue.pop(0) if queue else ""

    return ask


# --------------------------------------------------------------- демо -------


class TestDemo:
    def test_covers_all_three_tiers(self):
        tiers = {lot.tier for lot in demo_result().new_lots}
        assert tiers == {TIER_PREMIUM, TIER_STANDARD, TIER_UNKNOWN}

    def test_is_clearly_marked_as_fictional(self):
        assert demo_blocks()[0] == DEMO_WARNING
        assert "вымышлены" in DEMO_WARNING

    def test_shows_units_and_prices(self):
        text = "\n".join(demo_blocks())
        assert "единиц: 60" in text
        assert "$5.08 млн" in text

    def test_includes_a_failed_source_example(self):
        text = "\n".join(demo_blocks())
        assert "Источники с ошибкой" in text
        assert "yad2" in text

    def test_includes_changes_section(self):
        assert "Изменения по ранее найденным" in "\n".join(demo_blocks())

    def test_shows_a_reconstruction_lot(self):
        text = "\n".join(demo_blocks())
        assert "🏚" in text
        assert "פינוי בינוי" in text
        assert "застройка 11 200 м²" in text

    def test_fits_into_telegram_messages(self):
        assert all(len(m) <= 4096 for m in chunk_blocks(demo_blocks()))

    def test_threshold_moves_lots_between_tiers(self):
        text = "\n".join(demo_blocks(threshold_usd=10_000_000))
        assert "🔥" not in text  # при таком пороге дорогих лотов нет


class TestDemoCommand:
    def test_dry_run_prints_actual_messages(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert cli.main(["demo", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "───── сообщение 1/" in out
        assert "Демонстрационная сводка" in out

    def test_without_credentials_points_to_setup(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert cli.main(["demo"]) == 2
        assert "landtender setup" in capsys.readouterr().out


# ------------------------------------------------------------- мастер -------


class TestWriteEnvFile:
    def test_writes_both_values(self, tmp_path):
        path = tmp_path / ".env"
        write_env_file(path, "123:AA", "@channel")
        text = path.read_text("utf-8")
        assert "TELEGRAM_BOT_TOKEN=123:AA" in text
        assert "TELEGRAM_CHAT_ID=@channel" in text

    def test_file_is_not_world_readable(self, tmp_path):
        path = tmp_path / ".env"
        write_env_file(path, "123:AA", "@channel")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0


class TestWizard:
    def test_happy_path_writes_env_and_sends_demo(self, tmp_path, capsys):
        env = tmp_path / ".env"
        code = run_wizard(env, ask=scripted(["123:AA", "@my_channel"]), say=lambda s: print(s))

        assert code == 0
        assert "TELEGRAM_CHAT_ID=@my_channel" in env.read_text("utf-8")
        assert len(FakeNotifier.sent) == 1
        assert FakeNotifier.sent[0][0] == DEMO_WARNING
        assert "my_land_bot" in capsys.readouterr().out

    def test_bad_token_is_retried_then_accepted(self, tmp_path, capsys):
        FakeNotifier.bad_tokens = {"плохой"}
        env = tmp_path / ".env"
        code = run_wizard(env, ask=scripted(["плохой", "123:AA", "@my_channel"]), say=lambda s: print(s))

        assert code == 0
        assert "Токен не принят" in capsys.readouterr().out

    def test_gives_up_after_three_bad_tokens(self, tmp_path):
        FakeNotifier.bad_tokens = {"нет"}
        env = tmp_path / ".env"
        assert run_wizard(env, ask=scripted(["нет", "нет", "нет"]), say=lambda s: None) == 1
        assert not env.exists()

    def test_inaccessible_channel_is_retried(self, tmp_path, capsys):
        FakeNotifier.bad_chats = {"@чужой"}
        env = tmp_path / ".env"
        code = run_wizard(env, ask=scripted(["123:AA", "@чужой", "@мой"]), say=lambda s: print(s))

        assert code == 0
        out = capsys.readouterr().out
        assert "Канал недоступен" in out
        assert "администратором" in out

    def test_discover_flow_finds_private_channel_id(self, tmp_path):
        env = tmp_path / ".env"
        code = run_wizard(
            env,
            ask=scripted(["123:AA", "?", "", "-1001234567890"]),
            say=lambda s: None,
        )
        assert code == 0
        assert "TELEGRAM_CHAT_ID=-1001234567890" in env.read_text("utf-8")

    def test_discover_without_results_is_survivable(self, tmp_path, capsys):
        FakeNotifier.chats = []
        env = tmp_path / ".env"
        code = run_wizard(env, ask=scripted(["123:AA", "?", "", "@запасной"]), say=lambda s: print(s))

        assert code == 0
        assert "Ничего не найдено" in capsys.readouterr().out

    def test_existing_env_is_not_overwritten_without_consent(self, tmp_path, capsys):
        env = tmp_path / ".env"
        env.write_text("СТАРОЕ=значение\n", encoding="utf-8")

        code = run_wizard(env, ask=scripted(["n"]), say=lambda s: print(s))

        assert code == 1
        assert env.read_text("utf-8") == "СТАРОЕ=значение\n"
        assert "не тронут" in capsys.readouterr().out

    def test_existing_env_is_overwritten_after_confirmation(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("СТАРОЕ=значение\n", encoding="utf-8")

        code = run_wizard(env, ask=scripted(["y", "123:AA", "@my_channel"]), say=lambda s: None)

        assert code == 0
        assert "СТАРОЕ" not in env.read_text("utf-8")

    def test_interrupted_input_exits_without_traceback(self, tmp_path, capsys):
        def ask_eof(_prompt):
            raise EOFError

        env = tmp_path / ".env"
        assert run_wizard(env, ask=ask_eof, say=lambda s: print(s)) == 1
        assert "прервана" in capsys.readouterr().out
        assert not env.exists()

    def test_ctrl_c_exits_cleanly(self, tmp_path):
        def ask_interrupt(_prompt):
            raise KeyboardInterrupt

        env = tmp_path / ".env"
        assert run_wizard(env, ask=ask_interrupt, say=lambda s: None) == 1
        assert not env.exists()

    def test_demo_can_be_skipped(self, tmp_path):
        env = tmp_path / ".env"
        code = run_wizard(
            env, ask=scripted(["123:AA", "@my_channel"]), say=lambda s: None, send_demo=False
        )
        assert code == 0
        assert FakeNotifier.sent == []

    def test_environment_values_are_offered_as_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "из-окружения")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "@из-окружения")
        env = tmp_path / ".env"

        # Пользователь дважды жмёт Enter — берутся значения из окружения
        code = run_wizard(env, ask=scripted(["", ""]), say=lambda s: None)

        assert code == 0
        assert "TELEGRAM_BOT_TOKEN=из-окружения" in env.read_text("utf-8")
        assert "TELEGRAM_CHAT_ID=@из-окружения" in env.read_text("utf-8")


class TestSetupCommand:
    def test_writes_env_to_requested_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("builtins.input", scripted(["123:AA", "@my_channel"]))
        target = tmp_path / "custom.env"

        assert cli.main(["setup", "--env-file", str(target), "--no-demo"]) == 0
        assert target.exists()
