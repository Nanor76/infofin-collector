from __future__ import annotations

import sqlite3
from pathlib import Path

from db import Database
from models import Issuer

NETHERLANDS_COLUMNS = {
    "netherlands_afm_issuer_url",
    "netherlands_afm_detail_url",
    "netherlands_home_member_state",
    "netherlands_afm_record_id",
}


def test_database_migrates_netherlands_columns(tmp_path: Path) -> None:
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
    assert NETHERLANDS_COLUMNS.issubset(columns)


def test_netherlands_resolution_is_persisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "netherlands.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "AALBERTS NV",
                "NL0000852564",
                "AALB",
                "Euronext Amsterdam",
            )
        ]
    )

    stored = database.store_netherlands_issuer_resolution(
        name="AALBERTS NV",
        symbol="AALB",
        issuer_url="https://www.afm.nl/register?KeyWords=Aalberts",
        detail_url="https://www.afm.nl/register/details?id=A2510-03545",
        home_member_state="Netherlands",
        afm_record_id="A2510-03545",
    )

    assert stored is not None
    assert stored.netherlands_afm_record_id == "A2510-03545"
    assert stored.netherlands_home_member_state == "Netherlands"
    assert stored.netherlands_afm_detail_url.endswith("A2510-03545")
