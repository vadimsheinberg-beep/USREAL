"""Конфигурация: значения по умолчанию, TOML, секреты из окружения."""

import pytest

from landtender.config import DEFAULTS, Config, _deep_merge, load_config, resolve_secret

SAMPLE = """
[general]
threshold_usd = 2_500_000
db_path = "custom/lots.sqlite3"

[sources.yad2]
enabled = false

[telegram]
bot_token = "env:TEST_TG_TOKEN"
"""


def write_config(tmp_path, text=SAMPLE):
    path = tmp_path / "landtender.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults:
    def test_threshold_defaults_to_one_million(self):
        assert Config().threshold_usd == 1_000_000.0

    def test_all_sources_enabled_by_default(self):
        config = Config()
        assert config.source_enabled("rmi_michrazim") is True
        assert config.source_enabled("yad2") is True

    def test_unknown_source_is_disabled(self):
        assert Config().source_enabled("несуществующий") is False


class TestLoading:
    def test_file_values_override_defaults(self, tmp_path):
        config = load_config(write_config(tmp_path))
        assert config.threshold_usd == 2_500_000.0

    def test_untouched_defaults_survive_merge(self, tmp_path):
        config = load_config(write_config(tmp_path))
        assert config.get("general", "lookback_days") == 30
        assert config.source_config("rmi_michrazim")["details_budget"] == 500

    def test_source_can_be_disabled(self, tmp_path):
        config = load_config(write_config(tmp_path))
        assert config.source_enabled("yad2") is False
        assert config.source_enabled("rmi_michrazim") is True

    def test_relative_db_path_resolves_next_to_config(self, tmp_path):
        config = load_config(write_config(tmp_path))
        assert config.db_path == tmp_path / "custom/lots.sqlite3"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "нет.toml")

    def test_no_config_falls_back_to_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert load_config().threshold_usd == 1_000_000.0


class TestSecrets:
    def test_env_prefix_is_resolved(self, monkeypatch):
        monkeypatch.setenv("TEST_TG_TOKEN", "секрет-123")
        assert resolve_secret("env:TEST_TG_TOKEN") == "секрет-123"

    def test_missing_env_var_becomes_none(self, monkeypatch):
        monkeypatch.delenv("TEST_TG_TOKEN", raising=False)
        assert resolve_secret("env:TEST_TG_TOKEN") is None

    def test_plain_values_pass_through(self):
        assert resolve_secret("обычная-строка") == "обычная-строка"

    def test_config_get_resolves_secrets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_TG_TOKEN", "abc:def")
        config = load_config(write_config(tmp_path))
        assert config.get("telegram", "bot_token") == "abc:def"


class TestDefaultsAreNotShared:
    """Конфиг не должен быть окном в общие умолчания.

    ``dict(base)`` копирует только верхний уровень, поэтому секции, которых
    нет в файле, оказывались теми же объектами, что в ``DEFAULTS``. Команды
    дописывают себе настройки на лету — ``harvest`` включает архив, ``top``
    оценку, — и такая запись меняла умолчания на весь процесс: следующий
    читатель конфига получал чужой выбор. В тестах это проявилось как
    падение непричастных проверок после запуска команды.
    """

    def test_editing_a_config_leaves_defaults_alone(self):
        config = Config()
        config.data.setdefault("valuation", {})["estimate"] = True
        assert DEFAULTS["valuation"].get("estimate") is not True

    def test_two_configs_do_not_share_sections(self):
        first, second = Config(), Config()
        first.data["general"]["threshold_usd"] = 42
        assert second.data["general"]["threshold_usd"] != 42

    def test_nested_source_sections_are_copied_too(self):
        config = Config()
        config.data["sources"]["rmi_michrazim"]["active_only"] = False
        assert DEFAULTS["sources"]["rmi_michrazim"].get("active_only") is not False

    def test_merging_still_applies_the_file(self):
        merged = _deep_merge(DEFAULTS, {"general": {"threshold_usd": 7}})
        assert merged["general"]["threshold_usd"] == 7
        # Остальные ключи секции переживают слияние.
        assert "db_path" in merged["general"]
