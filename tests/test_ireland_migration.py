from __future__ import annotations

import sqlite3
from pathlib import Path

from db import Database
from models import Issuer

IRELAND_COLUMNS = {
    "ireland_euronext_oam_url",
    "ireland_euronext_direct_url",
    "ireland_detail_url",
    "ireland_record_id",
    "ireland_home_member_state",
}


def test_database_migrates_ireland_columns(tmp_path: Path) -> None:
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
            for row in connection.execute("PRAGMA table_info(issuers)")
        }
    assert IRELAND_COLUMNS.issubset(columns)


def test_ireland_defaults_and_resolution(tmp_path: Path) -> None:
    database = Database(tmp_path / "ireland.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "BANK OF IRELAND GP",
                "IE00BD1RP616",
                "BIRG",
                "Euronext Dublin",
            )
        ]
    )
    imported = database.list_issuers("Euronext Dublin")[0]
    assert imported.ireland_euronext_oam_url
    assert imported.ireland_euronext_direct_url
    assert imported.ireland_home_member_state == "Ireland"

    stored = database.store_ireland_issuer_resolution(
        name="BANK OF IRELAND GP",
        symbol="BIRG",
        direct_url="https://direct.euronext.com",
        oam_url="https://direct.euronext.com/#/oamfiling",
        detail_url="https://direct.euronext.com/#/oamfiling",
        home_member_state="Ireland",
        record_id="fixture-pdf-id",
    )
    assert stored is not None
    assert stored.ireland_record_id == "fixture-pdf-id"
