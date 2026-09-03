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


class JerusalemSource(Source):
    """Участки под жильё в Иерусалиме — предмет команды city."""

    name = "fake"
    title = "тестовый источник"

    def fetch(self):
        return [
            Lot(source="fake", source_id="1", tender_name="иерусалимский",
                settlement="ירושלים", purpose="מגורים", price_nis=2_000_000.0,
                area_sqm=500.0, closing_date="2099-01-01"),
            Lot(source="fake", source_id="2", tender_name="хайфский",
                settlement="חיפה", purpose="מגורים", price_nis=2_000_000.0,
                area_sqm=500.0, closing_date="2099-01-01"),
            # Портал заполняет назначение далеко не всегда — и таких лотов в
            # Иерусалиме оказались все 213 из 213.
            Lot(source="fake", source_id="3", tender_name="безымянный",
                settlement="ירושלים", price_nis=900_000.0,
                area_sqm=300.0, closing_date="2099-01-01"),
        ]


class ScorableSource(Source):
    """Лоты, которым есть чем набрать балл: площадь, единицы и срок подачи."""

    name = "fake"
    title = "тестовый источник"

    def fetch(self):
        return [
            Lot(source="fake", source_id="1", tender_name="плотный",
                settlement="חיפה", area_sqm=1_000.0, units=40,
                price_nis=9_000_000.0, closing_date="2099-01-01"),
            Lot(source="fake", source_id="2", tender_name="редкий",
                settlement="חיפה", area_sqm=10_000.0, units=4,
                price_nis=3_000_000.0, closing_date="2099-01-01"),
        ]


class MixedSource(Source):
    """Лот с ценой и лот без неё — полная выгрузка обязана показать оба."""

    name = "fake"
    title = "тестовый источник"

    def fetch(self):
        return [
            Lot(source="fake", source_id="1", tender_name="оценённый",
                settlement="חיפה", area_sqm=1_000.0, units=10,
                price_nis=4_000_000.0, closing_date="2099-01-01"),
            Lot(source="fake", source_id="2", tender_name="безценный",
                settlement="חיפה", area_sqm=800.0, closing_date="2099-01-01"),
        ]


class TestTopCommand:
    """Сама команда, а не только её начинка.

    Первый прогон топа упал на NameError: в модуле не было импорта, которым
    команда пользовалась. Ни один тест этого не поймал — проверялись подбор
    лотов и вёрстка по отдельности, но не команда целиком.
    """

    @pytest.fixture(autouse=True)
    def scorable(self, monkeypatch):
        # Сравнимые сделки берутся из базы, но за индексом ЦСБ ходят в сеть.
        # Пустой список означал бы «оценки нет», а без неё лот в рейтинг не
        # попадает — фикстуре нужны настоящие сделки.
        from tests.test_pipeline import comparables_for

        monkeypatch.setattr(
            pipeline, "build_appraiser", lambda *a, **k: comparables_for("חיפה")
        )
        monkeypatch.setattr(pipeline, "backfill_settlement_codes", lambda *a, **k: 0)
        monkeypatch.setattr(pipeline, "SOURCES_BY_NAME", {"fake": ScorableSource})
        monkeypatch.setattr(cli, "SOURCES_BY_NAME", {"fake": ScorableSource})

    def fill(self, config_file):
        cli.main(["--config", str(config_file), "run", "--sources", "fake", "--no-notify"])

    def test_runs_and_prints_the_ranking(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        assert cli.main(["--config", str(config_file), "top"]) == 0
        out = capsys.readouterr().out
        assert "Лучшие предложения — топ-2" in out
        assert "<b>1.</b>" in out
        # Порядок решает показатель цены: он весит больше остальных, потому
        # что единственный измеряется в деньгах. Плотный участок запрошен
        # дороже своей оценки и уступает место, несмотря на полный балл за
        # плотность, — ровно этого от рейтинга предложений и ждут.
        assert out.index("редкий") < out.index("плотный")

    def test_empty_database_explains_itself(self, config_file, capsys):
        assert cli.main(["--config", str(config_file), "top"]) == 0
        assert "harvest" in capsys.readouterr().out

    def test_limit_is_passed_through(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        cli.main(["--config", str(config_file), "top", "--limit", "1"])
        assert "топ-1" in capsys.readouterr().out

    def test_csv_export(self, config_file, tmp_path, capsys):
        self.fill(config_file)
        out = tmp_path / "top.csv"
        cli.main(["--config", str(config_file), "top", "--out", str(out)])
        assert out.exists()

    def test_send_without_a_token_says_so(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        assert cli.main(["--config", str(config_file), "top", "--send"]) == 2
        assert "Нет токена" in capsys.readouterr().out


class TestCityCommand:
    """Команда целиком, а не только её начинка.

    Прогон упал на NameError: в модуле не было импорта, которым команда
    пользовалась. Ровно та же ошибка уже случалась с топом, и тогда же был
    сделан вывод — каждая команда должна проверяться запуском. Для city и
    enrich этот вывод я применить забыл, и она повторилась.
    """

    @pytest.fixture(autouse=True)
    def city_source(self, monkeypatch):
        monkeypatch.setattr(pipeline, "SOURCES_BY_NAME", {"fake": JerusalemSource})
        monkeypatch.setattr(cli, "SOURCES_BY_NAME", {"fake": JerusalemSource})

    def fill(self, config_file):
        cli.main(["--config", str(config_file), "run", "--sources", "fake", "--no-notify"])

    def test_it_runs_and_prints_the_slice(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        code = cli.main(["--config", str(config_file), "city", "--city", "Иерусалим"])
        assert code == 0
        assert "Участки под жильё" in capsys.readouterr().out

    def test_the_price_ceiling_reaches_the_digest(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        cli.main([
            "--config", str(config_file), "city",
            "--city", "Иерусалим", "--max-usd", "1000000",
        ])
        assert "Порог цены: до $1.00 млн" in capsys.readouterr().out

    def test_csv_export(self, config_file, tmp_path):
        self.fill(config_file)
        out = tmp_path / "city.csv"
        cli.main([
            "--config", str(config_file), "city", "--city", "Иерусалим",
            "--out", str(out),
        ])
        assert out.exists()

    def test_send_without_a_token_says_so(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        code = cli.main([
            "--config", str(config_file), "city", "--city", "Иерусалим", "--send",
        ])
        assert code == 2
        assert "Нет токена" in capsys.readouterr().out

    def test_it_shows_where_the_lots_were_lost(self, config_file, capsys):
        """Пустой срез обязан назвать причину, а не просто оказаться пустым.

        Первый боевой прогон дал ровно ноль лотов по Иерусалиму, и по
        сообщению нельзя было понять, чего не хватило: города в базе, цены
        или запаса под порогом. Воронка отвечает на это числами.
        """
        self.fill(config_file)
        capsys.readouterr()
        cli.main([
            "--config", str(config_file), "city",
            "--city", "Иерусалим", "--max-usd", "1",
        ])
        out = capsys.readouterr().out
        counts = dict(
            (line.rsplit(maxsplit=1)[0].strip(), int(line.rsplit(maxsplit=1)[1]))
            for line in out.splitlines()
            if line.startswith("  ") and line.rsplit(maxsplit=1)[-1].isdigit()
        )
        # Иерусалимский лот в базе есть, цена у него есть, а под порог в
        # доллар он не проходит — и воронка называет именно этот шаг.
        assert counts["город совпал"] == 2
        assert counts["с объявленной ценой"] == 2
        assert counts["под порогом цены"] == 0
        assert "Отбор:" in out

    def test_it_lists_what_purposes_the_city_actually_has(self, config_file, capsys):
        """Перечень значений вместо догадок о содержимом чужого поля.

        Срез по Иерусалиму дважды выходил пустым, и оба раза причина была не
        та, на которую я думал: сперва решил, что дело в цене, потом — что
        назначение не заполнено. Ни то ни другое. Перечень отвечает точно.
        """
        self.fill(config_file)
        capsys.readouterr()
        cli.main(["--config", str(config_file), "city", "--city", "Иерусалим"])
        out = capsys.readouterr().out
        assert "что вообще есть в городе" in out
        assert "מגורים" in out

    def test_the_category_filter_does_not_rely_on_free_text(self, config_file, capsys):
        """Категория разобрана нами, а текст назначения пишет портал."""
        self.fill(config_file)
        capsys.readouterr()
        cli.main([
            "--config", str(config_file), "city",
            "--city", "Иерусалим", "--land-use", "agriculture",
        ])
        out = capsys.readouterr().out
        assert "категория совпала" in out
        assert "иерусалимский" not in out

    def test_lots_without_a_stated_purpose_stay_in(self, config_file, capsys):
        """Молчание портала о назначении — не ответ «не жильё».

        Из 213 иерусалимских лотов текст «מגורים» не стоял ни у одного, и
        срез схлопывался в ноль при живых лотах в базе.
        """
        self.fill(config_file)
        capsys.readouterr()
        cli.main([
            "--config", str(config_file), "city",
            "--city", "Иерусалим", "--purpose", "מסחר",
        ])
        out = capsys.readouterr().out
        assert "назначение совпало" in out
        # Лот с пустым назначением остался, лот с чужим назначением ушёл.
        assert "безымянный" in out
        assert "иерусалимский" not in out
        assert "назначение портал не указал" in out

    def test_the_indicators_are_recomputed(self, config_file, capsys, monkeypatch):
        """В строках среза стоят числа, а не прочерки.

        Срез читал лоты из базы как есть. Показатели там записаны в день
        загрузки лота — у большинства не записаны вовсе, — и витрина честно
        показывала прочерк там, где обещала показатель.
        """
        from tests.test_pipeline import comparables_for

        self.fill(config_file)
        monkeypatch.setattr(
            pipeline, "build_appraiser", lambda *a, **k: comparables_for("ירושלים")
        )
        capsys.readouterr()
        cli.main(["--config", str(config_file), "city", "--city", "Иерусалим"])
        row = [
            line for line in capsys.readouterr().out.splitlines()
            if "иерусалимский" in line
        ][0]
        from landtender.report import TABLE_COLUMNS

        # Считаем колонку по имени, а не по номеру: номер уже уезжал, когда
        # в таблицу добавился вид цены.
        assert row.split(", ")[TABLE_COLUMNS.index("оценка ₪")] != "—"


class TestEnrichCommand:
    """Прогон кадастра по базе: команда должна запускаться и без сети."""

    def test_it_runs_and_reports_counts(self, config_file, capsys, monkeypatch):
        monkeypatch.setattr(pipeline, "build_enricher", lambda *a, **k: None)
        code = cli.main(["--config", str(config_file), "enrich", "--minutes", "0"])
        assert code == 0
        assert "выключено" in capsys.readouterr().out


class TestAllCommand:
    """Полная выгрузка: все предложения со всеми показателями.

    Витрина обещает «все», и главная её проверка — что лот без цены или без
    оценки из неё не исчезает. Пустая клетка — это сведение о лоте; молча
    выброшенный лот — потеря.
    """

    @pytest.fixture(autouse=True)
    def scorable(self, monkeypatch):
        from tests.test_pipeline import comparables_for

        monkeypatch.setattr(
            pipeline, "build_appraiser", lambda *a, **k: comparables_for("חיפה")
        )
        monkeypatch.setattr(pipeline, "backfill_settlement_codes", lambda *a, **k: 0)
        monkeypatch.setattr(pipeline, "SOURCES_BY_NAME", {"fake": MixedSource})
        monkeypatch.setattr(cli, "SOURCES_BY_NAME", {"fake": MixedSource})

    def fill(self, config_file):
        cli.main(["--config", str(config_file), "run", "--sources", "fake", "--no-notify"])

    def test_it_runs_and_shows_every_lot(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        assert cli.main(["--config", str(config_file), "all"]) == 0
        out = capsys.readouterr().out
        assert "Все предложения — полная выгрузка" in out
        # Оба лота на месте: и тот, что с ценой, и безымянный по цене.
        assert "с ценой" in out
        assert "безценный" in out
        assert "оценённый" in out

    def test_it_counts_what_is_known(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        cli.main(["--config", str(config_file), "all"])
        assert "Лотов: 2 · с ценой: 1" in capsys.readouterr().out

    def test_lots_with_a_price_come_first(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        cli.main(["--config", str(config_file), "all"])
        out = capsys.readouterr().out
        assert out.index("оценённый") < out.index("безценный")

    def test_the_columns_are_named_before_the_rows(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        cli.main(["--config", str(config_file), "all"])
        out = capsys.readouterr().out
        assert "цена ₪/м²" in out
        assert out.index("цена ₪/м²") < out.index("оценённый")

    def test_csv_holds_the_full_list(self, config_file, tmp_path):
        self.fill(config_file)
        out = tmp_path / "all.csv"
        cli.main(["--config", str(config_file), "all", "--limit", "1", "--out", str(out)])
        # Сообщение урезано до одной строки, выгрузка — нет.
        assert len(out.read_text("utf-8").strip().splitlines()) == 3

    def test_send_without_a_token_says_so(self, config_file, capsys):
        self.fill(config_file)
        capsys.readouterr()
        assert cli.main(["--config", str(config_file), "all", "--send"]) == 2
        assert "Нет токена" in capsys.readouterr().out


def test_stats_command_reports_empty_database(config_file, capsys):
    assert cli.main(["--config", str(config_file), "stats"]) == 0
    assert "Запусков ещё не было" in capsys.readouterr().out


def test_help_is_available():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
