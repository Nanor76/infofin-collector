from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests

from connectors.base import ConnectorState
from connectors.lithuania_oam import (
    LithuaniaOamConnector,
    classify_lithuania_document,
    extract_lithuania_date_info,
    parse_lithuania_detail,
    parse_lithuania_listing,
)
from models import Issuer


FIXTURES = Path(__file__).parent / "fixtures"
LISTING = (FIXTURES / "lithuania_oam_listing.html").read_text(encoding="utf-8")
DETAIL = (FIXTURES / "lithuania_oam_detail.html").read_text(encoding="utf-8")


class FakeResponse:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.gets: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.gets.append({"url": url, **kwargs})
        if "/view/" in url:
            return FakeResponse(DETAIL)
        return FakeResponse(
            '<html><meta name="_csrf" content="csrf-token-test"/></html>'
        )

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return FakeResponse(LISTING)


def test_listing_and_detail_parsing() -> None:
    page = parse_lithuania_listing(LISTING)
    assert page.page_count == 1
    assert len(page.notices) == 4
    annual = page.notices[0]
    assert annual.record_id == "473009"
    assert annual.issuer_name == "UAB Kvartalas"
    assert annual.category == "Metinė informacija"
    assert annual.published_at == date(2026, 6, 15)
    assert annual.detail_url == "https://www.oam.lt/view/473009?lang=lt"

    detailed = parse_lithuania_detail(DETAIL, annual)
    assert [item.attachment_id for item in detailed.files] == [
        "344033",
        "344034",
        "6afad9ad-7901-497b-976f-9ebffb6b62cd",
    ]
    assert [item.file_format for item in detailed.files] == [
        "zip",
        "pdf",
        "pdf",
    ]


@pytest.mark.parametrize(
    ("title", "category", "expected"),
    [
        (
            "Audited annual financial statements 2025",
            "Metinė informacija",
            "annual_financial_report",
        ),
        (
            "Ignitis grupės 2025 m. pusmečio finansinė ataskaita",
            "Pusmečio informacija",
            "half_year_financial_report",
        ),
        (
            "AB Pieno Žvaigždės 2026 metų trijų mėnesių neaudituoti finansiniai rezultatai",
            "Tarpinė informacija",
            "quarterly_financial_report",
        ),
    ],
)
def test_periodic_classification(
    title: str,
    category: str,
    expected: str,
) -> None:
    assert classify_lithuania_document(title, category)[0] == expected


@pytest.mark.parametrize(
    "title",
    [
        "Annual report investor presentation",
        "Prospectus and final terms for bond issue",
        "General meeting and dividend announcement",
        "Share buyback programme annual report",
        "Nuosavų akcijų įsigijimas",
        "Pranešimas apie esminį įvykį",
    ],
)
def test_negative_terms_override_periodic_terms(title: str) -> None:
    classification, _, _, negative = classify_lithuania_document(
        title,
        "Metinė informacija",
    )
    assert classification == "other_regulatory_announcement"
    assert negative


def test_lithuanian_governance_attachment_is_rejected() -> None:
    classification, reason, _, negative = classify_lithuania_document(
        "Metinė informacija 2025",
        "Metinė informacija",
        "Korporacinio valdymo ataskaita 2025.pdf",
    )
    assert classification == "other_regulatory_announcement"
    assert "attachment exclusion" in reason
    assert "korporacinio valdymo" in negative


def test_publication_period_and_reporting_year_are_separate() -> None:
    annual = extract_lithuania_date_info(
        "Patikslinta 2025 m. finansinių ataskaitų audito išvada",
        "2026-06-15 18:00:00 EEST",
        "Metinė informacija",
        "UAB Kvartalas FA 2025 12 31 LT_EN.zip",
    )
    quarter = extract_lithuania_date_info(
        "AB Pieno Žvaigždės 2026 metų trijų mėnesių neaudituoti finansiniai rezultatai",
        "2026-06-09 08:00:00 EEST",
        "Tarpinė informacija",
    )
    assert annual["published_at"] == date(2026, 6, 15)
    assert annual["period_end_date"] == date(2025, 12, 31)
    assert annual["reporting_year"] == 2025
    assert quarter["published_at"] == date(2026, 6, 9)
    assert quarter["period_end_date"] == date(2026, 3, 31)
    assert quarter["reporting_year"] == 2026


def test_connector_is_source_first_cached_and_materializes_after_match() -> None:
    session = FakeSession()
    connector = LithuaniaOamConnector(
        session=session,  # type: ignore[arg-type]
        rate_limit_seconds=0,
    )
    recent = connector.search_recent_documents(
        "Vilnius Stock Exchange",
        since=date(2026, 1, 1),
    )
    issuer = Issuer(
        "UAB Kvartalas",
        "LT0000411167",
        "KVART",
        "Vilnius Stock Exchange",
        pea_geography_status="eu_candidate",
    )
    materialized = connector.materialize_candidate(recent[0], issuer)
    second_materialized = connector.materialize_candidate(recent[0], issuer)
    resolution = connector.resolve_issuer(issuer)

    assert len(session.posts) >= 1
    assert len([call for call in session.gets if "/view/" in call["url"]]) == 1
    assert connector.scanned_notices >= 4
    assert connector.details_visited == 1
    assert connector.cache_hits >= 1
    assert recent[0].metadata["pea_geography_status"] == "eu_candidate"
    assert recent[1].document_type == "other_regulatory_announcement"
    assert {item.metadata["file_format"] for item in materialized} == {
        "zip",
        "pdf",
    }
    assert [item.source_document_id for item in materialized] == [
        "473009:344033",
        "473009:344034",
        "473009:6afad9ad-7901-497b-976f-9ebffb6b62cd",
    ]
    assert [item.source_document_id for item in second_materialized] == [
        item.source_document_id for item in materialized
    ]
    assert resolution.found is True
    assert resolution.match_score >= 85.0
    assert all(call.get("verify") is True for call in session.gets + session.posts)


@pytest.mark.skipif(
    __import__("os").environ.get("RUN_LIVE_TESTS") != "1",
    reason="RUN_LIVE_TESTS=1 not set",
)
def test_lithuania_live_source() -> None:
    connector = LithuaniaOamConnector(
        session=requests.Session(),
        rate_limit_seconds=0.5,
    )
    diagnostic = connector.diagnose()
    annual = connector.discover("annual")
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count > 0
    assert annual.candidates