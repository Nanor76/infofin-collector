from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests

from connectors.austria_oekb_oam import (
    AustriaOekbOamConnector,
    PERIODIC_TYPE_MAP,
    classify_austria_document,
    extract_austria_date_info,
    parse_austria_feed,
)
from connectors.base import ConnectorState
from models import Issuer


FIXTURE = Path(__file__).parent / "fixtures" / "austria_oekb_feed.json"


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        return FakeResponse(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_feed_parsing_and_strict_category_mapping() -> None:
    notices = parse_austria_feed(
        json.loads(FIXTURE.read_text(encoding="utf-8"))
    )

    assert len(notices) == 5
    assert notices[0].record_id == "249355"
    assert notices[0].issuer_isins == ("AT0000969985",)
    assert notices[0].files[0].file_format == "zip"
    assert notices[-1].issuer_isins == ("AT0000969985",)
    for code, expected in PERIODIC_TYPE_MAP.items():
        classification, reason, positive, negative = (
            classify_austria_document(code)
        )
        assert classification == expected
        assert code in reason
        assert positive == [code]
        assert negative == []


@pytest.mark.parametrize(
    "code",
    [
        "EP_AD_HOC",
        "EP_EIGENGESCHAEFT_VON_FUEHRUNGSKRAFT",
        "EP_AEND_WESENTL_STIMMRECHTSSCHWELLEN",
        "EP_HV_ANKUENDIGUNG",
        "EP_BEKANNTGABEN_ZU_RUECKKAUFPROGRAMMEN",
        "EP_SONST_KAP_MASSNAHMEN",
    ],
)
def test_non_periodic_categories_are_rejected(code: str) -> None:
    classification, reason, positive, negative = (
        classify_austria_document(code)
    )
    assert classification == "other_regulatory_announcement"
    assert code in reason
    assert positive == []
    assert negative == [code]


def test_publication_and_reporting_dates_are_not_confused() -> None:
    explicit = extract_austria_date_info(
        title="Annual financial report",
        filename="issuer-2025-12-31-en.zip",
        published_raw=1773900348227,
    )
    ambiguous = extract_austria_date_info(
        title="Jahresfinanzbericht gemäß § 124 BörseG",
        filename="issuer-2026-06-11-0-de.xbri",
        published_raw=1781157681424,
    )
    fiscal_range = extract_austria_date_info(
        title="Annual financial report",
        filename="Annual_Report_2025-26.pdf",
        published_raw=1781157681424,
    )

    assert explicit["published_at"] == date(2026, 3, 19)
    assert explicit["period_end_date"] == date(2025, 12, 31)
    assert explicit["reporting_year"] == 2025
    assert ambiguous["published_at"] == date(2026, 6, 11)
    assert ambiguous["period_end_date"] is None
    assert ambiguous["reporting_year"] is None
    assert fiscal_range["period_end_date"] is None
    assert fiscal_range["reporting_year"] == 2026


def test_connector_uses_one_cached_feed_and_matches_locally() -> None:
    session = FakeSession()
    connector = AustriaOekbOamConnector(
        session=session,  # type: ignore[arg-type]
        rate_limit_seconds=0,
    )

    recent = connector.search_recent_documents(
        "Vienna Stock Exchange",
        since=date(2026, 5, 1),
        limit=20,
    )
    annual = connector.discover("annual")
    half_year = connector.discover("half-year")
    resolution = connector.resolve_issuer(
        Issuer(
            "AT & S Austria Technologie & Systemtechnik Aktiengesellschaft",
            "AT0000969985",
            "ATS",
            "Vienna Stock Exchange",
        )
    )
    diagnostic = connector.diagnose()

    assert len(session.calls) == 1
    assert connector.scanned_notices == 5
    assert any(
        candidate.document_type == "other_regulatory_announcement"
        for candidate in recent
    )
    assert {candidate.document_type for candidate in annual.candidates} == {
        "annual_financial_report",
    }
    assert half_year.candidates[0].document_type == "half_year_financial_report"
    assert resolution.found is True
    assert resolution.austria_oekb_oam_id == "1041"
    assert resolution.match_score == 100
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count == 5
    assert diagnostic.http_calls == 1
    assert {"pdf", "zip"}.issubset(set(diagnostic.formats))


@pytest.mark.skipif(
    __import__("os").environ.get("RUN_LIVE_TESTS") != "1",
    reason="RUN_LIVE_TESTS=1 not set",
)
def test_austria_live_source() -> None:
    connector = AustriaOekbOamConnector(
        session=requests.Session(),
        rate_limit_seconds=0.2,
    )
    diagnostic = connector.diagnose()
    annual = connector.discover("annual")
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count > 0
    assert annual.candidates
