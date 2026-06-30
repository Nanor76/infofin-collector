from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch

PDF_BYTES = b"%PDF-realistic-italy-fixture"


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

    def get(
        self,
        url: str,
        *,
        stream: bool,
        timeout: int,
    ) -> FakeDownloadResponse:
        self.downloads.append(url)
        return FakeDownloadResponse()

    def close(self) -> None:
        return None


class ItalyStaticConnector(Connector):
    market = "Euronext Milan"
    source_name = "emarketstorage"

    def __init__(self, document: DocumentCandidate) -> None:
        self.document = document
        self.state = ConnectorState.READY
        self.last_error = None

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return [self.document]


def test_italy_watch_path_persistence_and_second_run_idempotence(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "italy-watch.sqlite3",
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
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "LANDI RENZO",
                "IT0005619843",
                "LR",
                "Euronext Milan",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Relazione finanziaria annuale 2025",
        url="https://www.emarketstorage.it/report-185771.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="emarketstorage",
        source_document_id="185771",
    )
    connector = ItalyStaticConnector(candidate)
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Euronext Milan",
        "reports_dir": tmp_path / "reports",
        "session_factory": lambda **kwargs: session,
        "connector_factory": lambda market, **kwargs: connector,
        "now": lambda: datetime(2026, 6, 12, 12, 0, tzinfo=UTC),
    }

    first = run_watch(**common)
    second = run_watch(**common)

    assert first.stats.downloaded == 1
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 1
    assert session.downloads == [candidate.url]
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT documents.source_url, documents.sha256,
                   documents.source, documents.local_path,
                   issuers.market, issuers.name
            FROM documents
            JOIN issuers ON issuers.id = documents.issuer_id
            """
        ).fetchone()
    assert row["source_url"] == candidate.url
    assert len(row["sha256"]) == 64
    assert row["source"] == "emarketstorage"
    assert row["market"] == "Euronext Milan"
    assert row["name"] == "LANDI RENZO"
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "italy" / "IT0005619843"
    )
