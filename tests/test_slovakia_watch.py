from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-slovakia-periodic-report"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/pdf",
        "Content-Length": str(len(PDF_BYTES)),
        "Content-Disposition": 'attachment; filename="00014122.pdf"',
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


class SlovakiaStaticConnector(Connector):
    market = "Bratislava Stock Exchange"
    source_name = "slovakia_nbs_ceri"
    supports_source_first = True
    requires_watchlist_queries = True

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
        return []

    def search_documents_for_issuer(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.issuer_calls += 1
        return self.candidates

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


def test_slovakia_watch_download_and_idempotence(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "slovakia.sqlite3",
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
            "Tatry mountain resorts, a.s.",
            "SK1120010287",
            "TMR",
            "Bratislava Stock Exchange",
            pea_geography_status="eu_candidate",
        )
    ])
    candidate = DocumentCandidate(
        title="Polročná finančná správa 2025 - 00014122.pdf",
        url="https://ceri.nbs.sk/static/data/00014/00014122.pdf",
        published_date=date(2025, 7, 31),
        published_at=date(2025, 7, 31),
        period_end_date=date(2025, 6, 30),
        reporting_year=2025,
        document_type="half_year_financial_report",
        source="slovakia_nbs_ceri",
        source_document_id="00014122.pdf:00014122.pdf",
        metadata={
            "official_source": 1,
            "issuer_name": "Tatry mountain resorts, a.s.",
            "filename": "00014122.pdf",
            "file_id": "00014122.pdf",
            "file_format": "pdf",
            "record_id": "00014122.pdf",
            "category": "Polročná finančná správa - zverejnenie",
            "slovakia_nbs_ceri_url": "https://ceri.nbs.sk/search",
            "parent_page_url": "https://ceri.nbs.sk/search",
            "home_member_state": "Slovakia",
            "pea_geography_status": "eu_candidate",
        },
        classification="half_year_financial_report",
        classification_reason="Periodic report term",
        matched_positive_terms=["polrocna financna sprava"],
        matched_negative_terms=[],
    )
    connector = SlovakiaStaticConnector([candidate])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Bratislava Stock Exchange",
        "since": date(2025, 1, 1),
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
    assert connector.issuer_calls == 2
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM documents WHERE source = 'slovakia_nbs_ceri'"
        ).fetchone()
        resolution = connection.execute(
            """
            SELECT * FROM issuer_source_resolutions
            WHERE source = 'slovakia_nbs_ceri'
            """
        ).fetchone()
        issuer = connection.execute(
            """
            SELECT pea_geography_status FROM issuers
            WHERE isin = 'SK1120010287'
            """
        ).fetchone()
        orphan_urls = connection.execute(
            """
            SELECT COUNT(*) FROM document_urls
            WHERE document_id NOT IN (SELECT id FROM documents)
            """
        ).fetchone()[0]
    assert row["source_document_id"] == "00014122.pdf:00014122.pdf"
    assert row["official_source"] == 1
    assert row["format"] == "pdf"
    assert row["published_at"] == "2025-07-31"
    assert row["period_end_date"] == "2025-06-30"
    assert row["reporting_year"] == 2025
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "slovakia" / "SK1120010287"
    )
    assert len(row["sha256"]) == 64
    assert resolution["home_member_state"] == "Slovakia"
    assert issuer["pea_geography_status"] == "eu_candidate"
    assert orphan_urls == 0