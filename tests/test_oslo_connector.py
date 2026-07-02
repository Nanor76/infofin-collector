from __future__ import annotations

from pathlib import Path
from typing import Any

from connectors.base import ConnectorState
from connectors.oslo_newsweb import OsloNewsWebConnector
from models import Issuer


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(
        self,
        text: str,
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

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        timeout: int,
        verify: bool = False,
    ) -> FakeResponse:
        prepared = OsloNewsWebConnector._full_url(url, params)
        self.calls.append((url, params))
        if url.endswith("/robots.txt"):
            if "newsweb" in url:
                return FakeResponse(
                    "User-agent: *\nDisallow: /",
                    url=prepared,
                    content_type="text/plain",
                )
            return FakeResponse(
                "User-agent: *\nAllow: /",
                url=prepared,
                content_type="text/plain",
            )
        if "/search_instruments/" in url:
            return FakeResponse(fixture("oslo_search.html"), url=prepared)
        if "/product/equities/" in url:
            return FakeResponse(fixture("oslo_product.html"), url=prepared)
        if "/listview/company-press-release/245243" in url:
            return FakeResponse(fixture("oslo_listing.html"), url=prepared)
        if "/ajax/node/company-press-release/12868738" in url:
            return FakeResponse(fixture("oslo_detail.html"), url=prepared)
        if "/ajax/node/company-press-release/12861702" in url:
            return FakeResponse(
                fixture("oslo_quarter_detail.html"),
                url=prepared,
            )
        if "company-press-releases-by-mkt" in url:
            page = dict(params or []).get("page") if params else None
            name = "oslo_listing_page2.html" if page == "1" else "oslo_listing.html"
            return FakeResponse(fixture(name), url=prepared)
        if "/markets/oslo/equities/company-news" in url:
            return FakeResponse(fixture("oslo_listing.html"), url=prepared)
        raise AssertionError(f"URL inattendue: {prepared}")

    def close(self) -> None:
        return None


def make_connector(session: FakeSession | None = None) -> OsloNewsWebConnector:
    return OsloNewsWebConnector(
        session=session or FakeSession(),  # type: ignore[arg-type]
        euronext_news_url=(
            "https://live.euronext.com/en/markets/oslo/equities/company-news"
        ),
        newsweb_base_url="https://newsweb.oslobors.no",
        rate_limit_seconds=0,
        lookback_days=400,
        timeout=10,
    )


def test_listing_fixture_is_parsed_tolerantly() -> None:
    listing = OsloNewsWebConnector.parse_listing(
        fixture("oslo_listing.html"),
        "https://live.euronext.com/en/markets/oslo/equities/company-news",
    )

    assert listing.total_count == 3
    assert listing.has_pagination is True
    assert listing.ajax_path == "https://live.euronext.com/en/views/ajax"
    assert len(listing.notices) == 2
    assert listing.notices[0].node_id == "12868738"
    assert listing.notices[0].published_date.isoformat() == "2026-03-10"
    assert "Annual financial" in listing.topics[0]
    assert listing.topic_parameters["annual financial and audit reports"] == (
        "field_company_press_releases_target_id[96]",
        "96",
    )


def test_detail_fixture_exposes_text_newsweb_and_attachments() -> None:
    detail = OsloNewsWebConnector.parse_detail(
        fixture("oslo_detail.html"),
        "https://live.euronext.com/en/ajax/node/company-press-release/12868738",
    )

    assert detail.isin == "BMG9156K1018"
    assert detail.newsweb_url == "https://newsweb.oslobors.no/message/667877"
    assert "Annual Report for 2025" in detail.text
    assert [url.rsplit(".", 1)[-1] for url, _ in detail.attachments] == [
        "xhtml",
        "pdf",
    ]


def test_search_documents_handles_pagination_and_attachment_types() -> None:
    session = FakeSession()
    connector = make_connector(session)
    issuer = Issuer(
        "2020 BULKERS",
        "BMG9156K1018",
        "2020",
        "Oslo Børs",
    )

    candidates = connector.search_documents(issuer)

    assert connector.state == ConnectorState.READY
    assert len(candidates) == 3
    assert {candidate.document_type for candidate in candidates} == {
        "annual_financial_report",
        "quarterly_financial_report",
    }
    assert any(
        dict(params or []).get("page") == "1"
        for _, params in session.calls
        if isinstance(params, list)
    )
    assert not any(
        url == "https://newsweb.oslobors.no/message/667877"
        for url, _ in session.calls
    )


def test_diagnose_and_discover_return_real_page_shapes() -> None:
    connector = make_connector()

    diagnostic = connector.diagnose()
    discovery = connector.discover("annual financial")

    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.http_status == 200
    assert diagnostic.detected_count == 2
    assert diagnostic.example_notice["node_id"] == "12868738"
    assert {candidate.format for candidate in discovery.candidates} >= {
        "HTML",
        "JSON",
        "HTML fragment",
    }
    assert discovery.candidates[0].pagination is not None


def test_issuer_resolution_finds_internal_id_and_urls() -> None:
    connector = make_connector()

    resolution = connector.resolve_issuer(
        symbol="2020",
        name="2020 BULKERS",
    )

    assert resolution.found is True
    assert resolution.name == "2020 BULKERS"
    assert resolution.isin == "BMG9156K1018"
    assert resolution.oslo_issuer_id == "245243"
    assert resolution.newsweb_url == (
        "https://newsweb.oslobors.no/message/667877"
    )
    assert resolution.euronext_company_url.endswith(
        "/BMG9156K1018-XOSL"
    )
