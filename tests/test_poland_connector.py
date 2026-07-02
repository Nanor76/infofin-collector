from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from connectors.base import ConnectorState
from connectors.poland_knf_oam import (
    PolandKnfOamConnector,
    classify_poland_document,
    classify_poland_notice,
    parse_poland_detail,
    parse_poland_listing,
)
from models import Issuer


FIXTURES = Path(__file__).parent / "fixtures"
LISTING = (FIXTURES / "poland_knf_listing.html").read_text(
    encoding="utf-8"
)
DETAIL = (FIXTURES / "poland_knf_detail.html").read_text(
    encoding="utf-8"
)


class FakeResponse:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        if "/0/" in url:
            return FakeResponse(DETAIL)
        return FakeResponse(LISTING)


def test_listing_and_detail_parsing() -> None:
    page = parse_poland_listing(
        LISTING,
        page_url="https://moam.knf.gov.pl/moam.nsf",
    )
    detail = parse_poland_detail(
        DETAIL,
        record_id="1FB8C7935B0B37A2C1258E13005B5DD4",
        detail_url=(
            "https://moam.knf.gov.pl/moam.nsf/0/"
            "1FB8C7935B0B37A2C1258E13005B5DD4"
        ),
    )

    assert page.total_count == 2
    assert page.notices[0].report_code == "QSr"
    assert page.notices[0].package_url == (
        "https://moam.knf.gov.pl/mOAM/2026/20260615/"
        "1n3mtukgq1_Raport.zip"
    )
    assert detail.published_at == date(2026, 6, 15)
    assert detail.reporting_year == 2026
    assert detail.report_code == "QSr"


def test_strict_poland_classification() -> None:
    assert classify_poland_document("RR")[0] == "annual_financial_report"
    assert classify_poland_document("SRR")[0] == "annual_financial_report"
    assert classify_poland_document("PSr")[0] == (
        "half_year_financial_report"
    )
    assert classify_poland_document("QSr")[0] == (
        "quarterly_financial_report"
    )
    for code in ("RB", "RB-W", "RB_ASO", "UNI-EN", "R", "QS", ""):
        classification, _, positive, negative = (
            classify_poland_document(code)
        )
        assert classification == "other_regulatory_announcement"
        assert positive == []
        assert negative == ([code] if code else [])


def test_poland_title_fallback_classifies_foreign_periodic_reports() -> None:
    examples = [
        (
            "RB-W",
            "MOL Group releases 2025 Half Year Report",
            "ghdul2mk91_Raport.zip",
            "half_year_financial_report",
        ),
        (
            "UNI-EN",
            "Entity & Consolidated Financial Report for Q2 and H1 2025",
            "21fskmjss1_Raport.zip",
            "half_year_financial_report",
        ),
        (
            "UNI-EN",
            "ANNUAL REPORT FOR THE YEAR ENDED 30 JUNE 2025",
            "FY2025_Kernel_Annual_Report.pdf",
            "annual_financial_report",
        ),
        (
            "RB-W",
            "EUBG_CONSOLIDATED_REPORT_Q3_2025",
            "EUBG_CONSOLIDATED_REPORT_Q3_2025.pdf",
            "quarterly_financial_report",
        ),
    ]
    for code, title, filename, expected in examples:
        classification, reason, positive, negative = classify_poland_notice(
            code,
            title,
            filename,
        )
        assert classification == expected
        assert "title fallback" in reason
        assert positive
        assert negative == []


def test_poland_title_fallback_rejects_common_false_positives() -> None:
    examples = [
        (
            "UNI-EN",
            "KERNEL ANNOUNCES DATE FOR Q1 FY2026 OPERATIONS UPDATE PUBLICATION",
        ),
        ("RB-W", "Q3 2025 revenues"),
        ("RB-W", "EUBG_Bond_ISIN BG2100002224_Q2_2025"),
        ("RB", "Zaliczka na poczet dywidendy Q3 2025"),
        ("RB", "Zmiana terminu przekazania raportu półrocznego"),
    ]
    for code, title in examples:
        classification, _, _, negative = classify_poland_notice(code, title)
        assert classification == "other_regulatory_announcement"
        assert negative


def test_source_first_filters_before_detail_and_materializes_periodic() -> None:
    session = FakeSession()
    connector = PolandKnfOamConnector(
        session=session,  # type: ignore[arg-type]
        rate_limit_seconds=0,
    )
    candidates = connector.search_recent_documents(
        "Warsaw Stock Exchange",
        since=date.today(),
        limit=10,
    )
    periodic = next(
        candidate
        for candidate in candidates
        if candidate.document_type == "quarterly_financial_report"
    )
    rejected = next(
        candidate
        for candidate in candidates
        if candidate.document_type == "other_regulatory_announcement"
    )
    issuer = Issuer(
        "MODIVO Spółka Akcyjna",
        "PLCCC0000016",
        "MODIVO",
        "Warsaw Stock Exchange",
    )

    assert len(session.calls) == 1
    assert connector.materialize_candidate(rejected, issuer) == [rejected]
    assert len(session.calls) == 1
    materialized = connector.materialize_candidate(periodic, issuer)[0]
    assert len(session.calls) == 2
    assert connector.details_visited == 1
    assert materialized.published_at == date(2026, 6, 15)
    assert materialized.reporting_year == 2026
    assert materialized.metadata["report_code"] == "QSr"
    diagnostic = connector.diagnose()
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.http_calls == 3
