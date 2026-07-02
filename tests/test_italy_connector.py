from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from connectors.base import ConnectorState
from connectors.italy_emarketstorage import (
    ItalyEmarketStorageConnector,
    ItalyNotice,
    classify_italy_document,
    match_issuer_notice,
    parse_emarket_html,
)
from models import Issuer

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(
        self,
        text: str = "",
        *,
        url: str,
        status_code: int = 200,
        content_type: str = "text/html; charset=UTF-8",
    ) -> None:
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.headers = {"User-Agent": "InfoFin test"}
        self.calls: list[tuple[str, Any]] = []
        self.head_calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        timeout: int,
        verify: bool = True,
        stream: bool = False,
    ) -> FakeResponse:
        prepared = requests.Request("GET", url, params=params).prepare().url or url
        self.calls.append((prepared, params))
        if url.endswith("/robots.txt"):
            return FakeResponse(
                "User-agent: *\nAllow: /",
                url=prepared,
                content_type="text/plain",
            )
        if "1info.it" in url:
            return FakeResponse(fixture("italy_1info.html"), url=prepared)
        if "borsaitaliana.it" in url:
            return FakeResponse(
                "<html><title>Borsa Italiana</title></html>",
                url=prepared,
            )
        if url.rstrip("/") == "https://www.emarketstorage.it":
            return FakeResponse(fixture("italy_home.html"), url=prepared)
        if "/it/documenti" in url:
            page = str(params.get("page")) if params and "page" in params else ""
            if not page:
                page = dict(
                    item.split("=", 1)
                    for item in urlparse(prepared).query.split("&")
                    if "=" in item
                ).get("page", "")
            html = (
                fixture("italy_documents_page2.html")
                if page == "1"
                else fixture("italy_documents.html")
            )
            return FakeResponse(html, url=prepared)
        if "/it/comunicati-finanziari" in url:
            return FakeResponse(
                fixture("italy_press_releases.html"),
                url=prepared,
            )
        raise AssertionError(f"Unexpected URL: {prepared}")

    def head(
        self,
        url: str,
        *,
        timeout: int,
        verify: bool,
        allow_redirects: bool,
    ) -> FakeResponse:
        self.head_calls.append(url)
        return FakeResponse(
            url=url,
            content_type=(
                "application/zip"
                if url.endswith(".xbri")
                else "application/pdf"
            ),
        )

    def close(self) -> None:
        return None


def make_connector(
    session: FakeSession | None = None,
) -> ItalyEmarketStorageConnector:
    return ItalyEmarketStorageConnector(
        session=session or FakeSession(),  # type: ignore[arg-type]
        home_url="https://www.emarketstorage.it/",
        press_releases_url=(
            "https://www.emarketstorage.it/it/comunicati-finanziari"
        ),
        documents_url="https://www.emarketstorage.it/it/documenti",
        oneinfo_url="https://www.1info.it/PORTALE1INFO",
        market="Euronext Milan",
        rate_limit_seconds=0,
        lookback_days=400,
        timeout=10,
        verify_ssl=True,
        max_pages=2,
    )


def test_classification_keywords_and_categories() -> None:
    assert (
        classify_italy_document("Relazioni finanziarie annuali 2025")
        == "annual_financial_report"
    )
    assert (
        classify_italy_document("Bilancio consolidato")
        == "annual_financial_report"
    )
    assert (
        classify_italy_document("Documento periodico", "1.1")
        == "annual_financial_report"
    )
    assert (
        classify_italy_document("Documento periodico", "1.2")
        == "half_year_financial_report"
    )
    assert (
        classify_italy_document("Documento periodico", "DOAG")
        == "financial_report"
    )
    assert (
        classify_italy_document(
            "Relazione finanziaria annuale ESEF",
            "1.1",
            "https://official.test/report.xbri",
        )
        == "annual_financial_report"
    )
    assert (
        classify_italy_document("Relazione della società di revisione")
        == "audit_report"
    )
    assert (
        classify_italy_document(
            "Verbale Assemblea ordinaria per approvazione del bilancio 2025",
            "1.1",
        )
        == "other_regulatory_announcement"
    )
    assert (
        classify_italy_document("IWB - bilancio ESEF con relazione BDO", "1.1")
        == "annual_financial_report"
    )
    assert (
        classify_italy_document("Relazione di revisione al bilancio chiuso al 31.12.2025", "1.1")
        == "audit_report"
    )
    assert classify_italy_document("Comunicato ordinario") is None


def test_parser_extracts_direct_links_categories_and_pagination() -> None:
    parsed = parse_emarket_html(
        fixture("italy_documents.html"),
        base_url="https://www.emarketstorage.it/it/documenti",
    )

    assert len(parsed.notices) == 2
    assert parsed.notices[0].protocol == "185771"
    assert parsed.notices[0].published_date.isoformat() == "2026-06-12"
    assert parsed.notices[0].document_url.endswith("20260612_185771.pdf")
    assert parsed.notices[1].document_url.endswith("20260609_185623.xbri")
    assert any(category.startswith("1.1") for category in parsed.categories)
    assert any(category.startswith("1.2") for category in parsed.categories)
    assert any(category.startswith("DOAG") for category in parsed.categories)
    assert parsed.has_pagination is True
    assert parsed.next_url.endswith("categoria=100&page=1")


def test_issuer_matching_uses_normalized_name_and_symbol() -> None:
    issuer = Issuer(
        "Landi Renzo S.p.A.",
        "IT0005619843",
        "LR",
        "Euronext Milan",
    )
    exact = ItalyNotice(
        None,
        "LANDI RENZO",
        "Annual Report",
        "https://official.test/landi.pdf",
        None,
        "1",
        "1.1",
    )
    wrong = ItalyNotice(
        None,
        "MONDO TV",
        "Annual Report",
        "https://official.test/mondo.pdf",
        None,
        "2",
        "1.1",
    )

    assert match_issuer_notice(issuer, exact)
    assert not match_issuer_notice(issuer, wrong)


def test_diagnose_tests_required_emarket_capabilities() -> None:
    session = FakeSession()
    diagnostic = make_connector(session).diagnose()

    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.http_status == 200
    assert diagnostic.detected_count >= 3
    assert diagnostic.example_document["protocol"] == "185771"
    assert all(diagnostic.checks.values())
    assert diagnostic.fallback_sources == {"1info": "stub"}
    assert session.head_calls[0].endswith(".pdf")
    assert {
        "italy_home",
        "italy_documents",
        "italy_press_releases",
        "italy_pagination",
        "italy_direct_document",
        "italy_1info_fallback",
    }.issubset({attempt.name for attempt in diagnostic.attempts})


def test_discover_returns_real_notice_and_fallbacks() -> None:
    discovery = make_connector().discover(
        "relazione finanziaria annuale"
    )

    assert any(
        notice.protocol == "185771" for notice in discovery.notices
    )
    assert any(
        candidate.role == "primary regulated documents search"
        and candidate.state == ConnectorState.READY
        for candidate in discovery.candidates
    )
    assert any(
        candidate.role == "secondary authorized storage fallback"
        and candidate.state == ConnectorState.STUB
        for candidate in discovery.candidates
    )
    assert any(
        candidate.role == "Euronext Growth Milan issuer/company discovery"
        and candidate.verified
        and candidate.state == ConnectorState.DEGRADED
        for candidate in discovery.candidates
    )


def test_resolve_issuer_builds_emarket_url() -> None:
    resolution = make_connector().resolve_issuer(
        symbol="LR",
        name="Landi Renzo S.p.A.",
        isin="IT0005619843",
    )

    assert resolution.found is True
    assert resolution.emarket_issuer_id == "267"
    assert resolution.storage_provider == "emarketstorage"
    assert resolution.emarket_url.endswith("?azienda=267")
    assert resolution.borsa_italiana_company_url.endswith(
        "/IT0005619843.html?lang=it"
    )


def test_search_documents_uses_global_cache_and_pagination() -> None:
    session = FakeSession()
    connector = make_connector(session)
    issuer = Issuer(
        "LANDI RENZO",
        "IT0005619843",
        "LR",
        "Euronext Milan",
    )

    first = connector.search_documents(issuer)
    calls_after_first = len(session.calls)
    second = connector.search_documents(issuer)

    assert connector.state == ConnectorState.READY
    assert {candidate.document_type for candidate in first} == {
        "annual_financial_report",
        "half_year_financial_report",
    }
    assert all(candidate.source == "emarketstorage" for candidate in first)
    assert {candidate.source_document_id for candidate in first} == {
        "185771",
        "180001",
    }
    assert second == first
    assert len(session.calls) == calls_after_first
