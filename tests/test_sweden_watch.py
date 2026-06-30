from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

import pytest
import requests

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch

PDF_BYTES = b"%PDF-sweden-mock-content"

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

class SwedenStaticConnector(Connector):
    market = "Nasdaq Stockholm"
    source_name = "sweden_fi"
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
        db_path=tmp_path / "sweden-watch.sqlite3",
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

def test_sweden_watch_path_enrichment_and_idempotence(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Ericsson, Telefonaktiebolaget LM",
                "SE0000108656",
                "ERIC B",
                "Nasdaq Stockholm",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Ericsson annual financial report 2025",
        url="https://example.com/download/SE0000108656_2025.pdf",
        published_date=date(2026, 6, 10),
        document_type="annual_financial_report",
        source="sweden_fi",
        source_document_id="rec-ericsson-123",
        metadata={
            "record_id": "rec-ericsson-123",
            "issuer_isins": ["SE0000108656"],
            "detail_url": "https://example.com/ericsson",
            "sweden_nasdaq_company_url": "https://nasdaq.com/ericsson",
            "home_member_state": "Sweden",
            "pea_country_check": "eu_candidate",
        },
    )
    connector = SwedenStaticConnector([candidate])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Nasdaq Stockholm",
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

    issuer = database.list_issuers("Nasdaq Stockholm")[0]
    assert issuer.sweden_fi_record_id == "rec-ericsson-123"
    assert issuer.sweden_home_member_state == "Sweden"
    assert issuer.sweden_pea_country_check == "eu_candidate"

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
    assert row["source"] == "sweden_fi"
    assert row["market"] == "Nasdaq Stockholm"
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "sweden" / "SE0000108656"
    )

def test_sweden_watch_modes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Ericsson, Telefonaktiebolaget LM",
                "SE0000108656",
                "ERIC B",
                "Nasdaq Stockholm",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Ericsson Report",
        url="https://example.com/ericsson.pdf",
        published_date=date(2026, 6, 10),
        document_type="annual_financial_report",
        source="sweden_fi",
        source_document_id="rec-ericsson-123",
        metadata={"issuer_isins": ["SE0000108656"]},
    )
    connector = SwedenStaticConnector([candidate])
    session = FakeSession()

    # Mode 1: Daily watch run uses source-first (queries search_recent_documents, NOT search_documents)
    outcome_daily = run_watch(
        database,
        settings,
        market="Nasdaq Stockholm",
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
        market="Nasdaq Stockholm",
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

def test_sweden_watch_max_download_mb(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_download_bytes=10)  # very small limit
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Ericsson, Telefonaktiebolaget LM",
                "SE0000108656",
                "ERIC B",
                "Nasdaq Stockholm",
            )
        ]
    )
    candidate = DocumentCandidate(
        title="Ericsson Large Report",
        url="https://example.com/large.pdf",
        published_date=date(2026, 6, 10),
        document_type="annual_financial_report",
        source="sweden_fi",
        source_document_id="rec-ericsson-123",
        metadata={"issuer_isins": ["SE0000108656"]},
    )
    connector = SwedenStaticConnector([candidate])
    session = FakeSession()

    outcome = run_watch(
        database,
        settings,
        market="Nasdaq Stockholm",
        dry_run=False,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: session,
        connector_factory=lambda market, **kwargs: connector,
        now=lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )

    assert outcome.stats.downloaded == 0
    assert outcome.stats.skipped_too_large == 1

def test_sweden_watch_since_filters_on_published_at_and_not_period_end_date(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Ericsson, Telefonaktiebolaget LM",
                "SE0000108656",
                "ERIC B",
                "Nasdaq Stockholm",
            )
        ]
    )
    
    # 1. Candidate with published_at = 2026-03-15 and period_end_date = 2025-12-31.
    # If since = 2026-01-01: should be accepted because published_at >= since, 
    # even though period_end_date < since.
    c1 = DocumentCandidate(
        title="Ericsson Report 1",
        url="https://example.com/ericsson1.pdf",
        published_date=date(2026, 3, 15),
        published_at=date(2026, 3, 15),
        period_end_date=date(2025, 12, 31),
        reporting_year=2025,
        document_type="annual_financial_report",
        source="sweden_fi",
        source_document_id="rec-ericsson-1",
        metadata={"issuer_isins": ["SE0000108656"]},
    )
    
    # 2. Candidate with published_at = None and period_end_date = 2024-12-31.
    # If since = 2025-01-01: should be rejected because period_end_date < since.
    c2 = DocumentCandidate(
        title="Ericsson Report 2",
        url="https://example.com/ericsson2.pdf",
        published_date=None,
        published_at=None,
        period_end_date=date(2024, 12, 31),
        reporting_year=2024,
        document_type="annual_financial_report",
        source="sweden_fi",
        source_document_id="rec-ericsson-2",
        metadata={"issuer_isins": ["SE0000108656"]},
    )

    # 3. Candidate with published_at = None and period_end_date = 2025-12-31.
    # If since = 2025-01-01: should be accepted because period_end_date >= since.
    c3 = DocumentCandidate(
        title="Ericsson Report 3",
        url="https://example.com/ericsson3.pdf",
        published_date=None,
        published_at=None,
        period_end_date=date(2025, 12, 31),
        reporting_year=2025,
        document_type="annual_financial_report",
        source="sweden_fi",
        source_document_id="rec-ericsson-3",
        metadata={"issuer_isins": ["SE0000108656"]},
    )

    connector = SwedenStaticConnector([c1, c2, c3])
    session = FakeSession()

    outcome = run_watch(
        database,
        settings,
        market="Nasdaq Stockholm",
        since=date(2025, 1, 1), # c1 and c3 should be accepted, c2 rejected (period 2024)
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: session,
        connector_factory=lambda market, **kwargs: connector,
        now=lambda: datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )
    
    # We should have found 2 accepted candidates (c1, c3)
    assert outcome.stats.candidates_found == 2
    
    # Verify the generated report contains the rejection details for c2 but not for c1 or c3
    report_content = outcome.report_path.read_text(encoding="utf-8")
    assert c2.url in report_content
    # Rejection reason for c2 should contain "antérieure au 2025-01-01"
    assert "antérieure au 2025-01-01" in report_content
    
    # c1 and c3 are accepted new documents, so they should appear in the report
    assert c1.url in report_content
    assert c3.url in report_content

@pytest.mark.skipif(not os.environ.get("RUN_LIVE_TESTS"), reason="RUN_LIVE_TESTS=1 not set")
def test_sweden_live_connector(tmp_path: Path) -> None:
    from connectors.sweden_fi import SwedenFiConnector
    session = requests.Session()
    connector = SwedenFiConnector(
        session=session,
        base_url="https://finanscentralen.fi.se",
        nasdaq_listed_companies_url="https://www.nasdaqomxnordic.com",
        rate_limit_seconds=1.0,
    )
    diag = connector.diagnose()
    assert diag.http_status in (200, 503)  # either ready or under maintenance
