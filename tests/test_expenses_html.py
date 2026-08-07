"""HTML-отчёт: структура страницы, экранирование, самодостаточность."""

import re
from datetime import date

import pytest

from expenses.categories import Categorizer
from expenses.html_report import _int, _month_label, render_html
from expenses.models import DIRECTION_INCOME, Transaction


def tx(day: str, amount: float, description: str, **kwargs) -> Transaction:
    return Transaction(
        date=date.fromisoformat(day), amount=amount, description=description, source="t", **kwargs
    )


@pytest.fixture
def items():
    raw = []
    for month in ("01", "02", "03"):
        raw.append(tx(f"2026-{month}-05", 6000, "שכר דירה"))
        raw.append(tx(f"2026-{month}-07", 54.9, "NETFLIX.COM"))
        raw.append(tx(f"2026-{month}-10", 20000, "SALARY", direction=DIRECTION_INCOME))
    raw.append(tx("2026-03-12", 900, "SHUFERSAL DEAL"))
    return Categorizer().categorize_all(sorted(raw, key=lambda t: t.date))


@pytest.fixture
def page(items):
    return render_html(items, today=date(2026, 4, 1))


class TestStructure:
    def test_is_a_complete_document(self, page):
        assert page.startswith("<!doctype html>")
        assert page.rstrip().endswith("</html>")
        assert '<html lang="ru">' in page

    def test_has_every_section(self, page):
        for heading in (
            "Категории по месяцам",
            "Расход по месяцам",
            "Что изменилось",
            "Регулярные списания",
            "Крупнейшие траты",
        ):
            assert heading in page

    def test_pivot_has_a_column_per_month(self, page):
        header = re.search(r"<thead><tr>(.*?)</tr></thead>", page, re.S).group(1)
        for label in ("янв 26", "фев 26", "мар 26"):
            assert label in header

    def test_categories_are_rows(self, page):
        assert 'class="rowhead">Жильё</td>' in page
        assert 'class="rowhead">Подписки</td>' in page

    def test_months_without_spending_show_a_dash(self, page):
        # Продукты были только в марте — в январе и феврале прочерк.
        assert 'class="num zero">—</td>' in page

    def test_income_is_not_counted_as_expense(self, page):
        # 20000 × 3 — это зарплата, в расходах её быть не должно.
        assert "60 000" not in page

    def test_totals_row(self, page):
        # 6000×3 аренды + 54.9×3 подписки + 900 продуктов
        assert "Всего" in page and _int(19_064.7) in page


class TestSafety:
    def test_escapes_merchant_names(self):
        page = render_html([tx("2026-03-01", 10, '<script>alert("x")</script>')])
        assert "<script>alert" not in page
        assert "&lt;script&gt;" in page

    def test_no_external_requests(self, page):
        # Страница должна открываться без интернета: ни ссылок, ни импортов.
        assert "http://" not in page and "https://" not in page
        assert "@import" not in page and "<script" not in page

    def test_styles_are_inline(self, page):
        assert "<style>" in page and "<link" not in page


class TestThemes:
    def test_both_themes_are_defined(self, page):
        assert "@media (prefers-color-scheme: dark)" in page
        assert ':root[data-theme="dark"]' in page
        assert ':root[data-theme="light"]' in page


class TestDetails:
    def test_month_label_is_short(self):
        assert _month_label("2026-08") == "авг 26"
        assert _month_label("мусор") == "мусор"

    def test_numbers_use_spaces_as_thousands_separator(self):
        # Пробел неразрывный: иначе число ломается по переносу в узкой ячейке.
        assert _int(1234567) == "1\u00a0234\u00a0567"

    def test_partial_month_is_marked(self, items):
        page = render_html(items, today=date(2026, 3, 15))
        assert "неполный месяц" in page

    def test_finished_period_has_no_partial_note(self, page):
        assert "неполный месяц" not in page

    def test_empty_input_says_so(self):
        assert "Нет операций" in render_html([])

    def test_fragment_mode_has_no_document_tags(self, items):
        fragment = render_html(items, standalone=False)
        assert "<!doctype" not in fragment and "<body>" not in fragment
        assert "<style>" in fragment and 'class="sheet"' in fragment

    def test_currency_symbol_follows_config(self, items):
        assert "$" in render_html(items, currency="USD")


class TestCliIntegration:
    def test_report_html_writes_a_file(self, tmp_path, capsys, monkeypatch):
        from expenses.cli import main

        statement = tmp_path / "s.csv"
        statement.write_text(
            "Date,Description,Amount\n2026-03-15,RAMI LEVY,-247.80\n", encoding="utf-8"
        )
        data = tmp_path / "data.jsonl"
        main(["--data", str(data), "import", str(statement)])
        capsys.readouterr()

        monkeypatch.chdir(tmp_path)
        assert main(["--data", str(data), "report", "--months", "12", "--format", "html"]) == 0

        written = tmp_path / "expenses-report.html"
        assert written.exists()
        assert "RAMI LEVY" in written.read_text("utf-8")
        assert "expenses-report.html" in capsys.readouterr().out
