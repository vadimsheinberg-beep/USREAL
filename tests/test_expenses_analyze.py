"""Помесячная аналитика: сводки, тренды, регулярные списания, период."""

from datetime import date

import pytest

from expenses.analyze import (
    category_trends,
    compare_months,
    filter_period,
    find_recurring,
    month_range,
    monthly_summary,
    overall_stats,
    top_expenses,
)
from expenses.models import DIRECTION_INCOME, Transaction


def tx(day: str, amount: float, description: str, category: str = "Прочее", **kwargs):
    item = Transaction(
        date=date.fromisoformat(day), amount=amount, description=description, **kwargs
    )
    item.category = category
    item.merchant = description.lower()
    return item


@pytest.fixture
def three_months():
    items = []
    for month in ("01", "02", "03"):
        items.append(tx(f"2026-{month}-05", 6000, "Аренда", "Жильё"))
        items.append(tx(f"2026-{month}-07", 54.9, "Netflix", "Подписки"))
        items.append(tx(f"2026-{month}-10", 20000, "Зарплата", "Зарплата", direction=DIRECTION_INCOME))
    items.append(tx("2026-01-12", 300, "Продукты", "Продукты"))
    items.append(tx("2026-03-12", 900, "Продукты", "Продукты"))
    return sorted(items, key=lambda t: t.date)


class TestMonthlySummary:
    def test_splits_expense_and_income(self, three_months):
        summary = monthly_summary(three_months)
        assert [s.month for s in summary] == ["2026-01", "2026-02", "2026-03"]
        assert summary[0].expense == pytest.approx(6354.9)
        assert summary[0].income == pytest.approx(20000)
        assert summary[0].net == pytest.approx(13645.1)

    def test_income_does_not_count_as_operation(self, three_months):
        # count — это число трат; поступления сводку по операциям не раздувают.
        assert monthly_summary(three_months)[1].count == 2

    def test_top_categories_sorted_by_amount(self, three_months):
        top = monthly_summary(three_months)[2].top_categories()
        assert top[0][0] == "Жильё"
        assert top[1][0] == "Продукты"

    def test_empty_input(self):
        assert monthly_summary([]) == []


class TestCategoryTrends:
    def test_missing_month_is_zero_not_absent(self, three_months):
        trends = {t.category: t for t in category_trends(monthly_summary(three_months))}
        # Продукты были в январе и марте, февраль должен быть явным нулём.
        assert trends["Продукты"].by_month == {"2026-01": 300, "2026-02": 0.0, "2026-03": 900}

    def test_sorted_by_total(self, three_months):
        assert category_trends(monthly_summary(three_months))[0].category == "Жильё"

    def test_last_delta(self, three_months):
        trends = {t.category: t for t in category_trends(monthly_summary(three_months))}
        assert trends["Продукты"].last_delta == pytest.approx(900)

    def test_average_uses_all_months_in_period(self, three_months):
        trends = {t.category: t for t in category_trends(monthly_summary(three_months))}
        assert trends["Продукты"].average == pytest.approx(400)


class TestFindRecurring:
    def test_finds_stable_monthly_charge(self, three_months):
        found = {r.merchant: r for r in find_recurring(three_months)}
        assert "netflix" in found
        assert found["netflix"].typical_amount == pytest.approx(54.9)
        assert found["netflix"].yearly_estimate == pytest.approx(658.8)

    def test_ignores_jumpy_amounts(self, three_months):
        # Продукты: 300 и 900 — и месяцев мало, и разброс большой.
        assert "продукты" not in {r.merchant for r in find_recurring(three_months)}

    def test_respects_min_months(self, three_months):
        assert find_recurring(three_months, min_months=4) == []

    def test_uses_monthly_totals_not_single_charges(self):
        # Два похода в месяц по 50 — это 100 в месяц, а не 50.
        items = []
        for month in ("01", "02", "03"):
            items.append(tx(f"2026-{month}-03", 50, "Клуб"))
            items.append(tx(f"2026-{month}-20", 50, "Клуб"))
        found = find_recurring(items)
        assert found[0].typical_amount == pytest.approx(100)

    def test_income_is_not_recurring_charge(self, three_months):
        assert "зарплата" not in {r.merchant for r in find_recurring(three_months)}


class TestFilterPeriod:
    def test_last_n_months_counts_from_newest_transaction(self, three_months):
        # Выписку выгружают задним числом, поэтому опорная точка — данные.
        result = filter_period(three_months, months=2)
        assert {t.month for t in result} == {"2026-02", "2026-03"}

    def test_explicit_bounds(self, three_months):
        result = filter_period(three_months, since=date(2026, 2, 1), until=date(2026, 2, 28))
        assert {t.month for t in result} == {"2026-02"}

    def test_empty_input(self):
        assert filter_period([], months=6) == []

    def test_period_crossing_new_year(self):
        items = [tx("2025-11-05", 10, "A"), tx("2025-12-05", 10, "B"), tx("2026-01-05", 10, "C")]
        assert {t.month for t in filter_period(items, months=3)} == {
            "2025-11",
            "2025-12",
            "2026-01",
        }


class TestMisc:
    def test_month_range_crosses_year(self):
        assert month_range(date(2025, 11, 3), date(2026, 2, 1)) == [
            "2025-11",
            "2025-12",
            "2026-01",
            "2026-02",
        ]

    def test_compare_months_sorted_by_absolute_change(self, three_months):
        summary = monthly_summary(three_months)
        rows = compare_months(summary[1], summary[2])
        assert rows[0][0] == "Продукты"  # 0 → 900, самое заметное изменение

    def test_top_expenses_excludes_income(self, three_months):
        assert all(t.is_expense for t in top_expenses(three_months, 5))

    def test_overall_stats(self, three_months):
        stats = overall_stats(monthly_summary(three_months))
        assert stats["months"] == 3
        assert stats["expense_total"] == pytest.approx(19364.7)
        assert stats["expense_avg"] == pytest.approx(19364.7 / 3)

    def test_overall_stats_empty(self):
        assert overall_stats([])["months"] == 0
