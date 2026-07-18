from __future__ import annotations

from datetime import date

from classification_audit import ALL_MARKETS, _candidate_row
from connectors import SUPPORTED_WATCH_MARKETS
from connectors.base import DocumentCandidate


def test_full_audit_tracks_every_supported_market() -> None:
    assert ALL_MARKETS == SUPPORTED_WATCH_MARKETS


def test_audit_does_not_use_the_classification_as_its_own_evidence() -> None:
    candidate = DocumentCandidate(
        title="Halvårsrapport (Q2)",
        url="https://finanscentralen.fi.se/search/GetFile.aspx?fid=63222",
        published_date=date(2026, 7, 14),
        document_type="annual_financial_report",
        source="sweden_fi",
        classification="annual_financial_report",
        classification_reason="Source category",
    )

    row = _candidate_row("Nasdaq Stockholm", candidate)

    assert row.category == ""
    assert row.title_guess == "half_year_financial_report"
    assert row.status == "CONFLICT"


def test_audit_uses_the_oslo_local_period_classifier() -> None:
    candidate = DocumentCandidate(
        title="Sparebanken Øst - regnskap 2. kvartal 2026",
        url="https://live.euronext.com/notice/12898851",
        published_date=date(2026, 7, 14),
        document_type="quarterly_financial_report",
        source="euronext_oslo_company_news",
        metadata={
            "topic": (
                "Halvårsrapporter og revisjonsberetninger / uttalelser om "
                "forenklet revisorkontroll"
            )
        },
    )

    row = _candidate_row("Oslo Børs", candidate)

    assert row.title_guess == "quarterly_financial_report"
    assert row.status == "MATCH"


def test_audit_uses_the_denmark_local_period_classifier() -> None:
    candidate = DocumentCandidate(
        title="Tryg A/S – interim report Q2 and H1 2026",
        url="https://example.test/details/300011564",
        published_date=date(2026, 7, 10),
        document_type="half_year_financial_report",
        source="dfsa_oam",
        metadata={"category": "Issuer"},
    )

    row = _candidate_row("Nasdaq Copenhagen", candidate)

    assert row.title_guess == "half_year_financial_report"
    assert row.status == "MATCH"
