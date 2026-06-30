from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-lithuania-periodic-report"


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/pdf",
        "Content-Length": str(len(PDF_BYTES)),
        "Content-Disposition": (
            'attachment; filename="Kvartalas-EU-PIE_stand_alone_LT.pdf"'
        ),
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


class LithuaniaStaticConnector(Connector):
    market = "Vilnius Stock Exchange"
    source_name = "lithuania_oam"
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
    return datetime(2026, 6, 18, 8, 0, tzinfo=UTC)


def test_lithuania_watch_download_and_idempotence(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "lithuania.sqlite3",
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
            "UAB Kvartalas",
            "LT0000411167",
            "KVART",
            "Vilnius Stock Exchange",
            pea_geography_status="eu_candidate",
        )
    ])
    candidate = DocumentCandidate(
        title=(
            "Patikslinta 2025 m. finansinių ataskaitų audito išvada - "
            "Kvartalas - EU PIE_stand alone_LT.pdf"
        ),
        url=(
            "https://www.oam.lt/cns-web/oam/viewAttachment.action?"
            "messageAttachmentId=344034"
        ),
        published_date=date(2026, 6, 15),
        published_at=date(2026, 6, 15),
        period_end_date=date(2025, 12, 31),
        reporting_year=2025,
        document_type="annual_financial_report",
        source="lithuania_oam",
        source_document_id="473009:344034",
        metadata={
            "official_source": 1,
            "issuer_name": "UAB Kvartalas",
            "filename": "Kvartalas - EU PIE_stand alone_LT.pdf",
            "file_id": "344034",
            "file_format": "pdf",
            "record_id": "473009",
            "detail_url": "https://www.oam.lt/view/473009?lang=lt",
            "lithuania_oam_url": "https://www.oam.lt/",
            "parent_page_url": "https://www.oam.lt/view/473009?lang=lt",
            "home_member_state": "Lithuania",
            "pea_geography_status": "eu_candidate",
        },
        classification="annual_financial_report",
        classification_reason="OAM Lithuania exact annual-information category 171",
        matched_positive_terms=["171"],
        matched_negative_terms=[],
    )
    connector = LithuaniaStaticConnector([candidate])
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Vilnius Stock Exchange",
        "since": date(2026, 6, 1),
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
            "SELECT * FROM documents WHERE source = 'lithuania_oam'"
        ).fetchone()
        resolution = connection.execute(
            """
            SELECT * FROM issuer_source_resolutions
            WHERE source = 'lithuania_oam'
            """
        ).fetchone()
        issuer = connection.execute(
            """
            SELECT pea_geography_status FROM issuers
            WHERE isin = 'LT0000411167'
            """
        ).fetchone()
        orphan_urls = connection.execute(
            """
            SELECT COUNT(*) FROM document_urls
            WHERE document_id NOT IN (SELECT id FROM documents)
            """
        ).fetchone()[0]
    assert row["source_document_id"] == "473009:344034"
    assert row["official_source"] == 1
    assert row["format"] == "pdf"
    assert row["published_at"] == "2026-06-15"
    assert row["period_end_date"] == "2025-12-31"
    assert row["reporting_year"] == 2025
    assert Path(row["local_path"]).parent == (
        settings.data_dir / "lithuania" / "LT0000411167"
    )
    assert len(row["sha256"]) == 64
    assert resolution["home_member_state"] == "Lithuania"
    assert issuer["pea_geography_status"] == "eu_candidate"
    assert orphan_urls == 0