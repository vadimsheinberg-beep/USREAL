"""Распознавание участков со строениями: снос, усиление, расселение."""

import pytest

from landtender.renewal import (
    DEMOLITION,
    EXISTING_STRUCTURE,
    PINUI_BINUI,
    PRESERVATION,
    TAMA_38,
    URBAN_RENEWAL,
    badge,
    classify,
    classify_text,
    has_existing_structure,
)


class TestClassifyText:
    def test_pinui_binui(self):
        assert classify_text("מתחם פינוי בינוי ברמת גן") == PINUI_BINUI

    def test_pinui_binui_with_hyphen(self):
        assert classify_text("פינוי-בינוי") == PINUI_BINUI

    def test_pinui_uvinui(self):
        assert classify_text("פרויקט פינוי ובינוי") == PINUI_BINUI

    @pytest.mark.parametrize(
        "text",
        ['תמ"א 38', "תמ״א 38", "תמא 38", "TAMA 38", "חיזוק לפי תמ\"א 38"],
    )
    def test_tama_38_in_various_spellings(self, text):
        assert classify_text(text) == TAMA_38

    def test_demolition(self):
        assert classify_text("הריסה ובנייה מחדש") == DEMOLITION

    def test_preservation(self):
        assert classify_text("מבנה לשימור") == PRESERVATION

    def test_urban_renewal(self):
        assert classify_text("התחדשות עירונית בשכונה") == URBAN_RENEWAL

    def test_existing_structure(self):
        assert classify_text("מגרש עם מבנה קיים") == EXISTING_STRUCTURE

    def test_old_structure(self):
        assert classify_text("מבנה ישן") == EXISTING_STRUCTURE

    def test_empty_plot_is_not_classified(self):
        assert classify_text("מגרש ריק למגורים") is None

    def test_no_text_at_all(self):
        assert classify_text(None, None) is None

    def test_specific_term_wins_over_general(self):
        """פינוי בינוי конкретнее, чем общее «городское обновление»."""
        assert classify_text("התחדשות עירונית - פינוי בינוי") == PINUI_BINUI

    def test_searches_across_all_given_texts(self):
        assert classify_text("מגורים", None, 'הריסת מבנה קיים') == DEMOLITION


class TestHasExistingStructure:
    def test_built_area_proves_a_structure(self):
        assert has_existing_structure(1200.0, None) is True

    def test_renewal_term_implies_a_structure(self):
        assert has_existing_structure(None, PINUI_BINUI) is True

    def test_zero_built_area_alone_is_unknown(self):
        """Ноль у рм"י значит и «нет застройки», и «нет данных»."""
        assert has_existing_structure(0.0, None) is None

    def test_nothing_known(self):
        assert has_existing_structure(None, None) is None


class TestClassify:
    def test_terms_and_area_together(self):
        kind, structure = classify(purpose="פינוי בינוי", built_area=3000.0)
        assert (kind, structure) == (PINUI_BINUI, True)

    def test_built_area_without_terms_still_flags_a_structure(self):
        kind, structure = classify(purpose="מגורים", built_area=850.0)
        assert (kind, structure) == (EXISTING_STRUCTURE, True)

    def test_empty_plot_stays_clean(self):
        assert classify(purpose="מגורים", built_area=0.0) == (None, None)

    def test_comments_are_searched_too(self):
        kind, _ = classify(purpose="מגורים", comments='נדרשת הריסה של המבנה')
        assert kind == DEMOLITION

    def test_tender_name_is_searched(self):
        kind, _ = classify(tender_name="מכרז התחדשות עירונית 12/2026")
        assert kind == URBAN_RENEWAL


class TestBadge:
    def test_known_kind_has_a_badge(self):
        assert badge(PINUI_BINUI) == "פינוי בינוי"
        assert badge(DEMOLITION) == "снос"

    def test_empty_plot_has_no_badge(self):
        assert badge(None) is None
