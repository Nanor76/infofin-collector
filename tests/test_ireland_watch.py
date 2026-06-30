from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch

PDF_BYTES = b"%PDF-ireland-watch-fixture"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/octet-stream",
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


class IrelandStaticConnector(Connector):
    market = "Euronext Dublin"
    source_name = "euronext_direct"

    def __init__(self, document: DocumentCandidate) -> None:
        self.document = document
        self.state = ConnectorState.READY
        self.last_error = None

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return [self.document]


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "ireland-watch.sqlite3",
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


def test_ireland_watch_download_and_idempotence(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "BANK OF IRELAND GP",
                "IE00BD1RP616",
                "BIRG",
                "Euronext Dublin",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Bank of Ireland Group plc 2025 Annual Report",
        url=(
            "https://direct.euronext.com/api/PublicAnnouncements/"
            "OAMDocument/Announcement.pdf?id=fixture-pdf-id"
        ),
        published_date=date(2026, 3, 19),
        document_type="annual_financial_report",
        source="euronext_direct",
        source_document_id="fixture-pdf-id",
        metadata={
            "ireland_record_id": "fixture-pdf-id",
            "ireland_euronext_direct_url": "https://direct.euronext.com",
            "ireland_euronext_oam_url": (
                "https://direct.euronext.com/#/oamfiling"
            ),
            "detail_url": "https://direct.euronext.com/#/oamfiling",
            "home_member_state": "Ireland",
            "file_format": "pdf",
            "filename": "Announcement.pdf",
        },
    )
    connector = IrelandStaticConnector(candidate)
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Euronext Dublin",
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
    issuer = database.list_issuers("Euronext Dublin")[0]
    assert issuer.ireland_record_id == "fixture-pdf-id"
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
    assert row["source"] == "euronext_direct"
    assert row["market"] == "Euronext Dublin"
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "ireland" / "IE00BD1RP616"
    )


def test_watch_all_includes_ireland(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "BANK OF IRELAND GP",
                "IE00BD1RP616",
                "BIRG",
                "Euronext Dublin",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="2025 Annual Report",
        url="https://direct.euronext.com/annual.pdf?id=dry-run",
        published_date=date(2026, 3, 19),
        document_type="annual_financial_report",
        source="euronext_direct",
    )
    outcome = run_watch(
        database,
        settings,
        market=None,
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: IrelandStaticConnector(
            candidate
        ),
        now=lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )

    assert outcome.status == "success"
    assert outcome.market_stats["Euronext Dublin"].candidates_found == 1
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "Ireland" in report
    assert "Euronext Dublin" in report
