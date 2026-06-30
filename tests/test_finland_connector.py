from __future__ import annotations

from datetime import date
import pytest

from connectors.finland_oam import (
    FinlandNotice,
    FinlandOamConnector,
    classify_finland_document,
    extract_finland_date_info,
)


@pytest.mark.parametrize(
    ("title", "category", "expected"),
    [
        ("Annual Report 2025", "Tilinpäätös ja toimintakertomus", "annual_financial_report"),
        ("Vuosikertomus 2025", "Vuosikertomus", "annual_financial_report"),
        ("Half-year report Q2", "Puolivuosikatsaus", "half_year_financial_report"),
        ("Interim Report Q3 2025", "Osavuosikatsaus (Q1 and Q3)", "quarterly_financial_report"),
        ("Year-end report 2025", "Tilinpäätöstiedote", "year_end_report"),
    ],
)
def test_strict_positive_classification(title: str, category: str, expected: str) -> None:
    classification, reason, positive, negative = classify_finland_document(title, category)
    assert classification == expected
    assert not negative


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("Major holding announcement Matas", "Liputusilmoitus"),
        ("Manager transactions", "Johdon kaupat"),
        ("Share buyback program details", "Omien osakkeiden takaisinosto"),
        ("Financial calendar 2026", "Tulosjulkistamisajankohdat"),
        ("General meeting notice 2026", "Yhtiökokouskutsu"),
    ],
)
def test_strict_negative_classification(title: str, category: str) -> None:
    classification, reason, positive, negative = classify_finland_document(title, category)
    assert classification == "other_regulatory_announcement"
    assert negative


def test_finland_date_extraction() -> None:
    # Title containing year
    res = extract_finland_date_info("Annual report 2025", "2026-06-14 21:30:00 EEST", "Tilinpäätös")
    assert res["published_at"] == date(2026, 6, 14)
    assert res["reporting_year"] == 2025
    assert res["period_end_date"] == date(2025, 12, 31)

    # Half year report Q2 2025
    res = extract_finland_date_info("Half-year report 2025", "2025-08-15 08:00:00 EEST", "Puolivuosikatsaus")
    assert res["published_at"] == date(2025, 8, 15)
    assert res["reporting_year"] == 2025
    assert res["period_end_date"] == date(2025, 6, 30)


def test_search_recent_documents_uses_limit_as_page_size() -> None:
    class RecordingFinlandConnector(FinlandOamConnector):
        def __init__(self) -> None:
            self.lookback_days = 7
            self.recorded_page_size: int | None = None
            self._scanned_notices = 0
            self._details_visited = 0

        def _search(self, **kwargs: object) -> list[FinlandNotice]:
            self.recorded_page_size = int(kwargs["page_size"])
            return []

    connector = RecordingFinlandConnector()

    connector.search_recent_documents(
        "Nasdaq Helsinki",
        since=date(2026, 6, 1),
        limit=250,
    )

    assert connector.recorded_page_size == 250
