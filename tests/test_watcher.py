from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Iterator

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from models import Issuer
from watcher import run_watch


PDF_BYTES = b"%PDF-infofin-watch-test"


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
        stream: bool,
        timeout: int,
    ) -> FakeDownloadResponse:
        self.downloads.append(url)
        return FakeDownloadResponse()

    def close(self) -> None:
        return None


class StaticConnector(Connector):
    market = "Euronext Paris"
    source_name = "france-test"

    def __init__(
        self,
        candidates_by_isin: dict[str, list[DocumentCandidate]],
    ) -> None:
        self.candidates_by_isin = candidates_by_isin
        self.state = ConnectorState.READY
        self.last_error = None

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.state = ConnectorState.READY
        self.last_error = None
        return self.candidates_by_isin.get(issuer.isin, [])


class RecoveringDegradedConnector(StaticConnector):
    def __init__(
        self,
        failing_isin: str,
        candidates_by_isin: dict[str, list[DocumentCandidate]],
    ) -> None:
        super().__init__(candidates_by_isin)
        self.failing_isin = failing_isin

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        if issuer.isin == self.failing_isin:
            self.mark_degraded("réseau: timeout source France")
            return []
        return super().search_documents(issuer)


class AlwaysDegradedConnector(StaticConnector):
    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.mark_degraded("réseau: source France indisponible")
        return []


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "infofin.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=10,
        http_retries=0,
        http_backoff_factor=0.0,
        user_agent="test",
        max_download_bytes=1024 * 1024,
        amf_base_url="https://www.info-financiere.gouv.fr",
        amf_fallback_base_urls=(),
        amf_dataset="flux-amf-new-prod",
        amf_rows=100,
    )


def candidate(
    url: str,
    *,
    published: date = date(2026, 6, 10),
    title: str = "Rapport financier annuel 2025",
) -> DocumentCandidate:
    return DocumentCandidate(
        title=title,
        url=url,
        published_date=published,
        document_type="annual_financial_report",
        source="france-test",
    )


def fixed_clock() -> datetime:
    return datetime(2026, 6, 12, 8, 30, tzinfo=UTC)


def connector_factory(connector: Connector):
    def create(market: str, **kwargs: object) -> Connector:
        return connector

    return create


def session_factory(session: FakeSession):
    def create(**kwargs: object) -> FakeSession:
        return session

    return create


def initialize_issuers(
    database: Database,
    issuers: list[Issuer],
) -> None:
    database.initialize()
    database.upsert_issuers(issuers)


def test_second_watch_run_marks_known_url_duplicate(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    initialize_issuers(
        database,
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")],
    )
    document = candidate("https://official.test/air-liquide-rfa.pdf")
    connector = StaticConnector({"FR0000120073": [document]})
    session = FakeSession()

    first = run_watch(
        database,
        settings,
        market="Euronext Paris",
        reports_dir=tmp_path / "reports",
        session_factory=session_factory(session),
        connector_factory=connector_factory(connector),
        now=fixed_clock,
    )
    second = run_watch(
        database,
        settings,
        market="Euronext Paris",
        reports_dir=tmp_path / "reports",
        session_factory=session_factory(session),
        connector_factory=connector_factory(connector),
        now=fixed_clock,
    )

    assert first.stats.downloaded == 1
    assert first.stats.duplicates == 0
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 1
    assert session.downloads == [document.url]
    assert first.report_path != second.report_path
    second_report = second.report_path.read_text(encoding="utf-8")
    assert "## Sociétés vérifiées" in second_report
    assert "## Nouveaux documents" in second_report
    assert "## Doublons" in second_report
    assert "URL connue" in second_report
    with database.connect() as connection:
        documents = connection.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]
        runs = connection.execute(
            """
            SELECT downloaded, duplicates, status
            FROM watch_runs ORDER BY id
            """
        ).fetchall()
    assert documents == 1
    assert [tuple(row) for row in runs] == [
        (1, 0, "success"),
        (0, 1, "success"),
    ]


def test_same_content_under_new_url_is_sha256_duplicate(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    initialize_issuers(
        database,
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")],
    )
    connector = StaticConnector(
        {
            "FR0000120073": [
                candidate("https://official.test/report-v1.pdf")
            ]
        }
    )
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": "Euronext Paris",
        "reports_dir": tmp_path / "reports",
        "session_factory": session_factory(session),
        "connector_factory": connector_factory(connector),
        "now": fixed_clock,
    }

    first = run_watch(**common)
    connector.candidates_by_isin["FR0000120073"] = [
        candidate("https://official.test/report-v2.pdf")
    ]
    second = run_watch(**common)
    third = run_watch(**common)

    expected_sha = hashlib.sha256(PDF_BYTES).hexdigest()
    assert first.stats.downloaded == 1
    assert second.stats.duplicates == 1
    assert third.stats.duplicates == 1
    assert session.downloads == [
        "https://official.test/report-v1.pdf",
        "https://official.test/report-v2.pdf",
    ]
    report = second.report_path.read_text(encoding="utf-8")
    assert "SHA256 connu" in report
    assert expected_sha in report
    assert "URL connue" in third.report_path.read_text(encoding="utf-8")
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM document_urls"
        ).fetchone()[0] == 2


def test_degraded_issuer_does_not_stop_remaining_watch(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    initialize_issuers(
        database,
        [
            Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris"),
            Issuer("L'Oréal", "FR0000120321", "OR", "Euronext Paris"),
        ],
    )
    connector = RecoveringDegradedConnector(
        "FR0000120073",
        {
            "FR0000120321": [
                candidate("https://official.test/loreal-rfa.pdf")
            ]
        },
    )
    session = FakeSession()

    outcome = run_watch(
        database,
        settings,
        market="Euronext Paris",
        reports_dir=tmp_path / "reports",
        session_factory=session_factory(session),
        connector_factory=connector_factory(connector),
        now=fixed_clock,
    )

    assert outcome.status == "partial"
    assert outcome.stats.issuers_checked == 2
    assert outcome.stats.downloaded == 1
    assert outcome.stats.errors == 1
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "Air Liquide" in report
    assert "L'Oréal" in report
    assert "réseau: timeout source France" in report
    assert "## Sources en degraded" in report
    assert "france-test" in report


def test_dry_run_applies_since_and_document_limit(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    initialize_issuers(
        database,
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")],
    )
    connector = StaticConnector(
        {
            "FR0000120073": [
                candidate(
                    "https://official.test/old.pdf",
                    published=date(2024, 12, 31),
                ),
                candidate(
                    "https://official.test/new-1.pdf",
                    published=date(2026, 3, 20),
                ),
                candidate(
                    "https://official.test/new-2.pdf",
                    published=date(2026, 4, 20),
                ),
            ]
        }
    )
    session = FakeSession()

    outcome = run_watch(
        database,
        settings,
        market="Euronext Paris",
        since=date(2026, 1, 1),
        limit=1,
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=session_factory(session),
        connector_factory=connector_factory(connector),
        now=fixed_clock,
    )

    assert outcome.status == "success"
    assert outcome.stats.candidates_found == 2
    assert outcome.stats.downloaded == 0
    assert outcome.stats.duplicates == 0
    assert session.downloads == []
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "Mode: `dry-run`" in report
    assert "Depuis: `2026-01-01`" in report
    assert "Limite effective de documents traités: `1`" in report
    assert "new-2.pdf" in report
    assert "new-1.pdf" not in report
    assert "old.pdf" in report
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0] == 0


def test_multi_market_watch_consolidates_and_isolates_source_failures(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    initialize_issuers(
        database,
        [
            Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris"),
            Issuer("Aker", "NO0010234552", "AKER", "Oslo Børs"),
            Issuer("Landi Renzo", "IT0004210289", "LR", "Euronext Milan"),
            Issuer(
                "Aalberts",
                "NL0000852564",
                "AALB",
                "Euronext Amsterdam",
            ),
            Issuer(
                "AB INBEV",
                "BE0974293251",
                "ABI",
                "Euronext Brussels",
            ),
        ],
    )
    connectors = {
        "Euronext Paris": AlwaysDegradedConnector({}),
        "Oslo Børs": StaticConnector(
            {
                "NO0010234552": [
                    candidate("https://official.test/aker-rfa.pdf")
                ]
            }
        ),
        "Euronext Milan": StaticConnector(
            {
                "IT0004210289": [
                    candidate("https://official.test/landi-rfa.pdf")
                ]
            }
        ),
        "Euronext Amsterdam": StaticConnector(
            {
                "NL0000852564": [
                    candidate("https://official.test/aalberts-rfa.pdf")
                ]
            }
        ),
        "Euronext Brussels": StaticConnector(
            {
                "BE0974293251": [
                    candidate("https://official.test/abinbev-rfa.pdf")
                ]
            }
        ),
    }
    session = FakeSession()

    outcome = run_watch(
        database,
        settings,
        market=None,
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=session_factory(session),
        connector_factory=lambda market, **kwargs: connectors[market],
        now=fixed_clock,
    )

    assert outcome.status == "partial"
    assert outcome.stats.issuers_checked == 5
    assert outcome.stats.candidates_found == 4
    assert outcome.stats.errors == 1
    assert outcome.market_stats["Euronext Paris"].errors == 1
    assert outcome.market_stats["Oslo Børs"].candidates_found == 1
    assert outcome.market_stats["Euronext Milan"].candidates_found == 1
    assert outcome.market_stats["Euronext Amsterdam"].candidates_found == 1
    assert outcome.market_stats["Euronext Brussels"].candidates_found == 1
    assert session.downloads == []

    report = outcome.report_path.read_text(encoding="utf-8")
    assert (
            "Marché: `France + Oslo + Italie + Netherlands + Belgium + Portugal "
            "+ Ireland + Spain + Sweden + Denmark + Finland + Austria + Poland "
            "+ Czechia + Croatia + Slovenia + Estonia + Latvia + Lithuania + "
            "Slovakia + Romania + Bulgaria`"
            in report
        )
    assert "## Résumé par marché" in report
    assert "Euronext Paris" in report
    assert "Oslo Børs" in report
    assert "Euronext Milan" in report
    assert "Euronext Amsterdam" in report
    assert "Euronext Brussels" in report
    assert "aker-rfa.pdf" in report
    assert "landi-rfa.pdf" in report
    assert "aalberts-rfa.pdf" in report
    assert "abinbev-rfa.pdf" in report
    assert "source France indisponible" in report

    with database.connect() as connection:
        run = connection.execute(
            "SELECT market, status, issuers_checked, errors "
            "FROM watch_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(run) == (
                "France + Oslo + Italie + Netherlands + Belgium + Portugal "
                "+ Ireland + Spain + Sweden + Denmark + Finland + Austria + Poland "
                "+ Czechia + Croatia + Slovenia + Estonia + Latvia + Lithuania + "
                "Slovakia + Romania + Bulgaria",
            "partial",
            5,
            1,
        )


def test_multi_market_document_limit_is_global(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    initialize_issuers(
        database,
        [
            Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris"),
            Issuer("Aker", "NO0010234552", "AKER", "Oslo Børs"),
            Issuer("Landi Renzo", "IT0004210289", "LR", "Euronext Milan"),
        ],
    )
    connectors = {
        "Euronext Paris": StaticConnector(
            {
                "FR0000120073": [
                    candidate("https://official.test/france.pdf")
                ]
            }
        ),
        "Oslo Børs": StaticConnector(
            {
                "NO0010234552": [
                    candidate("https://official.test/oslo.pdf")
                ]
            }
        ),
        "Euronext Milan": StaticConnector(
            {
                "IT0004210289": [
                    candidate("https://official.test/italy.pdf")
                ]
            }
        ),
    }

    outcome = run_watch(
        database,
        settings,
        market=None,
        limit=2,
        dry_run=True,
        reports_dir=tmp_path / "reports",
        session_factory=session_factory(FakeSession()),
        connector_factory=lambda market, **kwargs: connectors[market],
        now=fixed_clock,
    )

    assert outcome.status == "success"
    assert outcome.stats.candidates_found == 3
    assert outcome.market_stats["Euronext Milan"].candidates_found == 1
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "france.pdf" in report
    assert "oslo.pdf" in report
    assert "italy.pdf" not in report


def test_watch_all_is_idempotent_and_generates_eml(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    initialize_issuers(
        database,
        [
            Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris"),
            Issuer("Aker", "NO0010234552", "AKER", "Oslo Børs"),
        ],
    )
    connectors = {
        "Euronext Paris": StaticConnector(
            {
                "FR0000120073": [
                    candidate("https://official.test/france-rfa.pdf")
                ]
            }
        ),
        "Oslo Børs": StaticConnector(
            {
                "NO0010234552": [
                    candidate("https://official.test/oslo-rfa.pdf")
                ]
            }
        ),
    }
    session = FakeSession()
    common = {
        "database": database,
        "settings": settings,
        "market": None,
        "reports_dir": tmp_path / "reports",
        "session_factory": session_factory(session),
        "connector_factory": lambda market, **kwargs: connectors[market],
        "now": fixed_clock,
    }

    first = run_watch(
        **common,
        notify_email="ops@example.com",
    )
    second = run_watch(**common)

    assert first.stats.downloaded == 1
    assert first.stats.duplicates == 1
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 2
    assert first.report_path.name == "watch_all_20260612_083000.md"
    assert first.notification_path is not None
    message = BytesParser(policy=policy.default).parsebytes(
        first.notification_path.read_bytes()
    )
    assert message["To"] == "ops@example.com"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Nouveaux documents" in body
    assert "Air Liquide" in body
    assert first.report_path.resolve().as_uri() in body


def test_watch_persists_skipped_too_large_without_error(
    tmp_path: Path,
) -> None:
    settings = replace(make_settings(tmp_path), max_download_bytes=10)
    database = Database(settings.db_path)
    initialize_issuers(
        database,
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")],
    )
    connector = StaticConnector(
        {
            "FR0000120073": [
                candidate("https://official.test/too-large.pdf")
            ]
        }
    )

    outcome = run_watch(
        database,
        settings,
        market="Euronext Paris",
        reports_dir=tmp_path / "reports",
        session_factory=session_factory(FakeSession()),
        connector_factory=connector_factory(connector),
        now=fixed_clock,
    )

    assert outcome.status == "success"
    assert outcome.stats.downloaded == 0
    assert outcome.stats.skipped_too_large == 1
    assert outcome.stats.errors == 0
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "Documents ignorés car trop gros" in report
    assert "skipped_too_large" in report
    with database.connect() as connection:
        event = connection.execute(
            """
            SELECT event_status, file_size
            FROM operational_events
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        run = connection.execute(
            """
            SELECT skipped_too_large, errors
            FROM watch_runs
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert tuple(event) == ("skipped_too_large", len(PDF_BYTES))
    assert tuple(run) == (1, 0)
