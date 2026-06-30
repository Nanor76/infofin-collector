from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-croatia-periodic-report"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/pdf",
        "Content-Length": str(len(PDF_BYTES)),
    }

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield PDF_BYTES

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.downloads: list[str] = []

    def get(self, url: str, **kwargs: object) -> FakeDownloadResponse:
        self.downloads.append(url)
        return FakeDownloadResponse()

    def close(self) -> None:
        return None


class CroatiaStaticConnector(Connector):
    market = "Zagreb Stock Exchange"
    source_name = "croatia_hanfa_srpi"
    supports_source_first = True

    def __init__(self, candidates: list[DocumentCandidate]) -> None:
        self.candidates = candidates
        self.recent_calls = 0
        self.issuer_calls = 0
        self.state = ConnectorState.READY

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        self.recent_calls += 1
        self._scanned_notices = 3
        return self.candidates[:limit]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.issuer_calls += 1
        return self.candidates


def fixed_clock() -> datetime:
    return datetime(2026, 6, 15, 8, 0, tzinfo=UTC)


def test_croatia_watch_download_and_idempotence(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "croatia.sqlite3",
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
            "ĐURO ĐAKOVIĆ GRUPA d.d.",
            "HRDDJTRA0007",
            "DDJH",
            "Zagreb Stock Exchange",
            pea_geography_status="eu_candidate",
        )
    ])
    candidate = DocumentCandidate(
        title="Annual financial report - report.pdf",
        url="https://www.hanfa.hr/SRPI/HR/2026/2026_06_05-1234447_pdf.pdf",
        published_date=date(2026, 6, 10),
        published_at=date(2026, 6, 10),
        period_end_date=date(2025, 12, 31),
        reporting_year=2025,
        document_type="annual_financial_report",
        source="croatia_hanfa_srpi",
        source_document_id="1234447:pdf",
        metadata={
            "official_source": 1,
            "issuer_name": "ĐURO ĐAKOVIĆ GRUPA d.d.",
            "filename": "annual-report-2025.pdf",
            "file_id": "1234447:pdf",
            "file_format": "pdf",
            "hanfa_srpi_url": "https://www.hanfa.hr/oam",
            "parent_page_url": "https://www.hanfa.hr/oam",
        },
        classification="annual_financial_report",
        classification_reason="HANFA SRPI exact periodic category 17",
        matched_positive_terms=["Annual financial report"],
        matched_negative_terms=[],
    )
    connector = CroatiaStaticConnector([candidate])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Zagreb Stock Exchange",
        "reports_dir": tmp_path / "reports",
        "session_factory": lambda **kwargs: session,
        "connector_factory": lambda market, **kwargs: connector,
        "now": fixed_clock,
    }

    first = run_watch(**common)
    second = run_watch(**common)

    assert first.stats.downloaded == 1
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 1
    assert connector.recent_calls == 2
    assert connector.issuer_calls == 0
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM documents WHERE source = 'croatia_hanfa_srpi'"
        ).fetchone()
    assert row["source_document_id"] == "1234447:pdf"
    assert row["official_source"] == 1
    assert row["format"] == "pdf"
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "croatia" / "HRDDJTRA0007"
    )
    assert len(row["sha256"]) == 64
