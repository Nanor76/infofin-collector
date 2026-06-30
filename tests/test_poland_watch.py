from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


ZIP_BYTES = b"PK\x03\x04poland-periodic-report"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/x-zip",
        "Content-Length": str(len(ZIP_BYTES)),
        "Content-Disposition": 'attachment; filename="report.zip"',
    }

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield ZIP_BYTES

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


class PolandStaticConnector(Connector):
    market = "Warsaw Stock Exchange"
    source_name = "knf_oam"
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
        self._scanned_notices = 120
        return self.candidates[:limit]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.issuer_calls += 1
        return self.candidates


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "poland-watch.sqlite3",
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


def fixed_clock() -> datetime:
    return datetime(2026, 6, 15, 8, 0, tzinfo=UTC)


def test_poland_source_first_download_storage_and_idempotence(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "MODIVO Spółka Akcyjna",
                "PLCCC0000016",
                "MODIVO",
                "Warsaw Stock Exchange",
                pea_geography_status="eu_candidate",
            )
        ]
    )
    periodic = DocumentCandidate(
        title="MODIVO consolidated quarterly financial report (QSr)",
        url=(
            "https://moam.knf.gov.pl/mOAM/2026/20260611/"
            "1n3mtukgq1_Raport.zip"
        ),
        published_date=date(2026, 6, 11),
        published_at=date(2026, 6, 11),
        reporting_year=2026,
        document_type="quarterly_financial_report",
        source="knf_oam",
        source_document_id=(
            "1FB8C7935B0B37A2C1258E13005B5DD4:"
            "1n3mtukgq1_Raport.zip"
        ),
        metadata={
            "official_source": 1,
            "issuer_name": "MODIVO Spółka Akcyjna",
            "home_member_state": "Poland",
            "report_code": "QSr",
            "report_number": None,
            "record_id": "1FB8C7935B0B37A2C1258E13005B5DD4",
            "knf_oam_record_id": "1FB8C7935B0B37A2C1258E13005B5DD4",
            "knf_oam_issuer_url": "https://moam.knf.gov.pl/moam.nsf/search",
            "knf_oam_detail_url": (
                "https://moam.knf.gov.pl/moam.nsf/0/"
                "1FB8C7935B0B37A2C1258E13005B5DD4"
            ),
            "filename": "1n3mtukgq1_Raport.zip",
            "file_id": "1FB8C7935B0B37A2C1258E13005B5DD4",
            "file_format": "zip",
        },
        classification="quarterly_financial_report",
        classification_reason="KNF OAM exact periodic form code: QSr",
        matched_positive_terms=["QSr"],
        matched_negative_terms=[],
    )
    rejected = DocumentCandidate(
        title="MODIVO current report",
        url=(
            "https://moam.knf.gov.pl/mOAM/2026/20260611/"
            "current_Raport.zip"
        ),
        published_date=date(2026, 6, 11),
        document_type="other_regulatory_announcement",
        source="knf_oam",
        source_document_id="CURRENT:current_Raport.zip",
        metadata={
            "issuer_name": "MODIVO Spółka Akcyjna",
            "report_code": "RB-W",
            "filename": "current_Raport.zip",
            "file_format": "zip",
        },
        classification="other_regulatory_announcement",
        classification_reason="KNF OAM current-report form rejected: RB-W",
        matched_positive_terms=[],
        matched_negative_terms=["RB-W"],
    )
    connector = PolandStaticConnector([periodic, rejected])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Warsaw Stock Exchange",
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
    assert session.downloads == [periodic.url]
    efficiency = next(iter(first.source_efficiency.values()))
    assert efficiency.mode == "source-first"
    assert efficiency.rejected_candidates == 1

    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT issuers.name AS issuer, issuers.isin, documents.source,
                   documents.source_document_id, documents.report_number,
                   documents.document_type, documents.published_at,
                   documents.period_end_date, documents.local_path,
                   documents.sha256, documents.file_size, documents.format,
                   documents.official_source
            FROM documents
            JOIN issuers ON issuers.id = documents.issuer_id
            """
        ).fetchone()
    assert row["source"] == "knf_oam"
    assert row["format"] == "zip"
    assert row["official_source"] == 1
    assert len(row["sha256"]) == 64
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "poland" / "PLCCC0000016"
    )
    assert Path(row["local_path"]).suffix == ".zip"
    report = first.report_path.read_text(encoding="utf-8")
    assert "RB-W" in report
    assert "Request efficiency" in report
