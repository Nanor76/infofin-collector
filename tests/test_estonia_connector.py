from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import requests

from connectors.base import ConnectorState
from connectors.estonia_oam import (
    EstoniaOamConnector,
    classify_estonia_document,
    extract_estonia_date_info,
    parse_estonia_detail,
    parse_estonia_listing,
)
from models import Issuer


LISTING = """
<html><body>
<table>
<tbody>
<tr>
  <td>28.04.2026 16:54:41</td>
  <td>AKTSIASELTS INFORTAR</td>
  <td>Annual financial report</td>
  <td>Audited Annual Report 2025</td>
  <td></td>
  <td><a href="/en/borsiteated/18500">View</a></td>
</tr>
<tr>
  <td>28.02.2026 09:00:00</td>
  <td>AS Tallink Grupp</td>
  <td>Management interim statement or quarterly financial report</td>
  <td>Unaudited interim report for Q4 2025</td>
  <td></td>
  <td><a href="/en/borsiteated/18400">View</a></td>
</tr>
<tr>
  <td>01.04.2026 10:00:00</td>
  <td>AKTSIASELTS INFORTAR</td>
  <td>Annual financial report</td>
  <td>Annual report investor presentation</td>
  <td></td>
  <td><a href="/en/borsiteated/18499">View</a></td>
</tr>
</tbody>
</table>
<nav class="pager">
  <li class="page-item"><a class="page-link" href="?page=1">2</a></li>
</nav>
</body></html>
"""

DETAIL = """
<html><body>
<a href="/sites/default/files/2026-04/infortar_2025_consolidated_audited_esef_esg-en.zip">
infortar_2025_consolidated_audited_esef_esg-en.zip (14.4 MB)
</a>
<a href="/sites/default/files/2026-04/2025_annual_audited_report-en.pdf">
2025_annual_audited_report-en.pdf (5.0 MB)
</a>
</body></html>
"""


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
        params = kwargs.get("params") or {}
        if "/en/borsiteated/" in url:
            return FakeResponse(DETAIL)
        if params.get("page") == "1":
            return FakeResponse("<html><body><table><tbody></tbody></table></body></html>")
        return FakeResponse(LISTING)


def test_listing_and_detail_parsing() -> None:
    page = parse_estonia_listing(LISTING)
    assert page.page_count == 2
    assert len(page.notices) == 3
    annual = page.notices[0]
    assert annual.record_id == "18500"
    assert annual.issuer_name == "AKTSIASELTS INFORTAR"
    assert annual.published_at == date(2026, 4, 28)
    assert annual.detail_url == "https://oam.fi.ee/en/borsiteated/18500"

    detailed = parse_estonia_detail(DETAIL, annual)
    assert [item.attachment_id for item in detailed.files] == [
        "infortar_2025_consolidated_audited_esef_esg-en.zip",
        "2025_annual_audited_report-en.pdf",
    ]
    assert [item.file_format for item in detailed.files] == ["zip", "pdf"]


@pytest.mark.parametrize(
    ("title", "category", "expected"),
    [
        (
            "Audited consolidated annual report 2025",
            "Annual financial report",
            "annual_financial_report",
        ),
        (
            "Semi-annual financial report 2025",
            "Half-yearly financial report",
            "half_year_financial_report",
        ),
        (
            "Interim report for the first quarter of 2026",
            "Management interim statement or quarterly financial report",
            "quarterly_financial_report",
        ),
        (
            "Quarterly report Q3 2025",
            "Management interim statement or quarterly financial report",
            "quarterly_financial_report",
        ),
        (
            "Consolidated Unaudited Interim Report for the 3rd quarter and 9 months of 2025/2026 financial year",
            "Management interim statement or quarterly financial report",
            "quarterly_financial_report",
        ),
        (
            "Unaudited financial report for the first quarter of 2026",
            "Other corporate action",
            "quarterly_financial_report",
        ),
        (
            "Baltic Horizon Fund consolidated unaudited results for Q1 2026",
            "Other corporate action",
            "quarterly_financial_report",
        ),
    ],
)
def test_periodic_classification(
    title: str,
    category: str,
    expected: str,
) -> None:
    assert classify_estonia_document(title, category)[0] == expected


@pytest.mark.parametrize(
    "title",
    [
        "Annual report investor presentation",
        "Annual report press release",
        "Bond annual report and final terms",
        "General meeting and dividend announcement",
        "UCITS annual report factsheet",
        "Share buyback annual financial report",
        "Net asset value of the EfTEN United Property Fund as of 31.05.2026",
        "AS Tallinna Vesi held an investor conference webinar to introduce the results of the first quarter of 2026",
    ],
)
def test_negative_terms_override_periodic_terms(title: str) -> None:
    classification, _, _, negative = classify_estonia_document(
        title,
        "Annual financial report",
    )
    assert classification == "other_regulatory_announcement"
    assert negative


def test_publication_period_and_reporting_year_are_separate() -> None:
    annual = extract_estonia_date_info(
        "Audited annual report for 2025",
        "28.04.2026 16:54:41",
        "Annual financial report",
        "infortar_2025_consolidated_audited_esef_esg-en.zip",
    )
    quarter = extract_estonia_date_info(
        "Interim report for Q1 2026",
        "15.05.2026 08:30:00",
        "Management interim statement or quarterly financial report",
    )
    assert annual["published_at"] == date(2026, 4, 28)
    assert annual["period_end_date"] == date(2025, 12, 31)
    assert annual["reporting_year"] == 2025
    assert quarter["published_at"] == date(2026, 5, 15)
    assert quarter["period_end_date"] == date(2026, 3, 31)
    assert quarter["reporting_year"] == 2026


def test_explicit_period_range_uses_the_end_date() -> None:
    result = extract_estonia_date_info(
        "Half-year report for 01.01.2025 - 30.06.2025",
        "29.08.2025 08:30:00",
        "Half-yearly financial report",
    )
    assert result["period_end_date"] == date(2025, 6, 30)
    assert result["reporting_year"] == 2025


def test_connector_is_source_first_cached_and_materializes_after_match() -> None:
    session = FakeSession()
    connector = EstoniaOamConnector(
        session=session,  # type: ignore[arg-type]
        rate_limit_seconds=0,
        max_pages=2,
    )
    recent = connector.search_recent_documents(
        "Tallinn Stock Exchange",
        since=date(2026, 1, 1),
    )
    issuer = Issuer(
        "AKTSIASELTS INFORTAR",
        "EE3100149394",
        "INF1T",
        "Tallinn Stock Exchange",
        pea_geography_status="eu_candidate",
    )
    materialized = connector.materialize_candidate(recent[0], issuer)
    second_materialized = connector.materialize_candidate(recent[0], issuer)
    resolution = connector.resolve_issuer(issuer)

    listing_gets = [call for call in session.gets if call["url"].endswith("/en/borsiteated")]
    detail_gets = [call for call in session.gets if "/en/borsiteated/" in call["url"]]
    assert len(listing_gets) == 4
    assert len(detail_gets) == 1
    assert connector.scanned_notices == 6
    assert connector.details_visited == 1
    assert connector.cache_hits >= 1
    assert recent[0].metadata["pea_geography_status"] == "eu_candidate"
    assert recent[2].document_type == "other_regulatory_announcement"
    assert {item.metadata["file_format"] for item in materialized} == {
        "zip",
        "pdf",
    }
    assert [item.source_document_id for item in materialized] == [
        "18500:infortar_2025_consolidated_audited_esef_esg-en.zip",
        "18500:2025_annual_audited_report-en.pdf",
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
def test_estonia_live_source() -> None:
    connector = EstoniaOamConnector(
        session=requests.Session(),
        rate_limit_seconds=0.5,
    )
    diagnostic = connector.diagnose()
    annual = connector.discover("annual")
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count > 0
    assert annual.candidates
