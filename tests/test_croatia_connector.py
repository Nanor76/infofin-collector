from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from connectors.base import ConnectorState
from connectors.croatia_hanfa_srpi import (
    CroatiaHanfaSrpiConnector,
    extract_croatia_date_info,
    parse_croatia_payload,
)
from models import Issuer


FIXTURE = Path(__file__).parent / "fixtures" / "croatia_hanfa_srpi.json"


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
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        category = kwargs["data"]["KatId"]
        if category != "17":
            payload["data"] = []
        return FakeResponse(payload)


def test_parse_filters_formats_and_marks_superseded() -> None:
    notices = parse_croatia_payload(
        json.loads(FIXTURE.read_text(encoding="utf-8")),
        category_id="17",
    )

    assert len(notices) == 3
    assert notices[0].issuer_name == "ĐURO ĐAKOVIĆ GRUPA d.d."
    assert notices[0].reporting_year == 2025
    assert [item.file_format for item in notices[0].files] == ["pdf"]
    assert notices[0].files[0].attachment_id == "1234447:pdf"
    assert notices[1].superseded is True
    period_end, reporting_year, reason = extract_croatia_date_info(
        notices[0],
        notices[0].files[0].filename,
    )
    assert period_end == date(2025, 12, 31)
    assert reporting_year == 2025
    assert "Annual category" in reason


def test_connector_is_source_first_cached_and_strict() -> None:
    session = FakeSession()
    connector = CroatiaHanfaSrpiConnector(
        session=session,  # type: ignore[arg-type]
        rate_limit_seconds=0,
        page_size=100,
    )

    recent = connector.search_recent_documents(
        "Zagreb Stock Exchange",
        since=date(2026, 1, 1),
    )
    resolution = connector.resolve_issuer(
        Issuer(
            "ĐURO ĐAKOVIĆ GRUPA d.d.",
            "HRDDJTRA0007",
            "DDJH",
            "Zagreb Stock Exchange",
        )
    )
    diagnostic = connector.diagnose()

    assert len(session.calls) == 9
    assert all(call["data"]["KatId"] in {"17", "24", "18"} for call in session.calls)
    assert all(
        candidate.document_type == "annual_financial_report"
        for candidate in recent
    )
    assert len(recent) == 3
    assert all(candidate.metadata["official_source"] == 1 for candidate in recent)
    assert resolution.found is True
    assert resolution.match_score == 100
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.attachment_count == 4


def test_quarterly_period_requires_explicit_year_and_quarter() -> None:
    payload = {
        "data": [[
            "2026-05-29 13:22:04",
            "Čakovečki mlinovi d.d.",
            "Quarterly financial report (art.468.CMA)",
            "<span>Year: </span> 2026<span>Quarter: </span> 1",
            "",
            '<a href="/SRPI/EN/2026/2026_05_29-1232803_pdf.pdf">Q1 report.pdf</a>',
        ]]
    }
    notice = parse_croatia_payload(payload, category_id="18")[0]
    period_end, reporting_year, _ = extract_croatia_date_info(
        notice,
        notice.files[0].filename,
    )

    assert period_end == date(2026, 3, 31)
    assert reporting_year == 2026
