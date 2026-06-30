from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from connectors.portugal_cmvm_sdi import MemoryDownloadResponse
from db import Database
from models import Issuer
from watcher import run_watch


class FakeSession:
    def close(self) -> None:
        return None


class PortugalStaticConnector(Connector):
    market = "Euronext Lisbon"
    source_name = "cmvm_sdi"

    def __init__(self, candidate: DocumentCandidate) -> None:
        self.candidate = candidate
        self.state = ConnectorState.READY
        self.last_error = None

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return [self.candidate]


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "portugal.sqlite3",
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


def test_portugal_watch_download_and_idempotence(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Altri SGPS SA",
                "PTALT0AE0002",
                "ALTR",
                "Euronext Lisbon",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Relatório e Contas 2025",
        url="https://www.cmvm.pt/PInstitucional/PdfViewerInfPriv?Input=X",
        published_date=date(2026, 5, 4),
        document_type="annual_financial_report",
        source="cmvm_sdi",
        source_document_id="1383791:pdf",
        metadata={
            "cmvm_record_id": "1383791",
            "cmvm_sdi_url": "https://www.cmvm.pt/PInstitucional/Content",
            "detail_url": "https://www.cmvm.pt/PInstitucional/PdfViewerInfPriv?Input=X",
            "home_member_state": "Portugal",
            "cmvm_download_kind": "pdf",
            "file_format": "pdf",
        },
    )
    connector = PortugalStaticConnector(candidate)
    monkeypatch.setattr(
        "connectors.portugal_cmvm_sdi.fetch_cmvm_download",
        lambda *args, **kwargs: MemoryDownloadResponse(
            b"%PDF-portugal",
            content_type="application/pdf",
            filename="annual.pdf",
        ),
    )
    common = {
        "database": database,
        "settings": settings,
        "market": "Euronext Lisbon",
        "reports_dir": tmp_path / "reports",
        "session_factory": lambda **kwargs: FakeSession(),
        "connector_factory": lambda market, **kwargs: connector,
        "now": lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    }

    first = run_watch(**common)
    second = run_watch(**common)

    assert first.stats.downloaded == 1
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 1
    issuer = database.list_issuers("Euronext Lisbon")[0]
    assert issuer.portugal_cmvm_record_id == "1383791"
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
    assert row["source"] == "cmvm_sdi"
    assert row["market"] == "Euronext Lisbon"
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "portugal" / "PTALT0AE0002"
    )


def test_watch_all_includes_portugal(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Altri SGPS SA",
                "PTALT0AE0002",
                "ALTR",
                "Euronext Lisbon",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Relatório anual",
        url="https://www.cmvm.pt/PInstitucional/PdfViewerInfPriv?Input=DRY",
        published_date=date(2026, 5, 4),
        document_type="annual_financial_report",
        source="cmvm_sdi",
    )
    outcome = run_watch(
        database,
        settings,
        market=None,
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: PortugalStaticConnector(
            candidate
        ),
        now=lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )

    assert outcome.status == "success"
    assert outcome.market_stats["Euronext Lisbon"].candidates_found == 1
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "Portugal" in report
    assert "Euronext Lisbon" in report
