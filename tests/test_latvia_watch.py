from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-latvia-periodic-report"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/pdf",
        "Content-Length": str(len(PDF_BYTES)),
        "Content-Disposition": 'attachment; filename="Latvenergo_Annual_Report_2025.pdf"',
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


class LatviaStaticConnector(Connector):
    market = "Riga Stock Exchange"
    source_name = "latvia_oam"
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
        self._scanned_notices = 1
        return self.candidates[:limit]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.issuer_calls += 1
        return self.candidates


def fixed_clock() -> datetime:
    return datetime(2026, 6, 15, 8, 0, tzinfo=UTC)


def test_latvia_watch_download_and_idempotence(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "latvia.sqlite3",
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
            "Latvenergo, AS",
            "LV0000870129",
            "ELEK024227A",
            "Riga Stock Exchange",
            pea_geography_status="eu_candidate",
        )
    ])
    candidate = DocumentCandidate(
        title=(
            "Latvenergo Group Consolidated and Latvenergo AS Annual Report "
            "2025 - Latvenergo_Annual_Report_2025.pdf"
        ),
        url=(
            "https://csri.investinfo.lv/?task=download&doc_id=24885&"
            "doc_version=1&f_id=22080"
        ),
        published_date=date(2026, 4, 29),
        published_at=date(2026, 4, 29),
        period_end_date=date(2025, 12, 31),
        reporting_year=2025,
        document_type="annual_financial_report",
        source="latvia_oam",
        source_document_id="24885:22080",
        metadata={
            "official_source": 1,
            "issuer_name": "Latvenergo, AS",
            "filename": "Latvenergo_Annual_Report_2025.pdf",
            "file_id": "22080",
            "file_format": "pdf",
            "record_id": "24885",
            "detail_url": "https://csri.investinfo.lv/?view=csridocumentsdetails&id=24885",
            "latvia_oam_url": "https://csri.investinfo.lv/lv/",
            "parent_page_url": "https://csri.investinfo.lv/?view=csridocumentsdetails&id=24885",
            "home_member_state": "Latvia",
            "pea_geography_status": "eu_candidate",
        },
        classification="annual_financial_report",
        classification_reason="OAM Latvia exact annual category",
        matched_positive_terms=["annual report"],
        matched_negative_terms=[],
    )
    connector = LatviaStaticConnector([candidate])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Riga Stock Exchange",
        "since": date(2026, 4, 1),
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
            "SELECT * FROM documents WHERE source = 'latvia_oam'"
        ).fetchone()
        resolution = connection.execute(
            """
            SELECT * FROM issuer_source_resolutions
            WHERE source = 'latvia_oam'
            """
        ).fetchone()
        issuer = connection.execute(
            """
            SELECT pea_geography_status FROM issuers
            WHERE isin = 'LV0000870129'
            """
        ).fetchone()
    assert row["source_document_id"] == "24885:22080"
    assert row["official_source"] == 1
    assert row["format"] == "pdf"
    assert row["published_at"] == "2026-04-29"
    assert row["period_end_date"] == "2025-12-31"
    assert row["reporting_year"] == 2025
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "latvia" / "LV0000870129"
    )
    assert len(row["sha256"]) == 64
    assert resolution["home_member_state"] == "Latvia"
    assert issuer["pea_geography_status"] == "eu_candidate"
