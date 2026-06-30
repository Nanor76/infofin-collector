from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests

from connectors.base import ConnectorState
from connectors.base import DocumentCandidate
from connectors.denmark_dfsa_oam import (
    DenmarkDfsaOamConnector,
    classify_denmark_document,
    extract_denmark_date_info,
    parse_denmark_detail_json,
    parse_denmark_dfsa_html,
    parse_denmark_search_json,
)
from models import Issuer
from watcher import _candidate_match_score


FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://www.dfsa.dk/financial-themes/capital-market/company-announcements"
ORIGIN = "https://appft.gold.extension.gopublic.dk"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        text: str = "",
        payload: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> None:
        self.url = url
        self.text = text
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url))
        if "/details/" in url:
            return FakeResponse(
                url=url,
                payload=json.loads(fixture("denmark_dfsa_detail.json")),
            )
        return FakeResponse(url=url, text=fixture("denmark_dfsa_landing.html"))

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url))
        return FakeResponse(
            url=url,
            payload=json.loads(fixture("denmark_dfsa_listing.json")),
        )


def test_dfsa_json_html_and_detail_parsing() -> None:
    payload = json.loads(fixture("denmark_dfsa_listing.json"))
    notices = parse_denmark_search_json(
        payload,
        f"{ORIGIN}/api/test-module/details",
    )
    html_notices = parse_denmark_dfsa_html(
        fixture("denmark_dfsa_listing.html"),
        f"{ORIGIN}/api/test-module/details",
    )
    detailed = parse_denmark_detail_json(
        json.loads(fixture("denmark_dfsa_detail.json")),
        notices[0],
        ORIGIN,
    )

    assert len(notices) == 2
    assert notices[0].record_id == "300009086"
    assert notices[0].published_raw == "19-05-2026 08:00:00"
    assert html_notices[0].issuer_name == "MATAS A/S"
    assert detailed.category == "Annual financial report"
    assert detailed.national_business_id == "27528406"
    assert detailed.lei == "2138004PXX8LWGHGL872"
    assert {item.file_type for item in detailed.files} == {
        "PDF",
        "XHTML",
        "ZIP/XBRL",
    }


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Annual report 2025", "annual_financial_report"),
        ("Årsrapport 2025", "annual_financial_report"),
        ("Årsregnskab 2025", "annual_financial_report"),
        ("Årsregnskabsmeddelelse 2025", "year_end_report"),
        ("Halvårsrapport 2026", "half_year_financial_report"),
        ("Delårsrapport Q1 2026", "interim_report"),
        ("Kvartalsrapport Q1 2026", "quarterly_report"),
    ],
)
def test_strict_positive_classification(title: str, expected: str) -> None:
    classification, _, positive, negative = classify_denmark_document(title)
    assert classification == expected
    assert positive
    assert not negative


@pytest.mark.parametrize(
    "title",
    [
        "Major holding notification",
        "Manager transaction",
        "Bond issuance",
        "Notes tender offer",
        "Share buyback programme",
        "Financial calendar 2026",
    ],
)
def test_strict_negative_classification(title: str) -> None:
    classification, _, _, negative = classify_denmark_document(title)
    assert classification == "other_regulatory_announcement"
    assert negative


def test_published_and_period_dates_are_separate() -> None:
    info = extract_denmark_date_info(
        "Annual report 2025",
        "19-05-2026 08:00:00",
        filenames=("issuer-2025-12-31-en.xhtml",),
    )
    assert info["published_at"] == date(2026, 5, 19)
    assert info["period_end_date"] == date(2025, 12, 31)
    assert info["reporting_year"] == 2025
    assert info["source_publication_date_raw"] == "19-05-2026 08:00:00"


def test_connector_discovers_endpoints_and_materializes_only_after_match() -> None:
    session = FakeSession()
    connector = DenmarkDfsaOamConnector(
        session=session,  # type: ignore[arg-type]
        base_url=BASE_URL,
        rate_limit_seconds=0,
    )

    recent = connector.search_recent_documents(
        "Nasdaq Copenhagen",
        since=date(2026, 5, 1),
        limit=10,
    )
    assert len(recent) == 2
    assert not any("/details/" in url for _, url in session.calls)

    detailed = connector.materialize_candidate(
        recent[0],
        Issuer("MATAS A/S", "DK0060497295", "MATAS", "Nasdaq Copenhagen"),
    )
    assert connector.details_visited == 1
    assert len(detailed) == 4
    assert len([item for item in detailed if item.classification in {
        "annual_financial_report",
        "half_year_financial_report",
        "interim_report",
        "quarterly_report",
        "year_end_report",
    }]) == 3
    assert detailed[0].metadata["issuer_country"] == "Denmark"
    assert detailed[0].metadata["pea_geography_status"] == "eu_candidate"

    diagnostic = connector.diagnose()
    assert diagnostic.state == ConnectorState.READY
    assert {"PDF", "XHTML", "ZIP/XBRL"}.issubset(set(diagnostic.formats))


def test_watchlist_matching_prefers_isin_then_name_then_symbol() -> None:
    issuer = Issuer(
        "MATAS A/S",
        "DK0060497295",
        "MATAS",
        "Nasdaq Copenhagen",
    )

    def candidate(metadata: dict[str, Any]) -> DocumentCandidate:
        return DocumentCandidate(
            title="Annual report",
            url="https://example.test/report.pdf",
            published_date=date(2026, 5, 19),
            document_type="annual_financial_report",
            source="dfsa_oam",
            metadata=metadata,
        )

    assert _candidate_match_score(
        issuer, candidate({"issuer_isins": ["DK0060497295"]})
    ) == 100
    assert _candidate_match_score(
        issuer, candidate({"issuer_name": "MATAS A/S"})
    ) == 90
    assert _candidate_match_score(
        issuer, candidate({"issuer_symbol": "MATAS"})
    ) == 75


def test_dfsa_search_pagination() -> None:
    class PagingSession(FakeSession):
        def post(self, url: str, **kwargs: Any) -> FakeResponse:
            self.calls.append(("POST", url))
            page = kwargs["json"]["page"]
            payload = json.loads(fixture("denmark_dfsa_listing.json"))
            payload["paging"]["totalPages"] = 2
            payload["paging"]["totalCount"] = 3
            if page == 2:
                payload["data"]["rows"] = [
                    {
                        "id": "300009088",
                        "HeadlineColumn": "Halvårsrapport 2026",
                        "IssuerColumn": "MATAS A/S",
                        "CategoryColumn": "Issuer",
                        "PublicationDateColumn": "21-05-2026 09:00:00",
                        "RegistrationDateColumn": "21-05-2026 09:00:10",
                    }
                ]
            return FakeResponse(url=url, payload=payload)

    session = PagingSession()
    connector = DenmarkDfsaOamConnector(
        session=session,  # type: ignore[arg-type]
        base_url=BASE_URL,
        rate_limit_seconds=0,
    )
    recent = connector.search_recent_documents(
        "Nasdaq Copenhagen",
        since=date(2026, 5, 1),
        limit=10,
    )
    assert len(recent) == 3
    assert len([call for call in session.calls if call[0] == "POST"]) == 2


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="RUN_LIVE_TESTS=1 not set",
)
def test_denmark_live_source() -> None:
    session = requests.Session()
    connector = DenmarkDfsaOamConnector(
        session=session,
        base_url=BASE_URL,
        rate_limit_seconds=0.2,
    )
    diagnostic = connector.diagnose()
    discovery = connector.discover("annual report", limit=5)
    assert diagnostic.state in {ConnectorState.READY, ConnectorState.DEGRADED}
    assert diagnostic.example_notice
    assert discovery.notices
    assert discovery.candidates
