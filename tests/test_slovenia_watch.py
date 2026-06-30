from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


XBRI_BYTES = b"PK\x03\x04slovenia-esef-report"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(XBRI_BYTES)),
        "Content-Disposition": (
            'attachment; filename="5493003GE7UJGPQAMN79-2025-12-31-1-en.xbri"'
        ),
    }

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield XBRI_BYTES

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


class SloveniaStaticConnector(Connector):
    market = "Ljubljana Stock Exchange"
    source_name = "slovenia_oam"
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


def test_slovenia_watch_download_and_idempotence(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "slovenia.sqlite3",
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
            "TELEKOM SLOVENIJE, d.d., Ljubljana",
            "SI0031104290",
            "TLSG",
            "Ljubljana Stock Exchange",
            pea_geography_status="eu_candidate",
        )
    ])
    candidate = DocumentCandidate(
        title="Audited annual report 2025",
        url="https://www.oam.si/file.aspx?AttachmentID=29439",
        published_date=date(2026, 4, 17),
        published_at=date(2026, 4, 17),
        period_end_date=date(2025, 12, 31),
        reporting_year=2025,
        document_type="annual_financial_report",
        source="slovenia_oam",
        source_document_id="39591:29439",
        metadata={
            "official_source": 1,
            "issuer_name": "TELEKOM SLOVENIJE, d.d., Ljubljana",
            "issuer_lei": "5493003GE7UJGPQAMN79",
            "filename": "5493003GE7UJGPQAMN79-2025-12-31-1-en.xbri",
            "file_id": "29439",
            "file_format": "xbri",
            "record_id": "39591",
            "detail_url": "https://www.oam.si/detail/39591",
            "slovenia_oam_url": "https://www.oam.si/default_en.aspx",
            "parent_page_url": "https://www.oam.si/detail/39591",
            "home_member_state": "Slovenia",
            "pea_geography_status": "eu_candidate",
        },
        classification="annual_financial_report",
        classification_reason="OAM Slovenia exact annual category",
        matched_positive_terms=["annual report"],
        matched_negative_terms=[],
    )
    connector = SloveniaStaticConnector([candidate])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Ljubljana Stock Exchange",
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
            "SELECT * FROM documents WHERE source = 'slovenia_oam'"
        ).fetchone()
        resolution = connection.execute(
            """
            SELECT * FROM issuer_source_resolutions
            WHERE source = 'slovenia_oam'
            """
        ).fetchone()
        issuer = connection.execute(
            """
            SELECT pea_geography_status FROM issuers
            WHERE isin = 'SI0031104290'
            """
        ).fetchone()
    assert row["source_document_id"] == "39591:29439"
    assert row["official_source"] == 1
    assert row["format"] == "xbri"
    assert row["published_at"] == "2026-04-17"
    assert row["period_end_date"] == "2025-12-31"
    assert row["reporting_year"] == 2025
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "slovenia" / "SI0031104290"
    )
    assert len(row["sha256"]) == 64
    assert resolution["home_member_state"] == "Slovenia"
    assert issuer["pea_geography_status"] == "eu_candidate"
