from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from config import Settings
from connectors.base import DocumentCandidate
from db import Database
from main import main, run_healthcheck
from models import Issuer
from operations import export_latest_documents, render_status


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "infofin.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=10,
        http_retries=0,
        http_backoff_factor=0.0,
        user_agent="test",
        max_download_bytes=100 * 1024 * 1024,
        amf_base_url="https://official.test",
        amf_fallback_base_urls=(),
        amf_dataset="test",
        amf_rows=10,
    )


def seed_document(database: Database) -> None:
    database.upsert_issuers(
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")]
    )
    issuer = database.list_issuers()[0]
    database.add_document(
        issuer_id=issuer.id,
        candidate=DocumentCandidate(
            title="Rapport financier annuel 2025",
            url="https://official.test/air-liquide.pdf",
            published_date=date(2025, 12, 31),
            document_type="annual_financial_report",
            source="france-test",
        ),
        local_path="data/raw/france/report.pdf",
        sha256="a" * 64,
        content_type="application/pdf",
        file_size=123,
    )


def seed_market_document(
    database: Database,
    *,
    company: str,
    isin: str,
    market: str,
    source: str,
    document_type: str,
    local_path: str,
    sha256: str,
    downloaded_at: str,
) -> None:
    database.upsert_issuers([Issuer(company, isin, isin[-4:], market)])
    issuer = next(item for item in database.list_issuers() if item.isin == isin)
    url = f"https://official.test/{source}/{isin}.pdf"
    database.add_document(
        issuer_id=issuer.id,
        candidate=DocumentCandidate(
            title=f"{company} report",
            url=url,
            published_date=date.fromisoformat(downloaded_at[:10]),
            document_type=document_type,
            source=source,
        ),
        local_path=local_path,
        sha256=sha256,
        content_type="application/pdf",
        file_size=123,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE documents SET downloaded_at = ? WHERE source_url = ?",
            (downloaded_at, url),
        )


def test_status_contains_daily_operational_sections(tmp_path: Path) -> None:
    database = Database(tmp_path / "infofin.sqlite3")
    database.initialize()
    seed_document(database)
    issuer = database.list_issuers()[0]
    run_id = database.create_watch_run(
        "Euronext Paris",
        started_at="2026-06-13T08:00:00+00:00",
    )
    database.record_watch_market_stats(
        run_id,
        {
            "Euronext Paris": SimpleNamespace(
                issuers_checked=1,
                candidates_found=1,
                downloaded=1,
                duplicates=0,
                skipped_too_large=0,
                errors=1,
            )
        },
    )
    database.finish_watch_run(
        run_id,
        status="partial",
        issuers_checked=1,
        candidates_found=1,
        downloaded=1,
        duplicates=0,
        errors=1,
        report_path="reports/watch.md",
    )
    database.add_operational_event(
        watch_run_id=run_id,
        issuer=issuer,
        source="france-test",
        event_status="error",
        message="timeout",
    )
    database.set_source_state(
        source="france-test",
        market="Euronext Paris",
        state="degraded",
        error="timeout",
        context="watch",
    )
    raw_file = tmp_path / "raw" / "sample.pdf"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(b"1234")

    output = render_status(database, data_dir=tmp_path / "raw")

    assert "Émetteurs par marché" in output
    assert "Documents par marché / source / type" in output
    assert "Dernier watch_run par marché" in output
    assert "Derniers téléchargements" in output
    assert "Dernières erreurs" in output
    assert "Sources degraded / unavailable" in output
    assert "Euronext Paris" in output
    assert "timeout" in output
    assert "4 B" in output


def test_status_command_prints_operational_view(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    seed_document(database)
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )

    exit_code = main(["status"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "# InfoFin status" in output
    assert "Air Liquide" in output


def test_healthcheck_writes_consolidated_report_and_fails_on_unavailable(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()

    def diagnostic(source: str) -> dict[str, object]:
        state = "unavailable" if source == "italy" else "ready"
        return {
            "source": source,
            "state": state,
            "http_status": 503 if source == "italy" else 200,
            "detected_count": 0 if source == "italy" else 1,
            "error": "maintenance" if source == "italy" else None,
        }

    outcome = run_healthcheck(
        database,
        settings,
        reports_dir=tmp_path / "reports",
        now=lambda: datetime(2026, 6, 13, 8, 30, tzinfo=UTC),
        diagnostic_provider=diagnostic,
    )

    assert outcome.exit_code == 1
    assert outcome.report_path.name == "healthcheck_20260613_083000.md"
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "Résumé consolidé" in report
    assert "Euronext Milan" in report
    assert "unavailable" in report
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM source_health_checks"
        ).fetchone()[0] == 23


def test_export_latest_documents_csv_and_json(tmp_path: Path) -> None:
    database = Database(tmp_path / "infofin.sqlite3")
    database.initialize()
    seed_document(database)
    fixed = datetime(2026, 6, 13, 10, 0, tzinfo=UTC)

    csv_path = export_latest_documents(
        database,
        export_format="csv",
        exports_dir=tmp_path / "exports",
        now=fixed,
    )
    json_path = export_latest_documents(
        database,
        export_format="json",
        exports_dir=tmp_path / "exports",
        now=fixed,
    )

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    json_rows = json.loads(json_path.read_text(encoding="utf-8"))
    assert csv_path.name == "latest_documents_20260613.csv"
    assert json_path.name == "latest_documents_20260613.json"
    assert csv_rows[0]["isin"] == "FR0000120073"
    assert json_rows[0]["company"] == "Air Liquide"


def test_export_latest_documents_keeps_default_latest_download_date(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "infofin.sqlite3")
    database.initialize()
    seed_market_document(
        database,
        company="OMV Petrom",
        isin="ROSNPPACNOR9",
        market="Bucharest Stock Exchange",
        source="romania_asf_oam",
        document_type="quarterly_financial_report",
        local_path="data/raw/romania/ROSNPPACNOR9/report.pdf",
        sha256="b" * 64,
        downloaded_at="2026-06-18T13:37:47+00:00",
    )
    seed_market_document(
        database,
        company="Trident Estates plc",
        isin="MT0001670109",
        market="Malta Stock Exchange",
        source="malta_mse_oam",
        document_type="annual_financial_report",
        local_path="data/raw/malta/MT0001670109/report.pdf",
        sha256="c" * 64,
        downloaded_at="2026-06-19T16:03:03+00:00",
    )

    csv_path = export_latest_documents(
        database,
        export_format="csv",
        exports_dir=tmp_path / "exports",
        now=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert [row["source"] for row in rows] == ["malta_mse_oam"]


def test_export_latest_documents_since_exports_multi_market_csv_and_json(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "infofin.sqlite3")
    database.initialize()
    seed_market_document(
        database,
        company="OMV Petrom",
        isin="ROSNPPACNOR9",
        market="Bucharest Stock Exchange",
        source="romania_asf_oam",
        document_type="quarterly_financial_report",
        local_path="data/raw/romania/ROSNPPACNOR9/report.pdf",
        sha256="d" * 64,
        downloaded_at="2026-06-18T13:37:47+00:00",
    )
    seed_market_document(
        database,
        company="DKC Dobrich",
        isin="BG11DKJ20001",
        market="Bulgarian Stock Exchange",
        source="bulgaria_bse_x3news",
        document_type="half_year_financial_report",
        local_path="data/raw/bulgaria/BG11DKJ20001/report.pdf",
        sha256="e" * 64,
        downloaded_at="2026-06-19T14:06:00+00:00",
    )
    seed_market_document(
        database,
        company="Trident Estates plc",
        isin="MT0001670109",
        market="Malta Stock Exchange",
        source="malta_mse_oam",
        document_type="annual_financial_report",
        local_path="data/raw/malta/MT0001670109/report.pdf",
        sha256="f" * 64,
        downloaded_at="2026-06-19T16:03:03+00:00",
    )

    fixed = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    csv_path = export_latest_documents(
        database,
        export_format="csv",
        exports_dir=tmp_path / "exports",
        now=fixed,
        since=date(2026, 6, 18),
    )
    json_path = export_latest_documents(
        database,
        export_format="json",
        exports_dir=tmp_path / "exports",
        now=fixed,
        since=date(2026, 6, 18),
    )

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    json_rows = json.loads(json_path.read_text(encoding="utf-8"))

    expected_sources = {
        "romania_asf_oam",
        "bulgaria_bse_x3news",
        "malta_mse_oam",
    }
    assert {row["source"] for row in csv_rows} == expected_sources
    assert {row["source"] for row in json_rows} == expected_sources
    assert {
        row["market"] for row in csv_rows
    } == {
        "Bucharest Stock Exchange",
        "Bulgarian Stock Exchange",
        "Malta Stock Exchange",
    }
    for row in csv_rows + json_rows:
        assert row["source"]
        assert row["document_type"]
        assert row["sha256"]
        assert row["local_path"]
