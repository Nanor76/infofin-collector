import json
import sqlite3
from pathlib import Path
from typing import Any

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from main import build_parser, check_documents, diagnose_source, main
from models import Issuer


class FakeResponse:
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {
            "total_count": 1,
            "results": [{"ISIN": "FR0000120073", "Société": "Air Liquide"}],
        }

    def close(self) -> None:
        return None


class FakeSession:
    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        timeout: int,
        stream: bool = False,
    ) -> FakeResponse:
        return FakeResponse()

    def close(self) -> None:
        return None


class DummyDownloader:
    def __init__(self, **kwargs: Any) -> None:
        pass


class DegradedFranceConnector(Connector):
    market = "Euronext Paris"
    source_name = "france-test"

    def __init__(self) -> None:
        self.state = ConnectorState.READY
        self.last_error = None

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.mark_degraded("HTTP test")
        return []


class WorkingOsloConnector(Connector):
    market = "Oslo Børs"
    source_name = "oslo-test"

    def __init__(self) -> None:
        self.state = ConnectorState.READY
        self.last_error = None
        self.calls = 0

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        self.calls += 1
        return []


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "infofin.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=10,
        http_retries=0,
        http_backoff_factor=0.0,
        user_agent="test",
        max_download_bytes=1024,
        amf_base_url=(
            "https://data.economie.gouv.fr/"
            "api/explore/v2.1/catalog/datasets"
        ),
        amf_fallback_base_urls=(),
        amf_dataset="flux-amf-new-prod",
        amf_rows=100,
    )


def test_diagnose_source_prints_json(monkeypatch: Any, capsys: Any) -> None:
    settings = make_settings(Path("data/test"))
    monkeypatch.setattr("main.build_http_session", lambda **kwargs: FakeSession())

    exit_code = diagnose_source(
        settings,
        "france",
        dataset="flux-amf-override",
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["state"] == "ready"
    assert output["dataset"] == "flux-amf-override"
    assert output["total_count"] == 1
    assert output["fields"] == ["ISIN", "Société"]


def test_degraded_france_does_not_block_oslo(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris"),
            Issuer("Aker", "NO0010234552", "AKER", "Oslo Børs"),
        ]
    )
    france = DegradedFranceConnector()
    oslo = WorkingOsloConnector()

    def connector_factory(market: str, **kwargs: Any) -> Connector:
        return france if market == "Euronext Paris" else oslo

    monkeypatch.setattr("main.connector_for_market", connector_factory)
    monkeypatch.setattr("main.build_http_session", lambda **kwargs: FakeSession())
    monkeypatch.setattr("main.DocumentDownloader", DummyDownloader)

    exit_code = check_documents(database, settings, market=None)

    assert exit_code == 1
    assert france.state == ConnectorState.DEGRADED
    assert oslo.calls == 1
    with database.connect() as connection:
        run = connection.execute(
            "SELECT status, errors FROM download_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert run["status"] == "partial"
    assert run["errors"] == 1


def test_main_handles_sqlite_error_without_traceback(
    monkeypatch: Any,
    caplog: Any,
) -> None:
    monkeypatch.setattr(
        "main.Database.initialize",
        lambda self: (_ for _ in ()).throw(sqlite3.OperationalError("db refused")),
    )

    exit_code = main(["import-csv", "missing.csv"])

    assert exit_code == 2
    assert "db refused" in caplog.text


def test_watch_parser_accepts_daily_options() -> None:
    args = build_parser().parse_args(
        [
            "watch",
            "--market",
            "Euronext Paris",
            "--since",
            "2026-01-01",
            "--limit",
            "12",
            "--dry-run",
        ]
    )

    assert args.command == "watch"
    assert args.market == "Euronext Paris"
    assert args.since.isoformat() == "2026-01-01"
    assert args.limit == 12
    assert args.dry_run is True


def test_watch_parser_accepts_all_supported_markets() -> None:
    args = build_parser().parse_args(
        [
            "watch",
            "--all",
            "--since",
            "2026-01-01",
            "--limit",
            "12",
            "--dry-run",
        ]
    )

    assert args.command == "watch"
    assert args.all is True
    assert args.market is None
    assert args.since.isoformat() == "2026-01-01"
    assert args.limit == 12
    assert args.dry_run is True


def test_watch_parser_accepts_global_operational_options_after_command() -> None:
    args = build_parser().parse_args(
        [
            "watch",
            "--all",
            "--max-download-mb",
            "50",
            "--notify-email",
            "ops@example.com",
        ]
    )

    assert args.max_download_mb == 50
    assert args.notify_email == "ops@example.com"


def test_parser_accepts_daily_operations_commands() -> None:
    status = build_parser().parse_args(["status"])
    healthcheck = build_parser().parse_args(["healthcheck"])
    export = build_parser().parse_args(
        ["export-latest", "--format", "json"]
    )

    assert status.command == "status"
    assert healthcheck.command == "healthcheck"
    assert export.format == "json"


def test_parser_accepts_oslo_discovery_commands() -> None:
    diagnose = build_parser().parse_args(["diagnose-source", "oslo"])
    discover = build_parser().parse_args(
        ["discover-source", "oslo", "--query", "annual financial"]
    )
    issuer = build_parser().parse_args(
        [
            "discover-issuer",
            "oslo",
            "--symbol",
            "2020",
            "--name",
            "2020 BULKERS",
        ]
    )

    assert diagnose.source == "oslo"
    assert discover.query == "annual financial"
    assert issuer.symbol == "2020"
    assert issuer.name == "2020 BULKERS"


def test_parser_accepts_italy_discovery_commands() -> None:
    diagnose = build_parser().parse_args(["diagnose-source", "italy"])
    discover = build_parser().parse_args(
        [
            "discover-source",
            "italy",
            "--query",
            "relazione finanziaria annuale",
        ]
    )
    issuer = build_parser().parse_args(
        [
            "discover-issuer",
            "italy",
            "--symbol",
            "LR",
            "--name",
            "LANDI RENZO",
        ]
    )

    assert diagnose.source == "italy"
    assert discover.query == "relazione finanziaria annuale"
    assert issuer.symbol == "LR"


def test_parser_accepts_netherlands_discovery_commands() -> None:
    diagnose = build_parser().parse_args(
        ["diagnose-source", "netherlands"]
    )
    discover = build_parser().parse_args(
        [
            "discover-source",
            "netherlands",
            "--query",
            "annual financial",
        ]
    )
    issuer = build_parser().parse_args(
        [
            "discover-issuer",
            "netherlands",
            "--symbol",
            "AALB",
            "--name",
            "AALBERTS NV",
        ]
    )

    assert diagnose.source == "netherlands"
    assert discover.query == "annual financial"
    assert issuer.symbol == "AALB"


def test_parser_accepts_belgium_discovery_commands() -> None:
    diagnose = build_parser().parse_args(["diagnose-source", "belgium"])
    discover = build_parser().parse_args(
        [
            "discover-source",
            "belgium",
            "--query",
            "annual financial report",
        ]
    )
    issuer = build_parser().parse_args(
        [
            "discover-issuer",
            "belgium",
            "--symbol",
            "ABI",
            "--name",
            "AB INBEV",
        ]
    )

    assert diagnose.source == "belgium"
    assert discover.query == "annual financial report"
    assert issuer.symbol == "ABI"


def test_parser_accepts_ireland_discovery_commands() -> None:
    diagnose = build_parser().parse_args(["diagnose-source", "ireland"])
    discover = build_parser().parse_args(
        [
            "discover-source",
            "ireland",
            "--query",
            "annual report",
        ]
    )
    issuer = build_parser().parse_args(
        [
            "discover-issuer",
            "ireland",
            "--symbol",
            "BIRG",
            "--name",
            "BANK OF IRELAND GP",
        ]
    )

    assert diagnose.source == "ireland"
    assert discover.query == "annual report"
    assert issuer.symbol == "BIRG"


def test_parser_accepts_austria_discovery_commands() -> None:
    diagnose = build_parser().parse_args(["diagnose-source", "austria"])
    discover = build_parser().parse_args(
        ["discover-source", "austria", "--query", "annual"]
    )
    issuer = build_parser().parse_args(
        [
            "discover-issuer",
            "austria",
            "--symbol",
            "ATS",
            "--name",
            "AT & S Austria Technologie & Systemtechnik Aktiengesellschaft",
            "--isin",
            "AT0000969985",
        ]
    )

    assert diagnose.source == "austria"
    assert discover.query == "annual"
    assert issuer.isin == "AT0000969985"


def test_parser_accepts_poland_discovery_commands() -> None:
    diagnose = build_parser().parse_args(["diagnose-source", "poland"])
    discover = build_parser().parse_args(
        ["discover-source", "poland", "--query", "quarterly"]
    )
    issuer = build_parser().parse_args(
        [
            "discover-issuer",
            "poland",
            "--symbol",
            "MODIVO",
            "--name",
            "MODIVO Spółka Akcyjna",
            "--isin",
            "PLCCC0000016",
        ]
    )

    assert diagnose.source == "poland"
    assert discover.query == "quarterly"
    assert issuer.isin == "PLCCC0000016"


def test_import_euronext_downloads_and_upserts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()

    class FakeCsvResponse:
        status_code = 200
        content = (
            b"Name;ISIN;Symbol;Market\n"
            b'"European Equities"\n'
            b'"15 May 2025"\n'
            b"Air Liquide;FR0000120073;AI;Paris\n"
            b"Aker;NO0010234552;AKER;XOSL\n"
        )

        def raise_for_status(self) -> None:
            pass

    class FakeImportSession:
        def get(self, url: str, **kwargs: Any) -> FakeCsvResponse:
            return FakeCsvResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "main.build_http_session",
        lambda **kwargs: FakeImportSession(),
    )

    args = build_parser().parse_args(
        ["import-euronext", "--url", "http://test.url"]
    )
    assert args.command == "import-euronext"
    assert args.url == "http://test.url"

    from main import import_euronext

    exit_code = import_euronext(database, settings, url="http://test.url")
    assert exit_code == 0

    issuers = database.list_issuers()
    assert len(issuers) == 2
    assert issuers[0].isin == "FR0000120073"
    assert issuers[1].isin == "NO0010234552"
