from __future__ import annotations

import sqlite3
from pathlib import Path

from db import Database
from models import Issuer

SWEDEN_COLUMNS = {
    "sweden_fi_issuer_url",
    "sweden_fi_record_id",
    "sweden_fi_detail_url",
    "sweden_home_member_state",
    "sweden_nasdaq_company_url",
    "sweden_pea_country_check",
}

def test_database_migrates_sweden_columns(tmp_path: Path) -> None:
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
    assert SWEDEN_COLUMNS.issubset(columns)

def test_sweden_defaults_and_resolution(tmp_path: Path) -> None:
    database = Database(tmp_path / "sweden.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Ericsson, Telefonaktiebolaget LM",
                "SE0000108656",
                "ERIC B",
                "Nasdaq Stockholm",
            )
        ]
    )
    imported = database.list_issuers("Nasdaq Stockholm")[0]
    # Default pea_country_check should be eu_candidate, home_member_state should be None until resolved
    assert imported.sweden_pea_country_check == "eu_candidate"
    assert imported.sweden_home_member_state is None

    stored = database.store_sweden_issuer_resolution(
        name="Ericsson, Telefonaktiebolaget LM",
        symbol="ERIC B",
        sweden_fi_issuer_url="https://example.com/ericsson",
        sweden_fi_record_id="record-456",
        sweden_fi_detail_url="https://example.com/ericsson-detail",
        sweden_home_member_state="Sweden",
        sweden_nasdaq_company_url="https://example.com/nasdaq-ericsson",
        sweden_pea_country_check="eu_candidate",
    )
    assert stored is not None
    assert stored.sweden_fi_record_id == "record-456"
    assert stored.sweden_fi_detail_url == "https://example.com/ericsson-detail"
    assert stored.sweden_home_member_state == "Sweden"
    assert stored.sweden_nasdaq_company_url == "https://example.com/nasdaq-ericsson"
