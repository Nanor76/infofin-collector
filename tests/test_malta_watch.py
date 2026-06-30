from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-malta-periodic-report"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/pdf",
        "Content-Length": str(len(PDF_BYTES)),
        "Content-Disposition": 'attachment; filename="TRI88.pdf"',
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


class MaltaStaticConnector(Connector):
    market = "Malta Stock Exchange"
    source_name = "malta_mse_oam"
    supports_source_first = True

    def __init__(self, candidates: list[DocumentCandidate]) -> None:
        self.candidates = candidates
        self.recent_calls = 0
        self.issuer_calls = 0
        self.state = ConnectorState.READY
        self._scanned_notices = 1

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
    return datetime(2026, 6, 19, 8, 0, tzinfo=UTC)


def test_malta_watch_source_first_download_and_idempotence(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "malta.sqlite3",
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
            "Trident Estates plc",
            "MT0001670109",
            "TRI",
            "Malta Stock Exchange",
            pea_geography_status="eu_candidate",
        )
    ])
    candidate = DocumentCandidate(
        title="Annual Report and Financial Statements 2026 - TRI88.pdf",
        url="https://cdn.borzamalta.com.mt/download/announcements/TRI88.pdf",
        published_date=date(2026, 5, 28),
        published_at=date(2026, 5, 28),
        period_end_date=date(2026, 1, 31),
        reporting_year=2026,
        document_type="annual_financial_report",
        source="malta_mse_oam",
        source_document_id="trident:annual:TRI88",
        metadata={
            "official_source": 1,
            "issuer_name": "Trident Estates plc",
            "issuer_lei": "635400OFBLZGT1KLUV50",
            "filename": "TRI88.pdf",
            "file_id": "TRI88",
            "file_format": "pdf",
            "record_id": "trident:annual:TRI88",
            "malta_mse_oam_url": (
                "https://www.borzamalta.com.mt/officially-appointed-mechanism"
            ),
            "parent_page_url": (
                "https://www.borzamalta.com.mt/officially-appointed-mechanism"
            ),
            "home_member_state": "Malta",
            "pea_geography_status": "eu_candidate",
        },
        classification="annual_financial_report",
        classification_reason="Periodic report term",
        matched_positive_terms=["annual report"],
        matched_negative_terms=[],
    )
    connector = MaltaStaticConnector([candidate])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Malta Stock Exchange",
        "since": date(2026, 1, 1),
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
            "SELECT * FROM documents WHERE source = 'malta_mse_oam'"
        ).fetchone()
        issuer = connection.execute(
            """
            SELECT pea_geography_status FROM issuers
            WHERE isin = 'MT0001670109'
            """
        ).fetchone()
        orphan_urls = connection.execute(
            """
            SELECT COUNT(*) FROM document_urls
            WHERE document_id NOT IN (SELECT id FROM documents)
            """
        ).fetchone()[0]
    assert row["source_document_id"] == "trident:annual:TRI88"
    assert row["official_source"] == 1
    assert row["format"] == "pdf"
    assert row["published_at"] == "2026-05-28"
    assert row["period_end_date"] == "2026-01-31"
    assert row["reporting_year"] == 2026
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "malta" / "MT0001670109"
    )
    assert len(row["sha256"]) == 64
    assert issuer["pea_geography_status"] == "eu_candidate"
    assert orphan_urls == 0