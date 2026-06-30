from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-bulgaria-periodic-report"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/pdf",
        "Content-Length": str(len(PDF_BYTES)),
        "Content-Disposition": 'attachment; filename="Doklad.pdf"',
    }

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield PDF_BYTES

    def close(self) -> None:
        return None


class FakeSession:
    def get(self, url: str, **kwargs: object) -> FakeDownloadResponse:
        return FakeDownloadResponse()

    def close(self) -> None:
        return None


class BulgariaStaticConnector(Connector):
    market = "Bulgarian Stock Exchange"
    source_name = "bulgaria_bse_x3news"
    supports_source_first = True

    def __init__(self, candidates: list[DocumentCandidate]) -> None:
        self.candidates = candidates
        self.recent_calls = 0
        self.issuer_calls = 0
        self.state = ConnectorState.READY
        self._scanned_notices = len(candidates)

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        self.recent_calls += 1
        return self.candidates

    def search_documents_for_issuer(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.issuer_calls += 1
        return []

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        return [candidate]


def fixed_clock() -> datetime:
    return datetime(2026, 6, 18, 8, 0, tzinfo=UTC)


def test_bulgaria_watch_download_and_idempotence(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "bulgaria.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=10,
        http_retries=0,
        http_backoff_factor=0,
        user_agent="test",
        max_download_bytes=1024 * 1024,
        amf_base_url="https://example.test",
        amf_fallback_base_urls=(),
        amf_dataset="test",
        amf_rows=100,
    )
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers([
        Issuer(
            "Тибиш ЕАД",
            "BG00TBISH001",
            "TIB",
            "Bulgarian Stock Exchange",
            pea_geography_status="eu_candidate",
        )
    ])
    candidate = DocumentCandidate(
        title="Финансови отчети към 31.12.2024/ - Доклад за дейността.pdf",
        url="https://download.bse-sofia.bg/x3news_companies/example/Doklad.pdf",
        published_date=date(2026, 6, 1),
        document_type="annual_financial_report",
        source="bulgaria_bse_x3news",
        source_document_id="bulgaria-test-doc-1",
        metadata={
            "issuer_name": "Тибиш ЕАД",
            "issuer_aliases": ["Тибиш ЕАД"],
            "strict_issuer_name_match": True,
            "filename": "Доклад за дейността.pdf",
            "file_id": "bulgaria-test-doc-1",
            "file_format": "pdf",
            "pea_geography_status": "eu_candidate",
        },
        classification="annual_financial_report",
        published_at=date(2026, 6, 1),
        period_end_date=date(2024, 12, 31),
        reporting_year=2024,
    )
    connector = BulgariaStaticConnector([candidate])

    def connector_factory(market: str, **kwargs: object) -> Connector | None:
        if market == "Bulgarian Stock Exchange":
            return connector
        return None

    first = run_watch(
        database,
        settings,
        market="Bulgarian Stock Exchange",
        dry_run=False,
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=connector_factory,
        now=fixed_clock,
        lookback_days=30,
        max_candidates_per_source=40,
    )
    assert first.stats.downloaded == 1
    assert connector.recent_calls == 1
    assert connector.issuer_calls == 0

    second = run_watch(
        database,
        settings,
        market="Bulgarian Stock Exchange",
        dry_run=False,
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=connector_factory,
        now=fixed_clock,
        lookback_days=30,
        max_candidates_per_source=40,
    )
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 1