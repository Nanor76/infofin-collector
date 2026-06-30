from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests

from connectors.base import ConnectorState
from connectors.slovenia_oam import (
    SloveniaOamConnector,
    classify_slovenia_document,
    extract_slovenia_date_info,
    parse_slovenia_detail,
    parse_slovenia_listing,
)
from models import Issuer


FIXTURES = Path(__file__).parent / "fixtures"
LISTING = (FIXTURES / "slovenia_oam_listing.html").read_text(encoding="utf-8")
DETAIL = (FIXTURES / "slovenia_oam_detail.html").read_text(encoding="utf-8")


class FakeResponse:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.posts.append({"url": url, **kwargs})
        if kwargs["data"]["field.page_no"] == "2":
            return FakeResponse("<html><body></body></html>")
        return FakeResponse(LISTING)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.gets.append({"url": url, **kwargs})
        return FakeResponse(DETAIL)


def test_listing_and_detail_parsing() -> None:
    page = parse_slovenia_listing(LISTING)
    assert page.page_count == 2
    assert len(page.notices) == 3
    annual = page.notices[0]
    assert annual.record_id == "39591"
    assert annual.issuer_lei == "5493003GE7UJGPQAMN79"
    assert annual.published_at == date(2026, 4, 17)
    assert annual.received_at == date(2026, 4, 16)
    assert annual.report_number == "AR-15/26"

    detailed = parse_slovenia_detail(DETAIL, annual)
    assert [item.attachment_id for item in detailed.files] == ["29439", "29440"]
    assert [item.file_format for item in detailed.files] == ["xbri", "pdf"]
    assert detailed.files[0].download_url == (
        "https://www.oam.si/file.aspx?AttachmentID=29439"
    )


@pytest.mark.parametrize(
    ("title", "category", "expected"),
    [
        (
            "Audited consolidated annual report 2025",
            "Annual financial and audit reports",
            "annual_financial_report",
        ),
        (
            "Semi-annual financial report 2025",
            "Half yearly financial reports and audit reports/limited reviews",
            "half_year_financial_report",
        ),
        (
            "Interim report for the first quarter of 2026",
            "Half yearly financial reports and audit reports/limited reviews",
            "quarterly_financial_report",
        ),
        (
            "Quarterly report Q3 2025",
            "Quarterly report",
            "quarterly_financial_report",
        ),
    ],
)
def test_periodic_classification(
    title: str,
    category: str,
    expected: str,
) -> None:
    assert classify_slovenia_document(title, category)[0] == expected


@pytest.mark.parametrize(
    "title",
    [
        "Annual report investor presentation",
        "Annual report press release",
        "Bond annual report and final terms",
        "General meeting and dividend announcement",
        "UCITS annual report factsheet",
    ],
)
def test_negative_terms_override_periodic_terms(title: str) -> None:
    classification, _, _, negative = classify_slovenia_document(
        title,
        "Annual financial and audit reports",
    )
    assert classification == "other_regulatory_announcement"
    assert negative


def test_public_presentation_is_rejected_in_periodic_category() -> None:
    classification, _, _, negative = classify_slovenia_document(
        "Javna predstavitev poslovanja v prvem polletju 2025",
        "Half yearly financial reports and audit reports/limited reviews",
    )
    assert classification == "other_regulatory_announcement"
    assert negative


def test_publication_period_and_reporting_year_are_separate() -> None:
    annual = extract_slovenia_date_info(
        "Audited annual report for 2025",
        "4/17/2026 8:30 AM",
        "Annual financial and audit reports",
        "5493003GE7UJGPQAMN79-2025-12-31-1-en.xbri",
    )
    quarter = extract_slovenia_date_info(
        "Interim report for the first quarter of 2026",
        "5/15/2026 8:30 AM",
        "Half yearly financial reports and audit reports/limited reviews",
    )
    assert annual["published_at"] == date(2026, 4, 17)
    assert annual["period_end_date"] == date(2025, 12, 31)
    assert annual["reporting_year"] == 2025
    assert quarter["published_at"] == date(2026, 5, 15)
    assert quarter["period_end_date"] == date(2026, 3, 31)
    assert quarter["reporting_year"] == 2026


def test_explicit_period_range_uses_the_end_date() -> None:
    result = extract_slovenia_date_info(
        "Poročilo za obdobje 1.1.2025 - 30.6.2025",
        "8/29/2025 8:30 AM",
        "Half yearly financial reports and audit reports/limited reviews",
    )
    assert result["period_end_date"] == date(2025, 6, 30)
    assert result["reporting_year"] == 2025


def test_connector_is_source_first_cached_and_materializes_after_match() -> None:
    session = FakeSession()
    connector = SloveniaOamConnector(
        session=session,  # type: ignore[arg-type]
        rate_limit_seconds=0,
        max_pages=2,
    )
    recent = connector.search_recent_documents(
        "Ljubljana Stock Exchange",
        since=date(2026, 1, 1),
    )
    issuer = Issuer(
        "TELEKOM SLOVENIJE, d.d., Ljubljana",
        "SI0031104290",
        "TLSG",
        "Ljubljana Stock Exchange",
    )
    materialized = connector.materialize_candidate(recent[0], issuer)
    resolution = connector.resolve_issuer(issuer)

    assert len(session.posts) == 4
    assert len(session.gets) == 1
    assert connector.scanned_notices == 6
    assert connector.details_visited == 1
    assert recent[0].metadata["issuer_lei"] == "5493003GE7UJGPQAMN79"
    assert recent[2].document_type == "other_regulatory_announcement"
    assert {item.metadata["file_format"] for item in materialized} == {
        "xbri",
        "pdf",
    }
    assert materialized[0].source_document_id == "39591:29439"
    assert resolution.found is True
    assert resolution.match_score == 100
    assert all(call["verify"] is True for call in session.posts + session.gets)


@pytest.mark.skipif(
    __import__("os").environ.get("RUN_LIVE_TESTS") != "1",
    reason="RUN_LIVE_TESTS=1 not set",
)
def test_slovenia_live_source() -> None:
    connector = SloveniaOamConnector(
        session=requests.Session(),
        rate_limit_seconds=0.5,
    )
    diagnostic = connector.diagnose()
    annual = connector.discover("annual")
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count > 0
    assert annual.candidates
