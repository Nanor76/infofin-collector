from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-denmark-periodic-report"


class FakeDownloadResponse:
    status_code = 200

    def __init__(self, content: bytes = PDF_BYTES) -> None:
        self.content = content
        self.headers = {
            "Content-Type": "application/pdf",
            "Content-Length": str(len(content)),
        }

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield self.content

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


class DenmarkStaticConnector(Connector):
    market = "Nasdaq Copenhagen"
    source_name = "dfsa_oam"
    supports_source_first = True

    def __init__(
        self,
        notices: list[DocumentCandidate],
        materialized: list[DocumentCandidate],
    ) -> None:
        self.notices = notices
        self.materialized = materialized
        self.recent_calls = 0
        self.issuer_calls = 0
        self._details_visited = 0
        self.state = ConnectorState.READY
        self.last_error = None

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        self.recent_calls += 1
        self._scanned_notices = len(self.notices)
        return self.notices[:limit]

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        self._details_visited += 1
        return self.materialized

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.issuer_calls += 1
        return self.materialized


def make_settings(
    tmp_path: Path,
    *,
    max_download_bytes: int = 1024 * 1024,
) -> Settings:
    return Settings(
        db_path=tmp_path / "denmark-watch.sqlite3",
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


def make_connector() -> DenmarkStaticConnector:
    notice = DocumentCandidate(
        title="Matas Group - Annual Report 2025/26",
        url="https://app.test/details/300009086",
        published_date=date(2026, 6, 10),
        document_type="annual_financial_report",
        source="dfsa_oam",
        source_document_id="300009086",
        metadata={
            "issuer_name": "MATAS A/S",
            "record_id": "300009086",
            "detail_url": "https://app.test/details/300009086",
        },
    )
    unmatched = DocumentCandidate(
        title="Other issuer annual report",
        url="https://app.test/details/other",
        published_date=date(2026, 6, 10),
        document_type="annual_financial_report",
        source="dfsa_oam",
        metadata={"issuer_name": "OTHER A/S"},
    )
    periodic = DocumentCandidate(
        title="Matas Annual Report 2025/26",
        url="https://files.test/matas-2026-03-31.pdf",
        published_date=date(2026, 6, 10),
        published_at=date(2026, 6, 10),
        period_end_date=date(2026, 3, 31),
        reporting_year=2026,
        document_type="annual_financial_report",
        source="dfsa_oam",
        source_document_id="300009086:report.pdf",
        metadata={
            "issuer_name": "MATAS A/S",
            "record_id": "300009086",
            "detail_url": "https://app.test/details/300009086",
            "denmark_home_member_state": "Denmark",
            "denmark_pea_country_check": "eu_candidate",
            "filename": "matas-2026-03-31.pdf",
        },
        classification="annual_financial_report",
        classification_reason="Periodic report term: annual report",
        matched_positive_terms=["annual report", "pdf"],
        matched_negative_terms=[],
    )
    rejected = DocumentCandidate(
        title="Matas major holding notification",
        url="https://files.test/matas-major-holding.pdf",
        published_date=date(2026, 6, 10),
        document_type="other_regulatory_announcement",
        source="dfsa_oam",
        classification="other_regulatory_announcement",
        classification_reason="Explicit exclusion term: major holding",
        matched_positive_terms=["pdf"],
        matched_negative_terms=["major holding"],
        metadata={"issuer_name": "MATAS A/S"},
    )
    return DenmarkStaticConnector([notice, unmatched], [periodic, rejected])


def prepare_database(settings: Settings) -> Database:
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "MATAS A/S",
                "DK0060497295",
                "MATAS",
                "Nasdaq Copenhagen",
            )
        ]
    )
    return database


def fixed_clock() -> datetime:
    return datetime(2026, 6, 13, 12, 0, tzinfo=UTC)


def test_denmark_source_first_filtering_download_and_idempotence(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = prepare_database(settings)
    connector = make_connector()
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Nasdaq Copenhagen",
        "reports_dir": tmp_path / "reports",
        "session_factory": lambda **kwargs: session,
        "connector_factory": lambda market, **kwargs: connector,
        "now": fixed_clock,
    }

    first = run_watch(**common)
    second = run_watch(**common)

    assert connector.recent_calls == 2
    assert connector.issuer_calls == 0
    assert connector.details_visited == 2
    assert first.stats.downloaded == 1
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 1
    assert session.downloads == ["https://files.test/matas-2026-03-31.pdf"]

    efficiency = next(iter(first.source_efficiency.values()))
    assert efficiency.mode == "source-first"
    assert efficiency.scanned_notices == 2
    assert efficiency.matched_issuers == 1
    assert efficiency.matched_candidates == 1
    assert efficiency.details_visited == 1
    assert efficiency.rejected_candidates == 1

    report = first.report_path.read_text(encoding="utf-8")
    assert "major holding" in report
    assert "Periodic report term: annual report" in report
    assert "Request efficiency" in report

    issuer = database.list_issuers("Nasdaq Copenhagen")[0]
    assert issuer.denmark_dfsa_record_id == "300009086"
    assert issuer.denmark_home_member_state == "Denmark"
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT documents.source, documents.source_url, documents.sha256,
                   documents.local_path, issuers.market
            FROM documents
            JOIN issuers ON issuers.id = documents.issuer_id
            """
        ).fetchone()
    assert row["source"] == "dfsa_oam"
    assert row["market"] == "Nasdaq Copenhagen"
    assert len(row["sha256"]) == 64
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "denmark" / "DK0060497295"
    )


def test_denmark_backfill_allows_issuer_mode(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = prepare_database(settings)
    connector = make_connector()
    outcome = run_watch(
        database,
        settings,
        market="Nasdaq Copenhagen",
        dry_run=True,
        backfill=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: connector,
        now=fixed_clock,
    )
    assert outcome.status == "success"
    assert connector.recent_calls == 0
    assert connector.issuer_calls == 1
    assert next(iter(outcome.source_efficiency.values())).mode == "backfill"


def test_denmark_regulatory_news_requires_explicit_option(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = prepare_database(settings)
    outcome = run_watch(
        database,
        settings,
        market="Nasdaq Copenhagen",
        dry_run=True,
        include_regulatory_news=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: make_connector(),
        now=fixed_clock,
    )
    efficiency = next(iter(outcome.source_efficiency.values()))
    assert outcome.stats.candidates_found == 2
    assert efficiency.rejected_candidates == 0


def test_denmark_max_download_size(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_download_bytes=10)
    database = prepare_database(settings)
    outcome = run_watch(
        database,
        settings,
        market="Nasdaq Copenhagen",
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: make_connector(),
        now=fixed_clock,
    )
    assert outcome.stats.downloaded == 0
    assert outcome.stats.skipped_too_large == 1
