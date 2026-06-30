from __future__ import annotations

import sqlite3
from pathlib import Path

from db import Database
from models import Issuer


DENMARK_COLUMNS = {
    "denmark_dfsa_issuer_url",
    "denmark_dfsa_record_id",
    "denmark_dfsa_detail_url",
    "denmark_home_member_state",
    "denmark_nasdaq_company_url",
    "denmark_pea_country_check",
}


def test_database_migrates_denmark_columns(tmp_path: Path) -> None:
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
    assert DENMARK_COLUMNS.issubset(columns)


def test_denmark_pea_defaults_and_confirmed_resolution(tmp_path: Path) -> None:
    database = Database(tmp_path / "denmark.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "MATAS A/S",
                "DK0060497295",
                "MATAS",
                "Nasdaq Copenhagen",
            )
        ]
    )
    imported = database.list_issuers("Nasdaq Copenhagen")[0]
    assert imported.denmark_pea_country_check == "eu_candidate"
    assert imported.denmark_home_member_state is None

    stored = database.store_denmark_issuer_resolution(
        name="MATAS A/S",
        symbol="MATAS",
        denmark_dfsa_issuer_url="https://www.dfsa.dk/company-announcements",
        denmark_dfsa_record_id="300009086",
        denmark_dfsa_detail_url="https://app.test/details/300009086",
        denmark_home_member_state="Denmark",
        denmark_nasdaq_company_url="https://nasdaq.test/matas",
        denmark_pea_country_check="eu_candidate",
    )
    assert stored is not None
    assert stored.denmark_dfsa_record_id == "300009086"
    assert stored.denmark_home_member_state == "Denmark"
    assert stored.denmark_pea_country_check == "eu_candidate"
