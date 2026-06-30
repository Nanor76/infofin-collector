from datetime import date
from pathlib import Path

from connectors.base import DocumentCandidate
from db import Database
from models import Issuer


def test_database_upsert_and_sha_deduplication(tmp_path: Path) -> None:
    database = Database(tmp_path / "infofin.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")]
    )
    issuer = database.list_issuers()[0]
    candidate = DocumentCandidate(
        title="Rapport financier annuel",
        url="https://example.test/report.pdf",
        published_date=date(2025, 12, 31),
        document_type="annual_financial_report",
        source="test",
    )

    inserted = database.add_document(
        issuer_id=issuer.id,
        candidate=candidate,
        local_path="data/report.pdf",
        sha256="a" * 64,
        content_type="application/pdf",
        file_size=10,
    )
    duplicate = database.add_document(
        issuer_id=issuer.id,
        candidate=candidate,
        local_path="data/report-copy.pdf",
        sha256="a" * 64,
        content_type="application/pdf",
        file_size=10,
    )

    assert inserted is True
    assert duplicate is False
    assert database.get_document_by_sha256("a" * 64) is not None
    assert (
        database.get_document_by_source_url(candidate.url)["sha256"]
        == "a" * 64
    )


def test_watch_run_lifecycle_is_persisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "infofin.sqlite3")
    database.initialize()

    run_id = database.create_watch_run(
        "Euronext Paris",
        started_at="2026-06-12T08:00:00+00:00",
    )
    database.finish_watch_run(
        run_id,
        status="partial",
        issuers_checked=3,
        candidates_found=4,
        downloaded=1,
        duplicates=2,
        errors=1,
        ended_at="2026-06-12T08:05:00+00:00",
    )

    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM watch_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert row["started_at"] == "2026-06-12T08:00:00+00:00"
    assert row["ended_at"] == "2026-06-12T08:05:00+00:00"
    assert row["market"] == "Euronext Paris"
    assert row["issuers_checked"] == 3
    assert row["candidates_found"] == 4
    assert row["downloaded"] == 1
    assert row["duplicates"] == 2
    assert row["errors"] == 1
    assert row["status"] == "partial"


def test_oslo_issuer_resolution_is_persisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "infofin.sqlite3")
    database.initialize()

    issuer = database.store_oslo_issuer_resolution(
        name="2020 BULKERS",
        symbol="2020",
        isin="BMG9156K1018",
        oslo_issuer_id="245243",
        newsweb_url="https://newsweb.oslobors.no/message/667877",
        euronext_company_url=(
            "https://live.euronext.com/en/product/equities/"
            "BMG9156K1018-XOSL"
        ),
    )
    stored = database.list_issuers("Oslo Børs")[0]

    assert issuer.id == stored.id
    assert stored.oslo_issuer_id == "245243"
    assert stored.newsweb_url.endswith("/message/667877")
    assert stored.euronext_company_url.endswith("/BMG9156K1018-XOSL")
