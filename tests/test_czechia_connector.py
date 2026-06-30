from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from connectors.czechia_cnb_curi import (
    CzechiaCnbCuriConnector,
    _extract_czechia_date_info,
    _parse_czech_date,
    _extract_isins,
)
from connectors.base import ConnectorState
from models import Issuer

FIXTURE = Path(__file__).parent / "fixtures" / "czechia_cnb_feed.xml"


class FakeResponse:
    status_code = 200

    def __init__(self, content: str) -> None:
        self.text = content

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs.get("data")))
        return FakeResponse("")

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        data = kwargs.get("data")
        self.calls.append(("POST", url, data))
        
        # If polling URL, return Report Completed
        if "/xmlpserver/servlet/xdo" in url:
            if data and data.get("finalRequest") == "true":
                # Final XML fetch
                return FakeResponse(FIXTURE.read_text(encoding="utf-8"))
            else:
                # Status poll
                return FakeResponse("Report Completed")
        
        # Initial search query, return intermediate page containing JS with polling path
        if "R1_RES.xdo" in url:
            mock_js = 'var url = "/xmlpserver/servlet/xdo?_xdo=%2FOAM_CNB_CZ%2FR1_RES.xdo&fromLoadingPage=true&_sTkn=mockToken&_id=mockId&_dFlag=false";'
            return FakeResponse(mock_js)

        return FakeResponse("")


def test_parse_czech_date() -> None:
    assert _parse_czech_date("31.05.2026 18:47:38") == date(2026, 5, 31)
    assert _parse_czech_date("18.05.2026") == date(2026, 5, 18)
    assert _parse_czech_date("2026-05-15") == date(2026, 5, 15)
    assert _parse_czech_date("") is None


def test_extract_isins() -> None:
    assert _extract_isins("AKESO_Funding_CZ0003573875_Vyrocni_zprava.zip") == ["CZ0003573875"]
    assert _extract_isins("No ISIN here.pdf") == []


def test_extract_czechia_date_info() -> None:
    # 1. Explicit date in title/filename
    res = _extract_czechia_date_info(
        title="Annual financial report",
        filename="Banka-CREDITAS-2025-12-31-cs.zip",
        published_date=date(2026, 5, 18),
        is_annual=True,
    )
    assert res["period_end_date"] == date(2025, 12, 31)
    assert res["reporting_year"] == 2025
    assert res["confidence"] == "high"

    # 2. Extrapolated from year in title/filename
    res2 = _extract_czechia_date_info(
        title="Annual financial report",
        filename="AKESO_Funding_Vyrocni_zprava_2025.zip",
        published_date=date(2026, 5, 31),
        is_annual=True,
    )
    assert res2["period_end_date"] == date(2025, 12, 31)
    assert res2["reporting_year"] == 2025
    assert res2["confidence"] == "high"

    # 3. Fallback to published date minus 1 year if first half of the year
    res3 = _extract_czechia_date_info(
        title="Annual financial report",
        filename="Simple_Annual_Report.zip",
        published_date=date(2026, 5, 31),
        is_annual=True,
    )
    assert res3["period_end_date"] is None
    assert res3["reporting_year"] == 2025
    assert res3["confidence"] == "low"


def test_connector_methods() -> None:
    session = FakeSession()
    connector = CzechiaCnbCuriConnector(
        session=session,  # type: ignore[arg-type]
        rate_limit_seconds=0,
    )

    recent = connector.search_recent_documents(
        "Prague Stock Exchange",
        since=date(2026, 5, 1),
        limit=10,
    )

    # 3 candidates expected: 1 from AKESO (zip), 2 from Banka CREDITAS (1 zip, 1 pdf). docx is other_regulatory_announcement.
    # Wait, search_recent_documents returns all candidates (even docx, classified accordingly).
    # In XML fixture, S21246564 has 1 file (zip), S21236010 has 3 files (zip, pdf, docx).
    # Total candidates = 4.
    assert len(recent) == 4
    
    # Check details of candidates
    c_zip_creditas = [c for c in recent if "31570010000000004266-2025-12-31-1-cs.zip" in c.title][0]
    assert c_zip_creditas.document_type == "annual_financial_report"
    assert c_zip_creditas.period_end_date == date(2025, 12, 31)
    assert c_zip_creditas.reporting_year == 2025

    c_docx_creditas = [c for c in recent if ".docx" in c.title][0]
    assert c_docx_creditas.document_type == "other_regulatory_announcement"

    # Resolve issuer test
    resolution = connector.resolve_issuer(
        Issuer(
            name="Banka CREDITAS a.s.",
            isin="CZ0008042488",
            symbol="63492555",
            market="Prague Stock Exchange",
        )
    )
    assert resolution.found is True
    assert resolution.czechia_cnb_curi_name == "Banka CREDITAS a.s."
    assert resolution.czechia_cnb_curi_record_id == "63492555"
    assert resolution.match_score == 95.0

    # Discover test
    discovery = connector.discover("CREDITAS")
    assert len(discovery.candidates) == 3
    assert len(discovery.notices) == 1

    # Diagnose test
    diagnostic = connector.diagnose()
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count == 2
    assert diagnostic.formats == ("docx", "pdf", "zip")
