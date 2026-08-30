"""Категоризация: встроенные правила, пользовательские правила, приоритеты."""

from datetime import date

import pytest

from expenses.categories import Categorizer, normalize_merchant, rules_from_config
from expenses.models import CATEGORY_UNKNOWN, DIRECTION_INCOME, Transaction


def tx(description: str, amount: float = 100.0, **kwargs) -> Transaction:
    return Transaction(date=date(2026, 3, 15), amount=amount, description=description, **kwargs)


class TestNormalizeMerchant:
    def test_strips_card_numbers_and_case(self):
        assert normalize_merchant("RAMI LEVY 12345 TEL AVIV") == "rami levy"

    def test_keeps_hebrew(self):
        assert "שופרסל" in normalize_merchant("שופרסל דיל 4821")

    def test_same_merchant_different_receipts_collapse(self):
        assert normalize_merchant("AROMA 118") == normalize_merchant("Aroma  9921")


class TestDefaultRules:
    @pytest.mark.parametrize(
        "description,category",
        [
            ("SHUFERSAL DEAL 4821", "Продукты"),
            ("שופרסל דיל", "Продукты"),
            ("RAMI LEVY HASHIKMA", "Продукты"),
            ("AROMA ESPRESSO BAR", "Кафе и рестораны"),
            ("WOLT ORDER 8812", "Кафе и рестораны"),
            ("PAZ YELLOW 118", "Топливо"),
            ("RAV KAV RECHARGE", "Транспорт"),
            ("CELLCOM MOBILE", "Связь и интернет"),
            ("NETFLIX.COM", "Подписки"),
            ("SUPER PHARM DIZENGOFF", "Аптека"),
            ("MACCABI HEALTH SERVICES", "Здоровье"),
            ("EL AL ISRAEL AIRLINES", "Путешествия"),
            ("חברת חשמל לישראל", "Коммуналка"),
            ("HAREL INSURANCE", "Страховка"),
            ("משיכת מזומן כספומט", "Наличные"),
        ],
    )
    def test_known_merchants(self, description, category):
        assert Categorizer().categorize(tx(description)).category == category

    def test_unknown_stays_unknown(self):
        result = Categorizer().categorize(tx("ЗАГАДОЧНЫЙ ПЛАТЁЖ 77"))
        assert result.category == CATEGORY_UNKNOWN
        assert result.category_rule is None

    def test_word_boundaries_prevent_false_positives(self):
        # «bar» из правила про кафе не должен ловить «barber shop».
        assert Categorizer().categorize(tx("BARBER SHOP TLV")).category == "Красота"

    def test_salary_is_income_not_transfer(self):
        result = Categorizer().categorize(tx("משכורת חודשית", direction=DIRECTION_INCOME))
        assert result.category == "Зарплата"

    def test_income_rules_do_not_touch_expenses(self):
        # То же слово в расходной операции не должно давать «Зарплата».
        assert Categorizer().categorize(tx("תשלום שכר דירה")).category != "Зарплата"


class TestCustomRules:
    def test_custom_rule_beats_default(self):
        rules = rules_from_config([{"category": "Работа", "patterns": ["aroma"]}])
        assert Categorizer(rules).categorize(tx("AROMA ESPRESSO")).category == "Работа"

    def test_amount_bounds(self):
        rules = rules_from_config(
            [{"category": "Крупные переводы", "patterns": ["העברה"], "min_amount": 5000}]
        )
        categorizer = Categorizer(rules)
        assert categorizer.categorize(tx("העברה בנקאית", 9000)).category == "Крупные переводы"
        assert categorizer.categorize(tx("העברה בנקאית", 100)).category != "Крупные переводы"

    def test_regex_rule(self):
        rules = rules_from_config(
            [{"category": "Такси", "patterns": [r"^(gett|yango)\b"], "regex": True}]
        )
        categorizer = Categorizer(rules)
        assert categorizer.categorize(tx("GETT RIDE 12")).category == "Такси"
        assert categorizer.categorize(tx("BUDGET GETT")).category != "Такси"

    def test_incomplete_rule_is_ignored(self):
        assert rules_from_config([{"category": "Пусто"}, {"patterns": ["x"]}]) == []

    def test_direction_scoped_rule(self):
        rules = rules_from_config(
            [{"category": "Возврат налога", "patterns": ["מס הכנסה"], "direction": "income"}]
        )
        categorizer = Categorizer(rules)
        income = tx("החזר מס הכנסה", direction=DIRECTION_INCOME)
        assert categorizer.categorize(income).category == "Возврат налога"
        assert categorizer.categorize(tx("תשלום מס הכנסה")).category == "Налоги"


class TestSourceCategory:
    def test_used_only_when_trusted(self):
        item = tx("НЕИЗВЕСТНО", source_category="Groceries")
        assert Categorizer().categorize(item).category == CATEGORY_UNKNOWN
        assert Categorizer(trust_source_category=True).categorize(item).category == "Groceries"

    def test_uncategorized_lists_only_unmatched(self):
        items = [tx("SHUFERSAL"), tx("НЕИЗВЕСТНО")]
        categorizer = Categorizer()
        categorizer.categorize_all(items)
        assert [t.description for t in categorizer.uncategorized(items)] == ["НЕИЗВЕСТНО"]
