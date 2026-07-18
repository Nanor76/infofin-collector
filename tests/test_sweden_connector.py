from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from connectors.base import ConnectorState
from connectors.sweden_fi import (
    SwedenFiConnector,
    parse_sweden_fi_html,
    SwedenIssuerResolution,
    classify_sweden_document,
)
from models import Issuer

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://finanscentralen.fi.se"
NASDAQ_URL = "https://www.nasdaqomxnordic.com"

def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")

class FakeResponse:
    def __init__(self, *, url: str, text: str = "", status_code: int = 200) -> None:
        self.url = url
        self.text = text
        self.status_code = status_code
        self.content = text.encode()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

class FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_nasdaq = False

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = 10,
        verify: bool = True,
    ) -> FakeResponse:
        self.calls.append(url)
        if "nasdaqomxnordic.com" in url:
            if self.fail_nasdaq:
                raise RuntimeError("Nasdaq error")
            # Simple mock response containing Nasdaq companies list structure
            nasdaq_html = """
            <table>
              <tr>
                <td>Ericsson, Telefonaktiebolaget LM</td>
                <td><a href="/shares/company/ERIC_B?symbol=ERIC B">ERIC B SE0000108656</a></td>
                <td>SE0000108656</td>
              </tr>
            </table>
            """
            return FakeResponse(url=url, text=nasdaq_html)
        if "Detail" in url:
            return FakeResponse(url=url, text=fixture("sweden_fi_detail.html"))
        return FakeResponse(url=url, text=fixture("sweden_fi_listing.html"))

    def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        timeout: int = 10,
        verify: bool = True,
    ) -> FakeResponse:
        self.calls.append(url)
        return FakeResponse(url=url, text=fixture("sweden_fi_listing.html"))

def test_sweden_fi_html_parsing() -> None:
    listing_html = fixture("sweden_fi_listing.html")
    notices = parse_sweden_fi_html(listing_html, base_url=BASE_URL)

    assert len(notices) == 2
    # Notice 1: Ericsson (from table structure, direct links)
    n1 = notices[0]
    assert n1.record_id == "rec-ericsson-123"
    assert n1.issuer_name == "Ericsson, Telefonaktiebolaget LM"
    assert n1.title == "Årsredovisning 2025"
    assert n1.document_type == "annual_financial_report"
    assert len(n1.files) == 2
    assert n1.files[0].file_type == "pdf"
    assert n1.files[1].file_type == "zip"

    # Notice 2: Volvo (pointing to detail page)
    n2 = notices[1]
    assert n2.record_id == "rec-volvo-456"
    assert n2.issuer_name == "Volvo, AB"
    assert n2.title == "Delårsrapport Q1 2026"
    assert n2.document_type == "quarterly_financial_report" # Delårsrapport is interim
    assert len(n2.files) == 0
    assert "Detail" in n2.detail_url

def test_connector_methods_and_nasdaq_enrichment() -> None:
    session = FakeSession()
    connector = SwedenFiConnector(
        session=session,  # type: ignore[arg-type]
        base_url=BASE_URL,
        nasdaq_listed_companies_url=NASDAQ_URL,
        rate_limit_seconds=0.0,
    )

    # Test resolution by name / symbol (Ericsson)
    res = connector.resolve_issuer(symbol="ERIC B", name="Ericsson, Telefonaktiebolaget LM", isin="SE0000108656")
    assert res.found
    assert res.sweden_fi_record_id == "rec-ericsson-123"
    assert res.sweden_nasdaq_company_url == "https://www.nasdaqomxnordic.com/shares/company/ERIC_B?symbol=ERIC B"
    assert res.sweden_pea_country_check == "eu_candidate"
    assert res.sweden_home_member_state == "Sweden"

    # Test search recent documents
    docs = connector.search_recent_documents(market="Nasdaq Stockholm")
    assert len(docs) == 3  # 2 direct from Ericsson + 1 pointing to Volvo detail URL
    assert all(d.source == "sweden_fi" for d in docs)
    assert docs[0].document_type == "annual_financial_report"
    assert docs[0].metadata["file_format"] == "pdf"
    assert docs[2].url == "https://finanscentralen.fi.se/Search/Detail?id=rec-volvo-456"

    # Test discover
    disc = connector.discover(query="Årsredovisning")
    assert len(disc.candidates) == 1
    assert disc.candidates[0].records_count == 2
    assert len(disc.notices) == 2

    # Test diagnose
    diag = connector.diagnose()
    assert diag.state == ConnectorState.READY
    assert diag.total_count == 2
    assert "pdf" in diag.formats
    assert "zip" in diag.formats

def test_nasdaq_graceful_failure() -> None:
    session = FakeSession()
    session.fail_nasdaq = True
    connector = SwedenFiConnector(
        session=session,  # type: ignore[arg-type]
        base_url=BASE_URL,
        nasdaq_listed_companies_url=NASDAQ_URL,
        rate_limit_seconds=0.0,
    )

    # Resolution should still succeed even if Nasdaq fails
    res = connector.resolve_issuer(symbol="ERIC B", name="Ericsson, Telefonaktiebolaget LM", isin="SE0000108656")
    assert res.found
    assert res.sweden_fi_record_id == "rec-ericsson-123"
    assert res.sweden_nasdaq_company_url is None  # no URL resolved, but didn't crash


def test_sweden_document_classification() -> None:
    # Bonava “conditional tender offer for green notes” doit être rejeté.
    cls, reason, pos, neg = classify_sweden_document(
        title="conditional tender offer for green notes",
        category="financial_report",
        url="http://example.com/bonava_notes.pdf"
    )
    assert cls == "other_regulatory_announcement"
    assert any(term in [n.lower() for n in neg] for term in ["notes", "tender offer", "green notes"])

    # “Annual report” doit être accepté.
    cls, reason, pos, neg = classify_sweden_document(
        title="Annual report 2025",
        category="financial_report",
        url="http://example.com/annual_report.pdf"
    )
    assert cls == "annual_financial_report"
    assert not neg

    # Un titre semestriel explicite prime sur une catégorie source annuelle
    # erronée et sur le marqueur calendaire Q2.
    cls, reason, pos, neg = classify_sweden_document(
        title="Halvårsrapport (Q2)",
        category="Annual financial report",
        url="https://finanscentralen.fi.se/search/GetFile.aspx?fid=63222",
    )
    assert cls == "half_year_financial_report"
    assert not neg

    # “Årsredovisning” doit être accepté.
    cls, reason, pos, neg = classify_sweden_document(
        title="Årsredovisning 2025",
        category="financial_report",
        url="http://example.com/arsredovisning.pdf"
    )
    assert cls == "annual_financial_report"
    assert not neg

    # “Bokslutskommuniké” doit être accepté.
    cls, reason, pos, neg = classify_sweden_document(
        title="Bokslutskommuniké 2025",
        category="financial_report",
        url="http://example.com/bokslut.pdf"
    )
    assert cls == "year_end_report"
    assert not neg

    # “Half-year report” doit être accepté.
    cls, reason, pos, neg = classify_sweden_document(
        title="Half-year report 2025",
        category="financial_report",
        url="http://example.com/half_year.pdf"
    )
    assert cls == "half_year_financial_report"
    assert not neg

    # “Notes / bond / tender offer” doit être rejeté.
    for term in ["Notes", "bond", "tender offer"]:
        cls, reason, pos, neg = classify_sweden_document(
            title=f"Some announcement about {term}",
            category="financial_report",
            url=f"http://example.com/{term.lower()}.pdf"
        )
        assert cls == "other_regulatory_announcement"
        assert len(neg) > 0


def test_sweden_date_handling() -> None:
    from datetime import date
    from connectors.sweden_fi import extract_sweden_date_info

    # 1. Search.aspx (issuer-mode grid) parsing mock test
    info_issuer_mode = extract_sweden_date_info("Annual Report 2025", "2025", None)
    assert info_issuer_mode["published_at"] is None
    assert info_issuer_mode["period_end_date"] == date(2025, 12, 31)
    assert info_issuer_mode["reporting_year"] == 2025
    assert info_issuer_mode["date_confidence"] == "low"
    assert "Absence de date d'enregistrement" in info_issuer_mode["date_extraction_reason"]

    # 2. SearchByRegistrationDate.aspx behavior (registration date present)
    info_recent_mode = extract_sweden_date_info("Annual Report 2025", None, date(2026, 3, 15))
    assert info_recent_mode["published_at"] == date(2026, 3, 15)
    assert info_recent_mode["period_end_date"] == date(2025, 12, 31)
    assert info_recent_mode["reporting_year"] == 2025
    assert info_recent_mode["date_confidence"] == "high"

    # 3. Quarterly report date extraction in recent mode
    info_q1 = extract_sweden_date_info("Interim Report Q1 2026", None, date(2026, 5, 10))
    assert info_q1["published_at"] == date(2026, 5, 10)
    assert info_q1["period_end_date"] == date(2026, 3, 31)
    assert info_q1["reporting_year"] == 2026

    # 4. Half-year report in recent mode
    info_h1 = extract_sweden_date_info("Half-Year Report 2026", None, date(2026, 8, 20))
    assert info_h1["published_at"] == date(2026, 8, 20)
    assert info_h1["period_end_date"] == date(2026, 6, 30)
    assert info_h1["reporting_year"] == 2026

