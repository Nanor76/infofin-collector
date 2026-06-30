from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from connectors.base import ConnectorState
from connectors.netherlands_afm import (
    NetherlandsAfmConnector,
    classify_afm_document,
    match_issuer_record,
    parse_afm_csv,
    parse_afm_detail_html,
    parse_afm_listing_html,
    parse_afm_xml,
    parse_home_member_state_xml,
)
from models import Issuer

FIXTURES = Path(__file__).parent / "fixtures"
REGISTER_URL = (
    "https://www.afm.nl/en/sector/registers/meldingenregisters/"
    "financiele-verslaggeving"
)
HOME_URL = (
    "https://www.afm.nl/en/sector/registers/meldingenregisters/"
    "home-member-state"
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(
        self,
        text: str,
        *,
        url: str,
        status_code: int = 200,
        encoding: str = "utf-8",
    ) -> None:
        self.text = text
        self.content = text.encode(encoding)
        self.url = url
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.headers = {"User-Agent": "InfoFin test"}
        self.calls: list[str] = []
        self.head_calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        timeout: int,
    ) -> FakeResponse:
        prepared = requests.Request("GET", url, params=params).prepare().url or url
        self.calls.append(prepared)
        if "export.aspx" in url and "format=xml" in url:
            if "6b365727" in url:
                return FakeResponse(
                    fixture("netherlands_home_member_state.xml"),
                    url=prepared,
                )
            return FakeResponse(
                fixture("netherlands_afm.xml"),
                url=prepared,
            )
        if "export.aspx" in url and "format=csv" in url:
            return FakeResponse(
                fixture("netherlands_afm.csv"),
                url=prepared,
                encoding="cp1252",
            )
        if url == HOME_URL:
            return FakeResponse(
                "<html><h1>Register home member state</h1></html>",
                url=prepared,
            )
        if "details?id=A2510-03545" in prepared:
            return FakeResponse(
                fixture("netherlands_detail.html"),
                url=prepared,
            )
        if "details?id=A2510-03453" in prepared:
            return FakeResponse(
                fixture("netherlands_ajax_detail.html"),
                url=prepared,
            )
        if url == REGISTER_URL:
            return FakeResponse(
                fixture("netherlands_listing.html"),
                url=prepared,
            )
        raise AssertionError(f"Unexpected URL: {prepared}")

    def head(
        self,
        url: str,
        *,
        timeout: int,
        allow_redirects: bool,
    ) -> FakeResponse:
        self.head_calls.append(url)
        return FakeResponse("", url=url)

    def close(self) -> None:
        return None


def make_connector(
    session: FakeSession | None = None,
) -> NetherlandsAfmConnector:
    return NetherlandsAfmConnector(
        session=session or FakeSession(),  # type: ignore[arg-type]
        register_url=REGISTER_URL,
        home_member_state_url=HOME_URL,
        rate_limit_seconds=0,
        lookback_days=900,
        timeout=10,
    )


def test_csv_and_xml_fixtures_are_parsed() -> None:
    xml_records = parse_afm_xml(
        fixture("netherlands_afm.xml"),
        register_url=REGISTER_URL,
    )
    csv_records = parse_afm_csv(
        fixture("netherlands_afm.csv"),
        register_url=REGISTER_URL,
    )

    assert len(xml_records) == 2
    assert xml_records[0].record_id == "A2510-03545"
    assert xml_records[0].filing_date.isoformat() == "2026-03-23"
    assert xml_records[0].detail_url.endswith("id=A2510-03545")
    assert xml_records[1].document_type_en == "Half-yearly financial report"
    assert len(csv_records) == 2
    assert csv_records[0].issuing_institution == "Aalberts N.V."
    assert csv_records[0].record_id is None


def test_html_listing_and_detail_fixtures_are_parsed() -> None:
    listing = parse_afm_listing_html(
        fixture("netherlands_listing.html"),
        register_url=REGISTER_URL,
    )
    documents = parse_afm_detail_html(
        fixture("netherlands_detail.html"),
        detail_url=f"{REGISTER_URL}/details?id=A2510-03545",
    )

    assert listing.total_count == 2
    assert listing.page_size == 50
    assert listing.context_item_id
    assert listing.records[0].record_id == "A2510-03545"
    assert documents[0].filename.endswith(".zip")
    assert "downloadregisterfile.aspx" in documents[0].download_url
    assert classify_afm_document(
        documents[0].document_type,
        documents[0].filename,
    ) == "esef"


def test_matching_issuer_and_home_member_state() -> None:
    records = parse_afm_xml(
        fixture("netherlands_afm.xml"),
        register_url=REGISTER_URL,
    )
    issuer = Issuer(
        "AALBERTS NV",
        "NL0000852564",
        "AALB",
        "Euronext Amsterdam",
    )
    wrong = Issuer(
        "Air France-KLM",
        "FR001400J770",
        "AF",
        "Euronext Amsterdam",
    )
    home = parse_home_member_state_xml(
        fixture("netherlands_home_member_state.xml")
    )

    assert match_issuer_record(issuer, records[0])
    assert not match_issuer_record(wrong, records[0])
    assert home[0].home_member_state == "Netherlands"
    assert home[1].home_member_state == "France"


def test_diagnose_discover_resolve_and_search_use_real_afm_shapes() -> None:
    session = FakeSession()
    connector = make_connector(session)

    diagnostic = connector.diagnose()
    discovery = connector.discover("annual financial")
    resolution = connector.resolve_issuer(
        symbol="AALB",
        name="AALBERTS NV",
        isin="NL0000852564",
    )
    candidates = connector.search_documents(
        Issuer(
            "AALBERTS NV",
            "NL0000852564",
            "AALB",
            "Euronext Amsterdam",
        )
    )

    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count == 2
    assert diagnostic.checks["csv_export"]
    assert diagnostic.checks["xml_export"]
    assert diagnostic.checks["home_member_state_export"]
    assert diagnostic.checks["automatic_download"]
    assert diagnostic.example_notice["record_id"] == "A2510-03545"
    assert any(
        candidate.format == "XML" and candidate.verified
        for candidate in discovery.candidates
    )
    assert discovery.notices[0].download_url
    assert resolution.found
    assert resolution.afm_record_id == "A2510-03545"
    assert resolution.home_member_state == "Netherlands"
    assert len(candidates) == 1
    assert candidates[0].source == "afm"
    assert candidates[0].document_type == "esef"
    assert candidates[0].metadata["home_member_state"] == "Netherlands"
    assert session.head_calls
