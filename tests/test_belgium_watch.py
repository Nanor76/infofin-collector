from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch

PDF_BYTES = b"%PDF-realistic-belgium-fixture"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/pdf",
        "Content-Length": str(len(PDF_BYTES)),
        "Content-Disposition": 'attachment; filename="annual-report.pdf"',
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


class BelgiumStaticConnector(Connector):
    market = "Euronext Brussels"
    source_name = "fsma_stori"

    def __init__(self, document: DocumentCandidate) -> None:
        self.document = document
        self.state = ConnectorState.READY
        self.last_error = None

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return [self.document]


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "belgium-watch.sqlite3",
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


def test_belgium_watch_path_enrichment_and_idempotence(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "AB INBEV",
                "BE0974293251",
                "ABI",
                "Euronext Brussels",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Annual financial report 2025",
        url=(
            "https://webapi.fsma.be/api/v1/en/stori/"
            "download?fileDataId=file-pdf"
        ),
        published_date=date(2026, 2, 12),
        document_type="annual_financial_report",
        source="fsma_stori",
        source_document_id="file-pdf",
        metadata={
            "fsma_record_id": "record-ab-inbev-2025",
            "stori_url": "https://www.fsma.be/en/stori",
            "detail_url": "https://www.fsma.be/en/stori",
            "home_member_state": "Belgium",
        },
    )
    connector = BelgiumStaticConnector(candidate)
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Euronext Brussels",
        "reports_dir": tmp_path / "reports",
        "session_factory": lambda **kwargs: session,
        "connector_factory": lambda market, **kwargs: connector,
        "now": lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    }

    first = run_watch(**common)
    second = run_watch(**common)

    assert first.stats.downloaded == 1
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 1
    assert session.downloads == [candidate.url]
    issuer = database.list_issuers("Euronext Brussels")[0]
    assert issuer.belgium_fsma_record_id == "record-ab-inbev-2025"
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
    assert row["source"] == "fsma_stori"
    assert row["market"] == "Euronext Brussels"
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "belgium" / "BE0974293251"
    )


def test_watch_all_includes_belgium(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "AB INBEV",
                "BE0974293251",
                "ABI",
                "Euronext Brussels",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Annual report",
        url="https://webapi.fsma.be/download?fileDataId=dry-run",
        published_date=date(2026, 2, 12),
        document_type="annual_financial_report",
        source="fsma_stori",
    )

    outcome = run_watch(
        database,
        settings,
        market=None,
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: (
            BelgiumStaticConnector(candidate)
        ),
        now=lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )

    assert outcome.status == "success"
    assert outcome.stats.issuers_checked == 1
    assert outcome.market_stats["Euronext Brussels"].candidates_found == 1
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "France + Oslo + Italie + Netherlands + Belgium" in report
    assert "Euronext Brussels" in report
