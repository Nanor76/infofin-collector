from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from main import discover_market_document_links
from models import Issuer


class FakeSession:
    def close(self) -> None:
        return None


class FakeSourceFirstConnector(Connector):
    supports_source_first = True
    source_name = "fake-oam"
    state = ConnectorState.READY
    last_error = None

    def __init__(self, candidates: list[DocumentCandidate]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, date | None, int | None]] = []

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        self.calls.append((market, since, limit))
        return self.candidates[:limit]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        raise AssertionError("watchlist/issuer mode must not be used")


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "unused.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=10,
        http_retries=0,
        http_backoff_factor=0,
        user_agent="test",
        max_download_bytes=1024 * 1024,
        amf_base_url="https://www.info-financiere.gouv.fr",
        amf_fallback_base_urls=(),
        amf_dataset="flux-amf-new-prod",
        amf_rows=100,
    )


def test_discover_market_document_links_filters_dates_and_dedupes(
    tmp_path: Path,
) -> None:
    in_range = DocumentCandidate(
        title="Annual report",
        url="https://official.test/report.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-1",
        metadata={
            "issuer_name": "Issuer A",
            "issuer_isins": ["FR0000000001"],
        },
    )
    duplicate = DocumentCandidate(
        title="Annual report duplicate",
        url="https://official.test/report-copy.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-1",
    )
    out_of_range = DocumentCandidate(
        title="Old annual report",
        url="https://official.test/old.pdf",
        published_date=date(2026, 6, 1),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-old",
    )
    connector = FakeSourceFirstConnector([in_range, duplicate, out_of_range])

    export = discover_market_document_links(
        make_settings(tmp_path),
        markets=("Euronext Paris",),
        date_from=date(2026, 6, 10),
        date_to=date(2026, 6, 15),
        output_format="csv",
        output_dir=tmp_path / "exports",
        max_candidates=100000,
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: connector,
    )

    assert export.documents_count == 1
    assert export.errors == ()
    assert export.warnings == ()
    assert connector.calls == [("Euronext Paris", date(2026, 6, 10), 100000)]

    with export.output_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows == [
        {
            "market": "Euronext Paris",
            "source": "fake-oam",
            "source_document_id": "doc-1",
            "published_at": "2026-06-12",
            "period_end_date": "",
            "reporting_year": "",
            "document_type": "annual_financial_report",
            "classification": "",
            "title": "Annual report",
            "url": "https://official.test/report.pdf",
            "issuer_name": "Issuer A",
            "issuer_isin": "FR0000000001",
            "issuer_lei": "",
            "category": "",
            "date_confidence": "high",
            "source_publication_date_raw": "",
        }
    ]


def test_discover_market_document_links_json_includes_errors(
    tmp_path: Path,
) -> None:
    export = discover_market_document_links(
        make_settings(tmp_path),
        markets=("Unknown Market",),
        date_from=date(2026, 6, 10),
        date_to=date(2026, 6, 15),
        output_format="json",
        output_dir=tmp_path / "exports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: None,
    )

    payload = json.loads(export.output_path.read_text(encoding="utf-8"))

    assert export.documents_count == 0
    assert export.errors == ("Unknown Market: aucun connecteur",)
    assert export.warnings == ()
    assert payload["errors"] == ["Unknown Market: aucun connecteur"]
    assert payload["warnings"] == []
    assert payload["market_summaries"] == [
        {
            "market": "Unknown Market",
            "source": "",
            "status": "error",
            "candidates_returned": 0,
            "documents_count": 0,
            "warning": "",
            "error": "aucun connecteur",
        }
    ]


def test_discover_market_document_links_warns_when_candidate_cap_is_hit(
    tmp_path: Path,
) -> None:
    candidates = [
        DocumentCandidate(
            title=f"Annual report {index}",
            url=f"https://official.test/report-{index}.pdf",
            published_date=date(2026, 6, 12),
            document_type="annual_financial_report",
            source="fake-oam",
            source_document_id=f"doc-{index}",
        )
        for index in range(2)
    ]
    connector = FakeSourceFirstConnector(candidates)

    export = discover_market_document_links(
        make_settings(tmp_path),
        markets=("Oslo Børs",),
        date_from=date(2026, 6, 10),
        date_to=date(2026, 6, 15),
        output_format="json",
        output_dir=tmp_path / "exports",
        max_candidates=1,
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: connector,
    )

    payload = json.loads(export.output_path.read_text(encoding="utf-8"))

    assert export.output_path.name.startswith("market_documents_oslo_brs_")
    assert export.warnings == (
        "Oslo Børs: le nombre de candidats retournés atteint "
        "--max-candidates=1; augmenter ce plafond pour prouver "
        "l'exhaustivité sur cette période",
    )
    assert payload["market_summaries"][0]["warning"] == (
        "le nombre de candidats retournés atteint --max-candidates=1; "
        "augmenter ce plafond pour prouver l'exhaustivité sur cette période"
    )


def test_discover_market_document_links_can_dedupe_urls_across_markets(
    tmp_path: Path,
) -> None:
    candidate = DocumentCandidate(
        title="Annual report",
        url="https://official.test/shared.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-1",
    )
    connectors = {
        "Euronext Brussels": FakeSourceFirstConnector([candidate]),
        "Euronext Growth Brussels": FakeSourceFirstConnector([candidate]),
    }

    export = discover_market_document_links(
        make_settings(tmp_path),
        markets=("Euronext Brussels", "Euronext Growth Brussels"),
        date_from=date(2026, 6, 10),
        date_to=date(2026, 6, 15),
        output_format="json",
        output_dir=tmp_path / "exports",
        dedupe_url=True,
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: connectors[market],
    )

    payload = json.loads(export.output_path.read_text(encoding="utf-8"))

    assert export.documents_count == 1
    assert payload["documents"][0]["url"] == "https://official.test/shared.pdf"
    assert payload["documents"][0]["market"] == (
        "Euronext Brussels, Euronext Growth Brussels"
    )
