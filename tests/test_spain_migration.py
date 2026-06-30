from __future__ import annotations

import sqlite3
from pathlib import Path

from db import Database
from models import Issuer

SPAIN_COLUMNS = {
    "spain_cnmv_entity_url",
    "spain_cnmv_nif",
    "spain_cnmv_record_id",
    "spain_bme_company_url",
    "spain_home_member_state",
    "spain_pea_country_check",
}

def test_database_migrates_spain_columns(tmp_path: Path) -> None:
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
    assert SPAIN_COLUMNS.issubset(columns)

def test_spain_defaults_and_resolution(tmp_path: Path) -> None:
    database = Database(tmp_path / "spain.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Banco Santander, S.A.",
                "ES0113900J37",
                "SAN",
                "Bolsa de Madrid",
            )
        ]
    )
    imported = database.list_issuers("Bolsa de Madrid")[0]
    # Default pea_country_check should be eu_candidate, home_member_state should be None until resolved/confirmed
    assert imported.spain_pea_country_check == "eu_candidate"
    assert imported.spain_home_member_state is None

    stored = database.store_spain_issuer_resolution(
        name="Banco Santander, S.A.",
        symbol="SAN",
        cnmv_entity_url="https://example.com/santander",
        cnmv_nif="A39000013",
        cnmv_record_id="record-123",
        bme_company_url="https://bme.com/santander",
        home_member_state="Spain",
        pea_country_check="eu_candidate",
    )
    assert stored is not None
    assert stored.spain_cnmv_record_id == "record-123"
    assert stored.spain_cnmv_nif == "A39000013"
    assert stored.spain_home_member_state == "Spain"
