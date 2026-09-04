"""Разведка API: отбор денежных полей и списка документов."""

from landtender.inspect import PRICE_FLOOR, document_list, inspect_tenders, price_candidates
from tests.conftest import FakeHttp, load_fixture


class TestPriceCandidates:
    def test_finds_money_fields_anywhere_in_the_tree(self):
        payload = {"Tik": {"Migrashim": [{"MinPrice": 18_500_000, "Area": 4200}]}}
        found = dict(price_candidates(payload))
        assert found["MinPrice"] == 18_500_000

    def test_small_numbers_are_not_prices(self):
        found = dict(price_candidates({"Area": 4200, "YechidotDiur": 60}))
        assert found == {}

    def test_sorted_by_size_descending(self):
        payload = {"A": 100_000, "B": 9_000_000, "C": 1_000_000}
        assert [key for key, _ in price_candidates(payload)] == ["B", "C", "A"]

    def test_threshold_is_applied(self):
        assert price_candidates({"X": PRICE_FLOOR - 1}) == []
        assert price_candidates({"X": PRICE_FLOOR}) == [("X", float(PRICE_FLOOR))]

    def test_string_amounts_are_parsed(self):
        found = dict(price_candidates({"Mechir": "18,500,000 ₪"}))
        assert found["Mechir"] == 18_500_000.0

    def test_unknown_key_names_are_still_reported(self):
        """Смысл разведки: найти цену под любым, даже незнакомым именем."""
        found = dict(price_candidates({"SchumHatchala": 4_200_000}))
        assert "SchumHatchala" in found


class TestDocumentList:
    def test_collects_documents_from_known_keys(self):
        payload = {"MichrazDocList": [{"Title": "חוברת מכרז"}], "Other": 1}
        assert document_list(payload) == [{"Title": "חוברת מכרז"}]

    def test_collects_from_nested_nodes(self):
        payload = {"Deep": {"Documents": [{"Title": "נספח"}]}}
        assert document_list(payload) == [{"Title": "נספח"}]

    def test_missing_documents_give_empty_list(self):
        assert document_list({"MichrazID": 1}) == []


class TestInspectRun:
    def test_reports_when_search_is_empty(self, capsys):
        http = FakeHttp({"SearchApi/Search": []})
        assert inspect_tenders(http) == 1
        assert "Поиск вернул тендеров: 0" in capsys.readouterr().out

    def test_search_failure_is_reported(self, capsys):
        from landtender.http import HttpError

        http = FakeHttp({"SearchApi/Search": HttpError("503")})
        assert inspect_tenders(http) == 1
        assert "Поиск не отвечает" in capsys.readouterr().out

    def test_walks_endpoint_variants_and_reports_prices(self, capsys):
        from landtender.http import HttpError

        http = FakeHttp({
            "MichrazDetailsApi/Get": load_fixture("rmi_details_20250142.json"),
            "SearchApi/Search": load_fixture("rmi_search.json"),
            "MichrazApi": HttpError("404"),  # несуществующий вариант
        })
        code = inspect_tenders(http, limit=1)
        out = capsys.readouterr().out

        assert code == 0
        assert "MinPrice" in out
        assert "Документы тендера" in out

    def test_tender_number_is_not_mistaken_for_a_price(self, capsys):
        """Номер тендера — восьмизначный, но это не цена."""
        assert price_candidates({"MichrazID": 20250142}) == []
        assert price_candidates({"KodYeshuv": 4000000}) == []

    def test_reports_when_no_variant_returns_content(self, capsys):
        from landtender.http import HttpError

        http = FakeHttp({
            "SearchApi/Search": load_fixture("rmi_search.json"),
            "MichrazDetailsApi": HttpError("404"),
            "MichrazApi": HttpError("404"),
        })
        assert inspect_tenders(http, limit=1) == 1
        assert "брошюры" in capsys.readouterr().out
