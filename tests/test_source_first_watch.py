from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-source-first-test"


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

    def get(self, url: str, **kwargs: object) -> FakeDownloadResponse:
        self.downloads.append(url)
        return FakeDownloadResponse()

    def close(self) -> None:
        return None


class SourceFirstConnector(Connector):
    supports_source_first = True

    def __init__(
        self,
        *,
        source_name: str,
        market: str,
        candidates: list[DocumentCandidate],
        estimated_requests: int = 1,
    ) -> None:
        self.source_name = source_name
        self.market = market
        self.candidates = candidates
        self.estimated_requests = estimated_requests
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
            candidate
            for candidate in self.candidates
            if issuer.isin in candidate.metadata.get("issuer_isins", [])
        ]

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        return self.estimated_requests


class HttpQueryConnector(SourceFirstConnector):
    def __init__(
        self,
        *,
        session: object,
        query_requests: int,
    ) -> None:
        super().__init__(
            source_name="france-http-source",
            market="Euronext Paris",
            candidates=[],
        )
        self.session = session
        self.query_requests = query_requests

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        self.recent_calls += 1
        for index in range(self.query_requests):
            response = self.session.get(
                f"https://official.test/query/{index}",
                timeout=10,
            )
            response.close()
        return []


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "source-first.sqlite3",
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


def make_candidate(
    *,
    source: str,
    isin: str,
    company: str,
    url: str,
) -> DocumentCandidate:
    return DocumentCandidate(
        title=f"{company} annual financial report 2025",
        url=url,
        published_date=date(2026, 6, 10),
        document_type="annual_financial_report",
        source=source,
        metadata={
            "issuer_isins": [isin],
            "issuer_name": company,
        },
    )


def fixed_clock() -> datetime:
    return datetime(2026, 6, 13, 8, 30, tzinfo=UTC)


def test_daily_watch_all_uses_one_source_query_per_market_group(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris"),
            Issuer("Danone", "FR0000120644", "BN", "Euronext Paris"),
            Issuer("Orange", "FR0000133308", "ORA", "Euronext Paris"),
            Issuer(
                "Bank of Ireland Group",
                "IE00BD1RP616",
                "BIRG",
                "Euronext Dublin",
            ),
            Issuer(
                "Ryanair Holdings",
                "IE00BYTBXV33",
                "RYA",
                "Euronext Dublin",
            ),
        ]
    )
    france = SourceFirstConnector(
        source_name="france-source",
        market="Euronext Paris",
        candidates=[
            make_candidate(
                source="france-source",
                isin="FR0000120073",
                company="Air Liquide",
                url="https://official.test/france.pdf",
            )
        ],
    )
    ireland = SourceFirstConnector(
        source_name="ireland-source",
        market="Euronext Dublin",
        candidates=[
            make_candidate(
                source="ireland-source",
                isin="IE00BD1RP616",
                company="Bank of Ireland Group",
                url="https://official.test/ireland.pdf",
            )
        ],
    )

    outcome = run_watch(
        database,
        settings,
        market=None,
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: (
            france if market == "Euronext Paris" else ireland
        ),
        now=fixed_clock,
    )

    assert outcome.status == "success"
    assert france.recent_calls == 1
    assert ireland.recent_calls == 1
    assert france.issuer_calls == 0
    assert ireland.issuer_calls == 0
    assert outcome.stats.issuers_checked == 5
    assert outcome.stats.candidates_found == 2
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "## Request efficiency" in report
    assert "| france-source | Euronext Paris | source-first |" in report
    assert "| ireland-source | Euronext Dublin | source-first |" in report


def test_backfill_explicitly_enables_issuer_queries(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris"),
            Issuer("Danone", "FR0000120644", "BN", "Euronext Paris"),
        ]
    )
    connector = SourceFirstConnector(
        source_name="france-source",
        market="Euronext Paris",
        candidates=[],
    )

    outcome = run_watch(
        database,
        settings,
        market="Euronext Paris",
        dry_run=True,
        backfill=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: connector,
        now=fixed_clock,
    )

    assert outcome.status == "success"
    assert connector.recent_calls == 0
    assert connector.issuer_calls == 2
    assert next(iter(outcome.source_efficiency.values())).mode == "backfill"


def test_large_run_guard_requires_explicit_confirmation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")]
    )
    connector = SourceFirstConnector(
        source_name="france-source",
        market="Euronext Paris",
        candidates=[],
        estimated_requests=501,
    )
    common = {
        "database": database,
        "settings": settings,
        "market": "Euronext Paris",
        "dry_run": True,
        "reports_dir": tmp_path / "reports",
        "session_factory": lambda **kwargs: FakeSession(),
        "connector_factory": lambda market, **kwargs: connector,
        "now": fixed_clock,
    }

    blocked = run_watch(**common)
    confirmed = run_watch(**common, confirm_large_run=True)

    assert blocked.status == "failed"
    assert connector.recent_calls == 1
    assert "au-dessus du garde-fou de 500" in blocked.report_path.read_text(
        encoding="utf-8"
    )
    assert confirmed.status == "success"


def test_http_request_counter_and_runtime_guard(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")]
    )

    counted = run_watch(
        database,
        settings,
        market="Euronext Paris",
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: HttpQueryConnector(
            session=kwargs["session"],
            query_requests=2,
        ),
        now=fixed_clock,
    )
    blocked = run_watch(
        database,
        settings,
        market="Euronext Paris",
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: HttpQueryConnector(
            session=kwargs["session"],
            query_requests=501,
        ),
        now=fixed_clock,
    )

    counted_efficiency = next(iter(counted.source_efficiency.values()))
    blocked_efficiency = next(iter(blocked.source_efficiency.values()))
    assert counted.status == "success"
    assert counted_efficiency.http_calls == 2
    assert blocked.status == "failed"
    assert blocked_efficiency.http_calls == 500
    assert "dépasserait la limite de 500 appels HTTP" in (
        blocked.report_path.read_text(encoding="utf-8")
    )


def test_source_first_watch_all_remains_idempotent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")]
    )
    candidate = make_candidate(
        source="france-source",
        isin="FR0000120073",
        company="Air Liquide",
        url="https://official.test/france.pdf",
    )
    connector = SourceFirstConnector(
        source_name="france-source",
        market="Euronext Paris",
        candidates=[candidate],
    )
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": None,
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
    assert session.downloads == [candidate.url]
