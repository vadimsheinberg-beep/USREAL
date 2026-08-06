"""Коннектор Bybit: подпись запросов, пагинация, нарезка периода, разбор записей."""

import hashlib
import hmac
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from expenses.categories import Categorizer
from expenses.models import DIRECTION_EXPENSE, DIRECTION_INCOME
from expenses.sources.bybit import (
    MAINNET_URL,
    TESTNET_URL,
    BybitClient,
    BybitConfig,
    BybitError,
    _from_deposit,
    _from_transaction_log,
    _from_withdrawal,
    _windows,
)

KEY, SECRET = "test-key", "test-secret"


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", KEY)
    monkeypatch.setenv("BYBIT_API_SECRET", SECRET)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def ok(result: dict) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": result}


class Recorder:
    """Подменяет ``session.get`` и запоминает запросы."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}))
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return FakeResponse(payload)

    def params(self, index: int = 0) -> dict:
        query = parse_qs(urlparse(self.calls[index][0]).query)
        return {k: v[0] for k, v in query.items()}


def client(monkeypatch, recorder, **kwargs) -> BybitClient:
    config = BybitConfig(rate_limit_delay=0.0, **kwargs)
    instance = BybitClient(config)
    monkeypatch.setattr(instance.session, "get", recorder)
    return instance


class TestAuth:
    def test_missing_keys_are_explained(self, monkeypatch):
        monkeypatch.delenv("BYBIT_API_KEY", raising=False)
        with pytest.raises(BybitError, match="только права на чтение"):
            BybitClient(BybitConfig())

    def test_signature_matches_bybit_scheme(self, monkeypatch):
        recorder = Recorder(ok({"rows": [], "nextPageCursor": ""}))
        instance = client(monkeypatch, recorder)
        instance.request("/v5/asset/deposit/query-record", {"limit": 5})

        url, headers = recorder.calls[0]
        query = urlparse(url).query
        expected = hmac.new(
            SECRET.encode(),
            f"{headers['X-BAPI-TIMESTAMP']}{KEY}{instance.config.recv_window}{query}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert headers["X-BAPI-SIGN"] == expected
        assert headers["X-BAPI-API-KEY"] == KEY

    def test_secret_never_goes_into_the_url(self, monkeypatch):
        recorder = Recorder(ok({"rows": []}))
        client(monkeypatch, recorder).request("/v5/asset/deposit/query-record", {"limit": 5})
        assert SECRET not in recorder.calls[0][0]

    def test_empty_params_are_dropped_before_signing(self, monkeypatch):
        recorder = Recorder(ok({"rows": []}))
        client(monkeypatch, recorder).request("/x", {"a": 1, "b": None, "c": ""})
        assert recorder.params() == {"a": "1"}

    def test_testnet_switch(self, monkeypatch):
        assert BybitConfig().url == MAINNET_URL
        assert BybitConfig(testnet=True).url == TESTNET_URL


class TestErrors:
    def test_nonzero_retcode_becomes_an_error(self, monkeypatch):
        recorder = Recorder({"retCode": 10004, "retMsg": "error sign", "result": {}})
        with pytest.raises(BybitError, match="подпись"):
            client(monkeypatch, recorder).request("/x", {})

    def test_permission_error_hints_read_only(self, monkeypatch):
        recorder = Recorder({"retCode": 10005, "retMsg": "permission denied", "result": {}})
        with pytest.raises(BybitError, match="только на чтение"):
            client(monkeypatch, recorder).request("/x", {})

    def test_unknown_code_still_readable(self, monkeypatch):
        recorder = Recorder({"retCode": 99999, "retMsg": "что-то не так", "result": {}})
        with pytest.raises(BybitError, match="99999"):
            client(monkeypatch, recorder).request("/x", {})


class TestPagination:
    def test_follows_cursor_until_it_is_empty(self, monkeypatch):
        recorder = Recorder(
            ok({"rows": [{"a": 1}], "nextPageCursor": "page2"}),
            ok({"rows": [{"a": 2}], "nextPageCursor": ""}),
        )
        rows = list(client(monkeypatch, recorder).paginate("/x", {}, "rows"))
        assert rows == [{"a": 1}, {"a": 2}]
        assert recorder.params(1)["cursor"] == "page2"

    def test_stops_on_empty_page(self, monkeypatch):
        recorder = Recorder(ok({"rows": [], "nextPageCursor": "still-here"}))
        assert list(client(monkeypatch, recorder).paginate("/x", {}, "rows")) == []
        assert len(recorder.calls) == 1

    def test_max_pages_guards_against_a_loop(self, monkeypatch):
        # API отдаёт тот же курсор бесконечно — выходим по лимиту, а не виснем.
        recorder = Recorder(ok({"rows": [{"a": 1}], "nextPageCursor": "same"}))
        rows = list(client(monkeypatch, recorder, max_pages=3).paginate("/x", {}, "rows"))
        assert len(rows) == 3


class TestWindows:
    def test_splits_long_period(self):
        result = list(_windows(date(2026, 1, 1), date(2026, 1, 20), 7))
        assert result[0] == (date(2026, 1, 1), date(2026, 1, 7))
        assert result[-1] == (date(2026, 1, 15), date(2026, 1, 20))

    def test_short_period_is_one_window(self):
        assert list(_windows(date(2026, 1, 1), date(2026, 1, 3), 30)) == [
            (date(2026, 1, 1), date(2026, 1, 3))
        ]

    def test_single_day(self):
        assert list(_windows(date(2026, 1, 1), date(2026, 1, 1), 7)) == [
            (date(2026, 1, 1), date(2026, 1, 1))
        ]

    def test_fetch_chunks_the_request(self, monkeypatch):
        recorder = Recorder(ok({"rows": [], "nextPageCursor": ""}))
        instance = client(monkeypatch, recorder, endpoints=("deposits",), asset_window_days=30)
        instance.fetch(date(2026, 1, 1), date(2026, 3, 31))
        assert len(recorder.calls) == 3  # 90 дней = три окна по 30

    def test_backwards_period_is_rejected(self, monkeypatch):
        recorder = Recorder(ok({"rows": []}))
        with pytest.raises(BybitError, match="позже конца"):
            client(monkeypatch, recorder).fetch(date(2026, 3, 1), date(2026, 1, 1))

    def test_default_period_is_used_when_none_given(self, monkeypatch):
        recorder = Recorder(ok({"rows": [], "nextPageCursor": ""}))
        instance = client(
            monkeypatch, recorder, endpoints=("deposits",), default_days=30, asset_window_days=30
        )
        instance.fetch()
        started = int(recorder.params()["startTime"]) / 1000
        assert date.fromtimestamp(started) >= date.today() - timedelta(days=31)


class TestDeposits:
    def test_deposit_is_income(self):
        tx = _from_deposit(
            {"coin": "USDT", "chain": "TRX", "amount": "500", "successAt": "1773532800000", "txID": "abc"}
        )
        assert tx.direction == DIRECTION_INCOME
        assert (tx.amount, tx.currency, tx.source_id) == (500.0, "USDT", "abc")
        assert tx.date == date(2026, 3, 15)

    def test_zero_amount_is_skipped(self):
        assert _from_deposit({"coin": "USDT", "amount": "0", "successAt": "1773532800000"}) is None

    def test_missing_timestamp_is_skipped(self):
        assert _from_deposit({"coin": "USDT", "amount": "5"}) is None


class TestWithdrawals:
    def test_withdrawal_and_its_fee_are_separate_operations(self):
        items = _from_withdrawal(
            {
                "coin": "USDT",
                "chain": "TRX",
                "amount": "300",
                "withdrawFee": "1",
                "updateTime": "1773532800000",
                "withdrawId": "w-1",
            }
        )
        assert [t.amount for t in items] == [300.0, 1.0]
        assert all(t.direction == DIRECTION_EXPENSE for t in items)
        # Разные id, иначе комиссия схлопнется с выводом при дедупликации.
        assert items[0].key != items[1].key

    def test_no_fee_means_one_operation(self):
        items = _from_withdrawal(
            {"coin": "BTC", "amount": "0.5", "withdrawFee": "0", "updateTime": "1773532800000"}
        )
        assert len(items) == 1
        assert items[0].currency == "BTC"


class TestTransactionLog:
    def test_fee_is_an_expense(self):
        items = _from_transaction_log(
            {
                "type": "TRADE",
                "currency": "USDT",
                "symbol": "BTCUSDT",
                "fee": "0.55",
                "cashFlow": "0",
                "transactionTime": "1773532800000",
                "id": "log-1",
            }
        )
        assert len(items) == 1
        assert items[0].direction == DIRECTION_EXPENSE
        assert items[0].amount == pytest.approx(0.55)

    def test_fee_refund_is_income(self):
        items = _from_transaction_log(
            {"type": "TRADE", "currency": "USDT", "fee": "-0.2", "transactionTime": "1773532800000"}
        )
        assert items[0].direction == DIRECTION_INCOME

    def test_trades_are_skipped_by_default(self):
        # Сделка меняет монету на монету — это не трата.
        items = _from_transaction_log(
            {
                "type": "TRADE",
                "currency": "USDT",
                "cashFlow": "-1000",
                "fee": "0",
                "transactionTime": "1773532800000",
            }
        )
        assert items == []

    def test_trades_can_be_included(self):
        items = _from_transaction_log(
            {
                "type": "TRADE",
                "currency": "USDT",
                "cashFlow": "-1000",
                "transactionTime": "1773532800000",
            },
            include_trades=True,
        )
        assert items[0].amount == pytest.approx(1000)

    def test_negative_funding_is_an_expense(self):
        items = _from_transaction_log(
            {
                "type": "SETTLEMENT",
                "currency": "USDT",
                "cashFlow": "-3.4",
                "transactionTime": "1773532800000",
            }
        )
        assert items[0].direction == DIRECTION_EXPENSE
        assert items[0].source_category == "bybit:funding"

    def test_positive_funding_is_income(self):
        items = _from_transaction_log(
            {
                "type": "SETTLEMENT",
                "currency": "USDT",
                "cashFlow": "2.1",
                "transactionTime": "1773532800000",
            }
        )
        assert items[0].direction == DIRECTION_INCOME

    def test_internal_transfers_are_skipped_by_default(self):
        row = {
            "type": "TRANSFER_OUT",
            "currency": "USDT",
            "cashFlow": "-100",
            "transactionTime": "1773532800000",
        }
        assert _from_transaction_log(row) == []
        assert _from_transaction_log(row, include_transfers=True)[0].amount == pytest.approx(100)

    def test_unparseable_row_is_skipped(self):
        assert _from_transaction_log({"type": "TRADE", "currency": "USDT"}) == []


class TestCategorisation:
    @pytest.mark.parametrize(
        "tag,category",
        [
            ("bybit:withdraw", "Вывод с биржи"),
            ("bybit:deposit", "Пополнение биржи"),
            ("bybit:fee", "Комиссии биржи"),
            ("bybit:funding", "Фандинг"),
            ("bybit:transfer", "Переводы между счетами"),
        ],
    )
    def test_machine_tags_map_to_categories(self, tag, category):
        tx = _from_deposit(
            {"coin": "USDT", "amount": "1", "successAt": "1773532800000", "txID": "t"}
        )
        tx.source_category = tag
        assert Categorizer().categorize(tx).category == category

    def test_custom_rule_still_wins(self):
        from expenses.categories import rules_from_config

        tx = _from_deposit({"coin": "USDT", "amount": "1", "successAt": "1773532800000"})
        rules = rules_from_config([{"category": "Инвестиции", "patterns": ["bybit:deposit"]}])
        assert Categorizer(rules).categorize(tx).category == "Инвестиции"


class TestFetchIntegration:
    def test_collects_all_endpoints(self, monkeypatch):
        deposit = {"coin": "USDT", "amount": "500", "successAt": "1773532800000", "txID": "d1"}
        withdraw = {
            "coin": "USDT",
            "amount": "100",
            "withdrawFee": "1",
            "updateTime": "1773532800000",
            "withdrawId": "w1",
        }
        log_row = {
            "type": "SETTLEMENT",
            "currency": "USDT",
            "cashFlow": "-2",
            "transactionTime": "1773532800000",
            "id": "l1",
        }

        def fake_get(url, headers=None, timeout=None):
            if "deposit" in url:
                return FakeResponse(ok({"rows": [deposit], "nextPageCursor": ""}))
            if "withdraw" in url:
                return FakeResponse(ok({"rows": [withdraw], "nextPageCursor": ""}))
            return FakeResponse(ok({"list": [log_row], "nextPageCursor": ""}))

        instance = client(monkeypatch, fake_get)
        items = instance.fetch(date(2026, 3, 15), date(2026, 3, 15))

        assert [t.source_category for t in items] == [
            "bybit:deposit",
            "bybit:withdraw",
            "bybit:fee",
            "bybit:funding",
        ]
        assert all(t.source == "bybit" for t in items)

    def test_probe_reports_each_endpoint(self, monkeypatch):
        recorder = Recorder(ok({"rows": [{"coin": "USDT", "amount": "1"}], "list": []}))
        output = client(monkeypatch, recorder).probe()
        assert "вводы" in output and "выводы" in output and "журнал операций" in output

    def test_probe_survives_a_broken_endpoint(self, monkeypatch):
        def fake_get(url, headers=None, timeout=None):
            if "withdraw" in url:
                return FakeResponse({"retCode": 10005, "retMsg": "denied", "result": {}})
            return FakeResponse(ok({"rows": [], "list": []}))

        output = client(monkeypatch, fake_get).probe()
        assert "ошибка" in output and "вводы" in output
