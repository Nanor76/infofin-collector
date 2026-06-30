from __future__ import annotations

import sqlite3
from pathlib import Path

from db import Database
from models import Issuer

BELGIUM_COLUMNS = {
    "belgium_fsma_stori_url",
    "belgium_fsma_detail_url",
    "belgium_home_member_state",
    "belgium_fsma_record_id",
}


def test_database_migrates_belgium_columns(tmp_path: Path) -> None:
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
    assert BELGIUM_COLUMNS.issubset(columns)


def test_belgium_defaults_and_resolution_are_persisted(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "belgium.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "AB INBEV",
                "BE0974293251",
                "ABI",
                "Euronext Brussels",
            )
        ]
    )

    imported = database.list_issuers("Euronext Brussels")[0]
    assert imported.belgium_fsma_stori_url == (
        "https://www.fsma.be/en/stori"
    )
    assert imported.belgium_home_member_state == "Belgium"

    stored = database.store_belgium_issuer_resolution(
        name="AB INBEV",
        symbol="ABI",
        stori_url="https://www.fsma.be/en/stori",
        detail_url="https://www.fsma.be/en/stori",
        home_member_state="Belgium",
        fsma_record_id="record-ab-inbev-2025",
    )

    assert stored is not None
    assert stored.belgium_fsma_record_id == "record-ab-inbev-2025"
