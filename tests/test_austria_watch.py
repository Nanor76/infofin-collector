from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-austria-periodic-report"


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

    def get(self, url: str, **kwargs: object) -> FakeDownloadResponse:
        self.downloads.append(url)
        return FakeDownloadResponse()

    def close(self) -> None:
        return None


class AustriaStaticConnector(Connector):
    market = "Vienna Stock Exchange"
    source_name = "oekb_oam"
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
        self._scanned_notices = 5000
        return self.candidates[:limit]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.issuer_calls += 1
        return self.candidates


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "austria-watch.sqlite3",
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


def test_austria_source_first_download_storage_and_idempotence(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "AT & S Austria Technologie & Systemtechnik Aktiengesellschaft",
                "AT0000969985",
                "ATS",
                "Vienna Stock Exchange",
            )
        ]
    )
    periodic = DocumentCandidate(
        title="AT&S annual financial report",
        url="https://my.oekb.at/issuer-info/rest/public/meldedaten/download/249311",
        published_date=date(2026, 6, 11),
        published_at=date(2026, 6, 11),
        period_end_date=date(2026, 3, 31),
        reporting_year=2026,
        document_type="annual financial report",
        source="oekb_oam",
        source_document_id="249312:249311",
        metadata={
            "official_source": 1,
            "issuer_name": (
                "AT & S Austria Technologie & Systemtechnik "
                "Aktiengesellschaft"
            ),
            "issuer_isins": ["AT0000969985"],
            "austria_oekb_oam_id": "1041",
            "austria_oekb_oam_issuer_url": "https://my.oekb.at/list",
            "austria_oekb_oam_detail_url": "https://my.oekb.at/list",
            "home_member_state": "Austria",
            "pea_country_check": "eu_candidate",
            "filename": "ATS_Annual-Financial-Report_2025-26.pdf",
            "file_id": "249311",
            "file_format": "pdf",
        },
        classification="annual financial report",
        classification_reason="OeKB periodic category: EP_JFB",
        matched_positive_terms=["EP_JFB"],
        matched_negative_terms=[],
    )
    rejected = DocumentCandidate(
        title="AT&S inside information",
        url="https://my.oekb.at/issuer-info/rest/public/meldedaten/download/249399",
        published_date=date(2026, 6, 11),
        document_type="other_regulatory_announcement",
        source="oekb_oam",
        source_document_id="249400:249399",
        metadata={
            "issuer_name": periodic.metadata["issuer_name"],
            "issuer_isins": ["AT0000969985"],
            "filename": "inside-information.pdf",
            "file_id": "249399",
            "file_format": "pdf",
        },
        classification="other_regulatory_announcement",
        classification_reason="OeKB excluded regulatory category: EP_AD_HOC",
        matched_positive_terms=[],
        matched_negative_terms=["EP_AD_HOC"],
    )
    connector = AustriaStaticConnector([periodic, rejected])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Vienna Stock Exchange",
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
    assert efficiency.scanned_notices == 5000
    assert efficiency.rejected_candidates == 1
    assert efficiency.http_calls == 1

    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT issuers.name AS issuer, issuers.isin, documents.source,
                   documents.source_document_id, documents.document_type,
                   documents.published_at, documents.period_end_date,
                   documents.local_path, documents.sha256,
                   documents.file_size, documents.format,
                   documents.official_source
            FROM documents
            JOIN issuers ON issuers.id = documents.issuer_id
            """
        ).fetchone()
    assert row["source"] == "oekb_oam"
    assert row["source_document_id"] == "249312:249311"
    assert row["format"] == "pdf"
    assert row["official_source"] == 1
    assert len(row["sha256"]) == 64
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "austria" / "AT0000969985"
    )
    assert Path(row["local_path"]).name.endswith("_249311.pdf")
    report = first.report_path.read_text(encoding="utf-8")
    assert "EP_AD_HOC" in report
    assert "Request efficiency" in report
