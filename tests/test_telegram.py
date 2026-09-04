"""Подключение к Telegram: .env, диагностика, отправка файла."""

import pytest

from landtender.config import load_config, parse_env_file
from landtender.notify.telegram import TelegramError, TelegramNotifier


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("не JSON")
        return self._payload


@pytest.fixture
def notifier():
    return TelegramNotifier(bot_token="123:AA", chat_id="-1001")


class TestParseEnvFile:
    def test_reads_simple_pairs(self):
        assert parse_env_file("A=1\nB=2") == {"A": "1", "B": "2"}

    def test_skips_comments_and_blank_lines(self):
        assert parse_env_file("# коммент\n\nA=1") == {"A": "1"}

    def test_strips_export_prefix(self):
        assert parse_env_file("export TOKEN=abc") == {"TOKEN": "abc"}

    def test_strips_surrounding_quotes(self):
        assert parse_env_file('TOKEN="123:AA"') == {"TOKEN": "123:AA"}

    def test_keeps_colons_and_dashes_in_value(self):
        parsed = parse_env_file("TELEGRAM_CHAT_ID=-1001234567890\nTELEGRAM_BOT_TOKEN=123:AA-BB")
        assert parsed["TELEGRAM_CHAT_ID"] == "-1001234567890"
        assert parsed["TELEGRAM_BOT_TOKEN"] == "123:AA-BB"

    def test_ignores_lines_without_equals(self):
        assert parse_env_file("мусор\nA=1") == {"A": "1"}


class TestEnvFileLoading:
    def test_config_picks_up_env_file_next_to_it(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=из-файла\n", encoding="utf-8")
        (tmp_path / "landtender.toml").write_text("[general]\n", encoding="utf-8")

        config = load_config(tmp_path / "landtender.toml")
        assert config.get("telegram", "bot_token") == "из-файла"

    def test_real_environment_wins_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "из-окружения")
        (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=из-файла\n", encoding="utf-8")
        (tmp_path / "landtender.toml").write_text("[general]\n", encoding="utf-8")

        config = load_config(tmp_path / "landtender.toml")
        assert config.get("telegram", "bot_token") == "из-окружения"

    def test_missing_env_file_is_not_an_error(self, tmp_path):
        (tmp_path / "landtender.toml").write_text("[general]\n", encoding="utf-8")
        assert load_config(tmp_path / "landtender.toml").threshold_usd == 1_000_000.0


class TestCredentials:
    def test_empty_token_is_rejected_early(self):
        with pytest.raises(TelegramError, match="bot_token"):
            TelegramNotifier(bot_token="", chat_id="-1001")

    def test_empty_chat_id_is_rejected_early(self):
        with pytest.raises(TelegramError, match="chat_id"):
            TelegramNotifier(bot_token="123:AA", chat_id="")


class TestDiagnostics:
    def test_get_me_returns_bot_profile(self, notifier, monkeypatch):
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **k: FakeResponse({"ok": True, "result": {"username": "land_bot"}}),
        )
        assert notifier.get_me()["username"] == "land_bot"

    def test_bad_token_raises_with_telegram_description(self, notifier, monkeypatch):
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **k: FakeResponse({"ok": False, "description": "Unauthorized"}, 401),
        )
        with pytest.raises(TelegramError, match="Unauthorized"):
            notifier.get_me()

    def test_get_chat_passes_chat_id(self, notifier, monkeypatch):
        captured = {}

        def fake_post(url, json=None, **kwargs):
            captured.update(json or {})
            return FakeResponse({"ok": True, "result": {"id": -1001, "title": "Земля"}})

        monkeypatch.setattr("requests.post", fake_post)
        assert notifier.get_chat()["title"] == "Земля"
        assert captured["chat_id"] == "-1001"

    def test_public_channel_username_is_passed_through_unchanged(self, monkeypatch):
        """Telegram принимает @имя вместо числового id — не ломаем его."""
        captured = {}

        def fake_post(url, json=None, **kwargs):
            captured.update(json or {})
            return FakeResponse({"ok": True, "result": {"id": -1001, "title": "Земля"}})

        monkeypatch.setattr("requests.post", fake_post)
        TelegramNotifier(bot_token="123:AA", chat_id="@my_land_channel").get_chat()
        assert captured["chat_id"] == "@my_land_channel"

    def test_discover_collects_channels_and_groups(self, notifier, monkeypatch):
        updates = {
            "ok": True,
            "result": [
                {"channel_post": {"chat": {"id": -1001, "title": "Тендеры", "type": "channel"}}},
                {"message": {"chat": {"id": 42, "first_name": "Вадим", "type": "private"}}},
                {"channel_post": {"chat": {"id": -1001, "title": "Тендеры", "type": "channel"}}},
            ],
        }
        monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse(updates))

        chats = notifier.discover_chats()
        assert len(chats) == 2  # дубль канала схлопнут
        assert {c["chat_id"] for c in chats} == {-1001, 42}

    def test_discover_on_empty_updates(self, notifier, monkeypatch):
        monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse({"ok": True, "result": []}))
        assert notifier.discover_chats() == []

    def test_non_json_response_is_wrapped(self, notifier, monkeypatch):
        monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse(None, 502, "<html>"))
        with pytest.raises(TelegramError, match="не JSON"):
            notifier.get_me()


class TestSendDocument:
    def test_sends_file_with_caption(self, notifier, tmp_path, monkeypatch):
        captured = {}

        def fake_post(url, data=None, files=None, **kwargs):
            captured["url"] = url
            captured["data"] = data
            captured["filename"] = files["document"][0]
            return FakeResponse({"ok": True}, 200)

        monkeypatch.setattr("requests.post", fake_post)
        path = tmp_path / "lots.csv"
        path.write_text("uid,price_usd\n", encoding="utf-8")

        notifier.send_document(path, caption="Новые лоты")

        assert "sendDocument" in captured["url"]
        assert captured["data"]["chat_id"] == "-1001"
        assert captured["data"]["caption"] == "Новые лоты"
        assert captured["filename"] == "lots.csv"

    def test_missing_file_raises(self, notifier, tmp_path):
        with pytest.raises(TelegramError, match="Не удалось прочитать"):
            notifier.send_document(tmp_path / "нет.csv")

    def test_http_error_raises(self, notifier, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "requests.post", lambda *a, **k: FakeResponse({"ok": False}, 413, "too big")
        )
        path = tmp_path / "lots.csv"
        path.write_text("x", encoding="utf-8")
        with pytest.raises(TelegramError, match="413"):
            notifier.send_document(path)

    def test_long_caption_is_truncated(self, notifier, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "requests.post",
            lambda url, data=None, files=None, **k: (captured.update(data), FakeResponse({"ok": True}))[1],
        )
        path = tmp_path / "lots.csv"
        path.write_text("x", encoding="utf-8")

        notifier.send_document(path, caption="я" * 2000)
        assert len(captured["caption"]) == 1024


class TestSendPacing:
    """Темп отправки: длинная выгрузка не должна упираться в лимит частоты.

    Полная выгрузка по всей базе — это около ста двадцати сообщений, а
    Telegram пускает в один канал примерно двадцать в минуту. На прежнем
    темпе в полсекунды всё после первых двадцати отбивалось бы по 429, и
    выгрузка обрывалась бы на середине.
    """

    def send(self, notifier, monkeypatch, count):
        delays = []
        monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse({"ok": True}, 200))
        monkeypatch.setattr("time.sleep", lambda seconds: delays.append(seconds))
        sent = notifier.send_blocks([f"блок {i}" * 400 for i in range(count)])
        return sent, delays

    def test_a_short_digest_goes_out_fast(self, notifier, monkeypatch):
        from landtender.notify.telegram import FAST_DELAY_SEC

        sent, delays = self.send(notifier, monkeypatch, 3)
        assert sent == 3
        assert set(delays) == {FAST_DELAY_SEC}

    def test_a_long_export_slows_to_the_channel_limit(self, notifier, monkeypatch):
        from landtender.notify.telegram import SLOW_DELAY_SEC

        sent, delays = self.send(notifier, monkeypatch, 40)
        assert sent == 40
        assert set(delays) == {SLOW_DELAY_SEC}

    def test_no_pause_after_the_last_message(self, notifier, monkeypatch):
        """Пауза нужна между сообщениями, а не в конце: это чистое ожидание."""
        sent, delays = self.send(notifier, monkeypatch, 3)
        assert len(delays) == sent - 1

    def test_rate_limit_is_retried_more_than_a_few_times(self, notifier, monkeypatch):
        """Очередь из сотни сообщений переживает больше отказов, чем сводка из трёх."""
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 4:
                return FakeResponse({"parameters": {"retry_after": 1}}, 429)
            return FakeResponse({"ok": True}, 200)

        monkeypatch.setattr("requests.post", flaky)
        monkeypatch.setattr("time.sleep", lambda seconds: None)
        assert notifier.send_blocks(["один блок"]) == 1
