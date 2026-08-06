"""Разбор сырых записей: даты, суммы, направление операции, маппинг полей."""

from datetime import date

import pytest

from expenses.models import DIRECTION_EXPENSE, DIRECTION_INCOME, Transaction
from expenses.sources.base import (
    FieldMap,
    SourceError,
    extract_records,
    normalize_record,
    normalize_records,
    parse_amount,
    parse_date,
    pick,
)


class TestParseDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-03-15", date(2026, 3, 15)),
            ("15/03/2026", date(2026, 3, 15)),
            ("15.03.2026", date(2026, 3, 15)),
            ("2026-03-15T22:10:05Z", date(2026, 3, 15)),
            ("2026-03-15 22:10:05", date(2026, 3, 15)),
        ],
    )
    def test_common_formats(self, raw, expected):
        assert parse_date(raw) == expected

    def test_unix_seconds(self):
        assert parse_date(1773532800) == date(2026, 3, 15)

    def test_unix_milliseconds(self):
        # Мобильные приложения часто отдают миллисекунды — не 5138 год.
        assert parse_date(1773532800000) == date(2026, 3, 15)

    def test_empty_is_an_error(self):
        with pytest.raises(SourceError):
            parse_date("")


class TestParseAmount:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (-45.5, -45.5),
            ("1,234.56", 1234.56),
            ("1 234,56", 1234.56),
            ("₪-45.00", -45.0),
            ("(45.00)", -45.0),
            ("120", 120.0),
        ],
    )
    def test_formats(self, raw, expected):
        assert parse_amount(raw) == pytest.approx(expected)

    def test_garbage_is_an_error(self):
        with pytest.raises(SourceError):
            parse_amount("—")


class TestPick:
    def test_dotted_path(self):
        assert pick({"payment": {"sum": 12}}, "payment.sum") == 12

    def test_list_of_candidates_takes_first_present(self):
        assert pick({"amount": 5}, ["sum", "amount"]) == 5

    def test_missing_path_is_none(self):
        assert pick({"a": {"b": 1}}, "a.c.d") is None


class TestNormalizeRecord:
    def test_maps_fields_and_signs(self):
        record = {
            "id": "tx-1",
            "date": "2026-03-15",
            "amount": -120.5,
            "description": "SHUFERSAL DEAL",
            "currency": "ILS",
        }
        tx = normalize_record(record, FieldMap(), "babit")
        assert tx.source_id == "tx-1"
        assert tx.amount == 120.5  # модуль суммы
        assert tx.direction == DIRECTION_EXPENSE
        assert tx.month == "2026-03"

    def test_positive_amount_is_income_by_default(self):
        record = {"id": "2", "date": "2026-03-15", "amount": 9000, "description": "SALARY"}
        assert normalize_record(record, FieldMap(), "babit").direction == DIRECTION_INCOME

    def test_inverted_sign_convention(self):
        # Некоторые API отдают расход положительным числом.
        mapping = FieldMap(negative_is_expense=False)
        record = {"id": "3", "date": "2026-03-15", "amount": 50, "description": "CAFE"}
        assert normalize_record(record, mapping, "babit").direction == DIRECTION_EXPENSE

    def test_explicit_direction_field_wins_over_sign(self):
        mapping = FieldMap(direction="type", expense_values=("DEBIT",))
        record = {"id": "4", "date": "2026-03-15", "amount": 50, "description": "X", "type": "DEBIT"}
        assert normalize_record(record, mapping, "babit").direction == DIRECTION_EXPENSE

    def test_nested_paths(self):
        mapping = FieldMap(
            date="meta.when", amount="payment.sum", description=["merchant.name", "note"]
        )
        record = {
            "meta": {"when": "2026-04-02"},
            "payment": {"sum": "-15.00"},
            "merchant": {"name": "AROMA"},
        }
        tx = normalize_record(record, mapping, "babit")
        assert (tx.date, tx.amount, tx.description) == (date(2026, 4, 2), 15.0, "AROMA")

    def test_missing_description_gets_placeholder(self):
        record = {"id": "5", "date": "2026-03-15", "amount": -10}
        assert normalize_record(record, FieldMap(), "babit").description == "без описания"


class TestNormalizeRecords:
    def test_broken_row_is_skipped_not_fatal(self):
        records = [
            {"id": "1", "date": "2026-03-15", "amount": -10, "description": "ok"},
            {"id": "2", "date": "не дата", "amount": -10, "description": "плохая"},
        ]
        result = normalize_records(records, FieldMap(), "csv")
        assert [tx.source_id for tx in result] == ["1"]

    def test_strict_mode_raises(self):
        records = [{"id": "2", "date": "не дата", "amount": -10, "description": "плохая"}]
        with pytest.raises(SourceError):
            normalize_records(records, FieldMap(), "csv", strict=True)


class TestExtractRecords:
    def test_bare_list(self):
        assert extract_records([{"a": 1}], None) == [{"a": 1}]

    def test_finds_common_wrappers(self):
        assert extract_records({"data": [{"a": 1}]}, None) == [{"a": 1}]

    def test_finds_nested_wrapper(self):
        assert extract_records({"data": {"items": [{"a": 1}]}}, None) == [{"a": 1}]

    def test_explicit_path(self):
        payload = {"result": {"page": {"rows": [{"a": 1}]}}}
        assert extract_records(payload, "result.page.rows") == [{"a": 1}]

    def test_unknown_shape_explains_itself(self):
        with pytest.raises(SourceError, match="records_path"):
            extract_records({"total": 3}, None)


class TestTransactionKey:
    def test_same_source_id_gives_same_key(self):
        a = Transaction(date=date(2026, 3, 1), amount=10, description="A", source="b", source_id="1")
        b = Transaction(date=date(2026, 4, 9), amount=99, description="B", source="b", source_id="1")
        assert a.key == b.key

    def test_without_id_key_uses_date_amount_description(self):
        a = Transaction(date=date(2026, 3, 1), amount=10, description="Cafe", source="csv")
        b = Transaction(date=date(2026, 3, 1), amount=10, description="cafe", source="csv")
        c = Transaction(date=date(2026, 3, 2), amount=10, description="Cafe", source="csv")
        assert a.key == b.key
        assert a.key != c.key

    def test_roundtrip_through_dict(self):
        tx = Transaction(date=date(2026, 3, 1), amount=10.5, description="X", source="csv")
        assert Transaction.from_dict(tx.to_dict()).to_dict() == tx.to_dict()
