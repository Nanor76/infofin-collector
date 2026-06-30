from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

import pytest

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch

PDF_BYTES = b"%PDF-spain-mock-content"

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
        stream: bool = False,
        timeout: int = 10,
    ) -> FakeDownloadResponse:
        self.downloads.append(url)
        return FakeDownloadResponse()

    def close(self) -> None:
        return None

class SpainStaticConnector(Connector):
    market = "Bolsa de Madrid"
    source_name = "spain_cnmv"
    supports_source_first = True

    def __init__(self, candidates: list[DocumentCandidate]) -> None:
        self.candidates = candidates
        self.recent_calls = 0
        self.issuer_calls = 0
        self.state = ConnectorState.READY
        self.last_error = None

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        self.recent_calls += 1
        self._scanned_notices = len(self.candidates)
        return self.candidates[:limit]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.issuer_calls += 1
        return [
            c for c in self.candidates
            if issuer.isin in c.metadata.get("issuer_isins", [])
        ]

    def estimate_recent_http_requests(self, since: date | None, limit: int | None) -> int:
        return 1

def make_settings(tmp_path: Path, max_download_bytes: int = 1024 * 1024) -> Settings:
    return Settings(
        db_path=tmp_path / "spain-watch.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=10,
        http_retries=0,
        http_backoff_factor=0,
        user_agent="test",
        max_download_bytes=max_download_bytes,
        amf_base_url="https://www.info-financiere.gouv.fr",
        amf_fallback_base_urls=(),
        amf_dataset="flux-amf-new-prod",
        amf_rows=100,
    )

def test_spain_watch_path_enrichment_and_idempotence(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Banco Santander, S.A.",
                "ES0113900J37",
                "SAN",
                "Bolsa de Madrid",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Banco Santander annual financial report 2025",
        url="https://example.com/download/ES0113900J37_2025.pdf",
        published_date=date(2026, 6, 10),
        document_type="annual_financial_report",
        source="spain_cnmv",
        source_document_id="202612345",
        metadata={
            "record_id": "202612345",
            "nif": "A39000013",
            "issuer_isins": ["ES0113900J37"],
            "detail_url": "https://example.com/santander",
            "spain_bme_company_url": "https://bme.com/santander",
            "home_member_state": "Spain",
            "pea_country_check": "eu_candidate",
        },
    )
    connector = SpainStaticConnector([candidate])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Bolsa de Madrid",
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

    issuer = database.list_issuers("Bolsa de Madrid")[0]
    assert issuer.spain_cnmv_record_id == "202612345"
    assert issuer.spain_cnmv_nif == "A39000013"
    assert issuer.spain_home_member_state == "Spain"
    assert issuer.spain_pea_country_check == "eu_candidate"

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
    assert row["source"] == "spain_cnmv"
    assert row["market"] == "Bolsa de Madrid"
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "spain" / "ES0113900J37"
    )

def test_spain_watch_modes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Banco Santander, S.A.",
                "ES0113900J37",
                "SAN",
                "Bolsa de Madrid",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Santander Report",
        url="https://example.com/santander.pdf",
        published_date=date(2026, 6, 10),
        document_type="annual_financial_report",
        source="spain_cnmv",
        source_document_id="202612345",
        metadata={"issuer_isins": ["ES0113900J37"]},
    )
    connector = SpainStaticConnector([candidate])
    session = FakeSession()

    # Mode 1: Daily watch run uses source-first (queries search_recent_documents, NOT search_documents)
    outcome_daily = run_watch(
        database,
        settings,
        market="Bolsa de Madrid",
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: session,
        connector_factory=lambda market, **kwargs: connector,
        now=lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )
    assert outcome_daily.status == "success"
    assert connector.recent_calls == 1
    assert connector.issuer_calls == 0

    # Reset calls
    connector.recent_calls = 0
    connector.issuer_calls = 0

    # Mode 2: Backfill watch run queries issuer-by-issuer (queries search_documents, NOT search_recent_documents)
    outcome_backfill = run_watch(
        database,
        settings,
        market="Bolsa de Madrid",
        dry_run=True,
        backfill=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: session,
        connector_factory=lambda market, **kwargs: connector,
        now=lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )
    assert outcome_backfill.status == "success"
    assert connector.recent_calls == 0
    assert connector.issuer_calls == 1

def test_spain_watch_max_download_mb(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_download_bytes=10)  # very small limit
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Banco Santander, S.A.",
                "ES0113900J37",
                "SAN",
                "Bolsa de Madrid",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Santander Large Report",
        url="https://example.com/large.pdf",
        published_date=date(2026, 6, 10),
        document_type="annual_financial_report",
        source="spain_cnmv",
        source_document_id="202612345",
        metadata={"issuer_isins": ["ES0113900J37"]},
    )
    connector = SpainStaticConnector([candidate])
    session = FakeSession()

    outcome = run_watch(
        database,
        settings,
        market="Bolsa de Madrid",
        dry_run=False,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: session,
        connector_factory=lambda market, **kwargs: connector,
        now=lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )

    assert outcome.stats.downloaded == 0
    assert outcome.stats.skipped_too_large == 1

@pytest.mark.skipif(not os.environ.get("RUN_LIVE_TESTS"), reason="RUN_LIVE_TESTS=1 not set")
def test_spain_live_connector(tmp_path: Path) -> None:
    from connectors.spain_cnmv import SpainCnmvConnector
    session = requests.Session()
    connector = SpainCnmvConnector(
        session=session,
        base_url="https://www.cnmv.es",
        bme_listed_companies_url="https://www.bolsasymercados.es",
        rate_limit_seconds=1.0,
    )
    diag = connector.diagnose()
    assert diag.http_status in (200, 503)  # either ready or under maintenance
