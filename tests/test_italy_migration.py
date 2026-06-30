from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from connectors.base import DocumentCandidate
from db import Database
from models import Issuer


ITALY_COLUMNS = {
    "italy_storage_provider",
    "italy_emarket_url",
    "italy_1info_url",
    "borsa_italiana_company_url",
}


def test_database_migrates_legacy_issuers_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE issuers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                isin TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    database = Database(db_path)
    database.initialize()

    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(issuers)"
            ).fetchall()
        }
    assert ITALY_COLUMNS.issubset(columns)


def test_italian_issuer_defaults_and_resolution_are_persisted(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "italy.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                name="LANDI RENZO",
                isin="IT0005619843",
                symbol="LR",
                market="Euronext Milan",
            )
        ]
    )

    imported = database.list_issuers("Euronext Milan")[0]
    assert imported.italy_storage_provider == "emarketstorage"
    assert imported.italy_emarket_url.endswith("/it/documenti")
    assert imported.italy_1info_url.endswith("/PORTALE1INFO")
    assert imported.borsa_italiana_company_url.endswith(
        "/IT0005619843.html?lang=it"
    )

    stored = database.store_italy_issuer_resolution(
        name="LANDI RENZO",
        symbol="LR",
        storage_provider="emarketstorage",
        emarket_url=(
            "https://www.emarketstorage.it/it/documenti?azienda=267"
        ),
        oneinfo_url="https://www.1info.it/PORTALE1INFO",
        borsa_italiana_company_url=(
            "https://www.borsaitaliana.it/borsa/azioni/"
            "scheda/IT0005619843.html?lang=it"
        ),
    )

    assert stored is not None
    assert stored.italy_emarket_url.endswith("?azienda=267")


def test_italy_document_idempotence(tmp_path: Path) -> None:
    database = Database(tmp_path / "italy-idempotence.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                name="LANDI RENZO",
                isin="IT0005619843",
                symbol="LR",
                market="Euronext Milan",
            )
        ]
    )
    issuer = database.list_issuers("Euronext Milan")[0]
    candidate = DocumentCandidate(
        title="Relazione finanziaria annuale 2025",
        url=(
            "https://www.emarketstorage.it/sites/default/files/"
            "comunicati/2026-06/20260612_185771.pdf"
        ),
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="emarketstorage",
        source_document_id="185771",
    )
    sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    assert database.add_document(
        issuer_id=issuer.id,
        candidate=candidate,
        local_path="data/raw/italy/IT0005619843/report.pdf",
        sha256=sha256,
        content_type="application/pdf",
        file_size=12345,
    )
    assert not database.add_document(
        issuer_id=issuer.id,
        candidate=candidate,
        local_path="data/raw/italy/IT0005619843/report.pdf",
        sha256=sha256,
        content_type="application/pdf",
        file_size=12345,
    )
    row = database.get_document_by_source_url(candidate.url)
    assert row is not None
    assert row["source"] == "emarketstorage"
