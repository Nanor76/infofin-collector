from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from connectors.base import ConnectorState
from connectors.spain_cnmv import (
    SpainCnmvConnector,
    parse_cnmv_html,
    SpainIssuerResolution,
)
from models import Issuer

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://www.cnmv.es"
BME_URL = "https://www.bolsasymercados.es"

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
        self.fail_bme = False

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = 10,
        verify: bool = True,
    ) -> FakeResponse:
        self.calls.append(url)
        if "Listed-Companies" in url:
            if self.fail_bme:
                raise RuntimeError("BME error")
            # Simple mock response containing BME list structure with ISIN ES0113900J37
            bme_html = """
            <table>
              <tr>
                <td>Banco Santander</td>
                <td><a href="/bolsa/company/SAN">SAN</a></td>
                <td>ES0113900J37</td>
              </tr>
            </table>
            """
            return FakeResponse(url=url, text=bme_html)
        if "Detalle.aspx" in url:
            return FakeResponse(url=url, text=fixture("spain_cnmv_detail.html"))
        return FakeResponse(url=url, text=fixture("spain_cnmv_listing.html"))

def test_cnmv_html_parsing() -> None:
    listing_html = fixture("spain_cnmv_listing.html")
    notices = parse_cnmv_html(listing_html, base_url=BASE_URL)

    assert len(notices) == 2
    # Notice 1: Santander (from data attributes)
    n1 = notices[0]
    assert n1.record_id == "202612345"
    assert n1.issuer_name == "Banco Santander, S.A."
    assert n1.nif == "A39000013"
    assert n1.isin_codes == ("ES0113900J37",)
    assert n1.title == "Informe financiero anual 2025"
    assert n1.document_type == "annual_financial_report"
    assert len(n1.files) == 2
    assert n1.files[0].file_type == "pdf"
    assert n1.files[1].file_type == "zip"

    # Notice 2: Telefonica (from fallback BeautifulSoup tags)
    n2 = notices[1]
    assert n2.record_id == "2026999"  # extracted from url Detalle.aspx?id=2026999
    assert n2.issuer_name == "Telefonica S.A."
    assert n2.isin_codes == ("ES0178430E18",)
    assert n2.title == "Cuentas anuales e informe de gestion 2025 (ES0178430E18)"
    assert n2.document_type == "annual_financial_report"
    assert len(n2.files) == 1
    assert n2.files[0].file_type == "xhtml"

def test_connector_methods_and_bme_enrichment() -> None:
    session = FakeSession()
    connector = SpainCnmvConnector(
        session=session,  # type: ignore[arg-type]
        base_url=BASE_URL,
        bme_listed_companies_url=BME_URL,
        rate_limit_seconds=0.0,
    )

    # Test resolution by ISIN (Santander)
    res = connector.resolve_issuer(symbol="SAN", name="Banco Santander, S.A.", isin="ES0113900J37")
    assert res.found
    assert res.cnmv_record_id == "202612345"
    assert res.cnmv_nif == "A39000013"
    assert res.bme_company_url == "https://www.bolsasymercados.es/bolsa/company/SAN"
    assert res.pea_country_check == "eu_candidate"

    # Test search recent documents
    docs = connector.search_recent_documents(market="Bolsa de Madrid")
    assert len(docs) == 3  # 2 files from Santander + 1 file from Telefonica
    assert all(d.source == "spain_cnmv" for d in docs)
    assert docs[0].document_type == "annual_financial_report"
    assert docs[0].metadata["file_format"] == "pdf"
    assert docs[2].metadata["file_format"] == "xhtml"

    # Test discover
    disc = connector.discover(query="informe financiero")
    assert len(disc.candidates) == 1
    assert disc.candidates[0].records_count == 2
    assert len(disc.notices) == 2

    # Test diagnose
    diag = connector.diagnose()
    assert diag.state == ConnectorState.READY
    assert diag.total_count == 2
    assert "pdf" in diag.formats
    assert "zip" in diag.formats
    assert "xhtml" in diag.formats

def test_bme_graceful_failure() -> None:
    session = FakeSession()
    session.fail_bme = True
    connector = SpainCnmvConnector(
        session=session,  # type: ignore[arg-type]
        base_url=BASE_URL,
        bme_listed_companies_url=BME_URL,
        rate_limit_seconds=0.0,
    )

    # Resolution should still succeed even if BME fails
    res = connector.resolve_issuer(symbol="SAN", name="Banco Santander, S.A.", isin="ES0113900J37")
    assert res.found
    assert res.cnmv_record_id == "202612345"
    assert res.bme_company_url is None  # no URL resolved, but didn't crash
