from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests

from connectors.base import ConnectorState
from connectors.latvia_oam import (
    LatviaOamConnector,
    classify_latvia_document,
    extract_latvia_date_info,
    parse_latvia_detail,
    parse_latvia_listing,
)
from models import Issuer


FIXTURES = Path(__file__).parent / "fixtures"
LISTING = (FIXTURES / "latvia_oam_listing.html").read_text(encoding="utf-8")
DETAIL = (FIXTURES / "latvia_oam_detail.html").read_text(encoding="utf-8")


class FakeResponse:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.gets: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.gets.append({"url": url, **kwargs})
        params = kwargs.get("params") or []
        if "csridocumentsdetails" in url:
            return FakeResponse(DETAIL)
        if ("start", "20") in list(params):
            return FakeResponse("<html><body><table><tbody></tbody></table></body></html>")
        return FakeResponse(LISTING)


def test_listing_and_detail_parsing() -> None:
    page = parse_latvia_listing(LISTING)
    assert page.page_count == 2
    assert len(page.notices) == 3
    annual = page.notices[0]
    assert annual.record_id == "24885"
    assert annual.issuer_name == "Latvenergo, AS"
    assert annual.category == "Annual financial reports and audit reports"
    assert annual.published_at == date(2026, 4, 29)
    assert annual.detail_url == "https://csri.investinfo.lv/?view=csridocumentsdetails&id=24885"

    detailed = parse_latvia_detail(DETAIL, annual)
    assert [item.attachment_id for item in detailed.files] == [
        "22080",
        "22081",
        "22082:xhtml",
    ]
    assert [item.file_format for item in detailed.files] == [
        "pdf",
        "zip",
        "xhtml",
    ]


@pytest.mark.parametrize(
    ("title", "category", "expected"),
    [
        (
            "Audited consolidated annual report 2025",
            "Annual financial reports and audit reports",
            "annual_financial_report",
        ),
        (
            "Semi-annual financial report 2025",
            "Half-year financial reports",
            "half_year_financial_report",
        ),
        (
            "Interim report for Q1 2026",
            "Periodic regulated information",
            "quarterly_financial_report",
        ),
        (
            "Quarterly report Q3 2025",
            "Periodic regulated information",
            "quarterly_financial_report",
        ),
        (
            "Storent Holding AS interim report for 6 months of 2025",
            "Periodic regulated information",
            "half_year_financial_report",
        ),
        (
            "JSC Development Finance Institution Altum unaudited interim condensed report for the six months period ending 30 June 2025",
            "Periodic regulated information",
            "half_year_financial_report",
        ),
    ],
)
def test_periodic_classification(
    title: str,
    category: str,
    expected: str,
) -> None:
    assert classify_latvia_document(title, category)[0] == expected


@pytest.mark.parametrize(
    "title",
    [
        "Annual report investor presentation",
        "Annual report press release",
        "Bond annual report and final terms",
        "General meeting and dividend announcement",
        "Share buyback annual financial report",
        "UCITS annual report factsheet",
    ],
)
def test_negative_terms_override_periodic_terms(title: str) -> None:
    classification, _, _, negative = classify_latvia_document(
        title,
        "Annual financial reports and audit reports",
    )
    assert classification == "other_regulatory_announcement"
    assert negative


def test_publication_date_announcement_is_not_a_half_year_report() -> None:
    classification, _, _, negative = classify_latvia_document(
        (
            "BluOr Bank AS announces the date of publication of the financial "
            "report for the first half of 2026"
        ),
        "Half-year financial reports",
    )

    assert classification == "other_regulatory_announcement"
    assert negative


def test_latvian_governance_attachment_is_rejected() -> None:
    classification, reason, _, negative = classify_latvia_document(
        "Latvenergo koncerna revidētie 2025. gada rezultāti",
        "Annual financial reports and audit reports",
        "3_Latvenergo Korporativas parvaldibas zinojums 2025.pdf",
    )
    assert classification == "other_regulatory_announcement"
    assert "attachment exclusion" in reason
    assert "korporativas parvaldibas" in negative


def test_publication_period_and_reporting_year_are_separate() -> None:
    annual = extract_latvia_date_info(
        "Latvenergo Group Consolidated and Latvenergo AS Annual Report 2025",
        "2026-04-29 09:53:35",
        "Annual financial reports and audit reports",
        "Latvenergo_Annual_Report_2025.pdf",
    )
    quarter = extract_latvia_date_info(
        "AS DelfinGroup interim report for Q1 2026",
        "2026-05-30 08:00:00",
        "Periodic regulated information",
    )
    assert annual["published_at"] == date(2026, 4, 29)
    assert annual["period_end_date"] == date(2025, 12, 31)
    assert annual["reporting_year"] == 2025
    assert quarter["published_at"] == date(2026, 5, 30)
    assert quarter["period_end_date"] == date(2026, 3, 31)
    assert quarter["reporting_year"] == 2026


def test_explicit_period_range_uses_the_end_date() -> None:
    result = extract_latvia_date_info(
        "Half-year report for 01.01.2025 - 30.06.2025",
        "2025-08-29 08:30:00",
        "Half-year financial reports",
    )
    assert result["period_end_date"] == date(2025, 6, 30)
    assert result["reporting_year"] == 2025


def test_connector_is_source_first_cached_and_materializes_after_match() -> None:
    session = FakeSession()
    connector = LatviaOamConnector(
        session=session,  # type: ignore[arg-type]
        rate_limit_seconds=0,
        max_pages=2,
    )
    recent = connector.search_recent_documents(
        "Riga Stock Exchange",
        since=date(2026, 1, 1),
    )
    issuer = Issuer(
        "Latvenergo, AS",
        "LV0000870129",
        "ELEK024227A",
        "Riga Stock Exchange",
        pea_geography_status="eu_candidate",
    )
    materialized = connector.materialize_candidate(recent[0], issuer)
    second_materialized = connector.materialize_candidate(recent[0], issuer)
    resolution = connector.resolve_issuer(issuer)

    listing_gets = [call for call in session.gets if call["url"].endswith("/lv/")]
    detail_gets = [call for call in session.gets if "csridocumentsdetails" in call["url"]]
    assert len(listing_gets) >= 6
    assert len(detail_gets) == 1
    assert connector.scanned_notices >= 9
    assert connector.details_visited == 1
    assert connector.cache_hits >= 1
    assert recent[0].metadata["pea_geography_status"] == "eu_candidate"
    assert recent[2].document_type == "other_regulatory_announcement"
    assert {item.metadata["file_format"] for item in materialized} == {
        "pdf",
        "zip",
        "xhtml",
    }
    assert [item.source_document_id for item in materialized] == [
        "24885:22080",
        "24885:22081",
        "24885:22082:xhtml",
    ]
    assert [item.source_document_id for item in second_materialized] == [
        item.source_document_id for item in materialized
    ]
    assert resolution.found is True
    assert resolution.match_score == 100
    assert all(call["verify"] is True for call in session.gets)


@pytest.mark.skipif(
    __import__("os").environ.get("RUN_LIVE_TESTS") != "1",
    reason="RUN_LIVE_TESTS=1 not set",
)
def test_latvia_live_source() -> None:
    connector = LatviaOamConnector(
        session=requests.Session(),
        rate_limit_seconds=0.5,
    )
    diagnostic = connector.diagnose()
    annual = connector.discover("annual")
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count > 0
    assert annual.candidates
