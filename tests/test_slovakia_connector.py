from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from connectors.slovakia_nbs_ceri import (
    SlovakiaNbsCeriConnector,
    build_ceri_download_url,
    build_ceri_search_payload,
    classify_slovakia_document,
    extract_slovakia_date_info,
    parse_slovakia_listing,
)


FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "slovakia_ceri_search.html").read_text(encoding="utf-8")


def test_build_ceri_download_url() -> None:
    filename, url = build_ceri_download_url("img00014449.zip")
    assert filename == "00014449.zip"
    assert url == "https://ceri.nbs.sk/static/data/00014/00014449.zip"


def test_build_ceri_search_payload() -> None:
    payload = build_ceri_search_payload(
        from_date=date(2025, 1, 1),
        to_date=date(2026, 6, 18),
        category="1",
    )
    assert payload["qissuer"] == "0"
    assert payload["qcathegory"] == "1"
    assert payload["qrdfrom"] == "01.01.2025"
    assert payload["qrdto"] == "18.06.2026"
    assert payload["search_set"] == "Hľadaj"


def test_listing_parsing_finds_tmr_annual_report() -> None:
    page = parse_slovakia_listing(SEARCH_HTML)
    annual = next(
        notice
        for notice in page.notices
        if notice.files[0].filename == "00014449.zip"
    )
    assert annual.issuer_name == "Tatry mountain resorts, a.s."
    assert annual.files[0].filename == "00014449.zip"
    assert annual.files[0].file_format == "zip"
    assert annual.published_at == date(2026, 3, 2)


@pytest.mark.parametrize(
    ("title", "category", "expected"),
    [
        (
            "Ročná finančná správa 2024/25",
            "Ročná finančná správa - zverejnenie",
            "annual_financial_report",
        ),
        (
            "Polročná finančná správa spoločnosti",
            "Polročná finančná správa - zverejnenie",
            "half_year_financial_report",
        ),
        (
            "Štvrťročná finančná správa Q1",
            "Predbežné vyhlásenie alebo Štvrťročná finančná správa",
            "quarterly_financial_report",
        ),
    ],
)
def test_periodic_classification(
    title: str,
    category: str,
    expected: str,
) -> None:
    assert classify_slovakia_document(title, category)[0] == expected


@pytest.mark.parametrize(
    "title",
    [
        "Oznámenie o konaní VZ",
        "Transakcie manažérov",
        "Zmeny v podieloch na hlasovacích právach emitenta",
        "Ročná finančná správa - inzerát",
        "Informácie pre držiteľov dlhových CP",
    ],
)
def test_non_periodic_classification(title: str) -> None:
    classification, _, _, negative = classify_slovakia_document(
        title,
        "Oznámenie o konaní VZ",
    )
    assert classification == "other_regulatory_announcement"
    assert negative


def test_publication_period_and_reporting_year_are_separate() -> None:
    annual = extract_slovakia_date_info(
        "Ročná finančná správa 2024/25",
        "02.03.2026 22:35",
        "Ročná finančná správa - zverejnenie",
        "00014449.zip",
    )
    half_year = extract_slovakia_date_info(
        "Polročná finančná správa k 30.06.2025",
        "31.07.2025 12:33",
        "Polročná finančná správa - zverejnenie",
        "00014122.pdf",
    )
    assert annual["published_at"] == date(2026, 3, 2)
    assert annual["reporting_year"] == 2025
    assert annual["period_end_date"] == date(2025, 12, 31)
    assert half_year["published_at"] == date(2025, 7, 31)
    assert half_year["period_end_date"] == date(2025, 6, 30)


def test_materialize_candidate_without_extra_http() -> None:
    page = parse_slovakia_listing(SEARCH_HTML)
    annual = next(
        notice for notice in page.notices if notice.files[0].filename.endswith(".zip")
    )
    connector = SlovakiaNbsCeriConnector(session=object())
    connector._notice_cache[annual.record_id] = annual
    candidate = connector._notice_candidate(annual)
    from models import Issuer

    issuer = Issuer(
        "Tatry mountain resorts, a.s.",
        "SK1120010287",
        "TMR",
        "Bratislava Stock Exchange",
    )
    materialized = connector.materialize_candidate(candidate, issuer)
    assert len(materialized) == 1
    assert materialized[0].url.endswith("00014449.zip")
    assert materialized[0].document_type == "annual_financial_report"
    assert connector.details_visited == 0