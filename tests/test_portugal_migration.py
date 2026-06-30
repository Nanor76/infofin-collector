from __future__ import annotations

import sqlite3
from pathlib import Path

from db import Database
from models import Issuer

PORTUGAL_COLUMNS = {
    "portugal_cmvm_sdi_url",
    "portugal_cmvm_detail_url",
    "portugal_cmvm_record_id",
    "portugal_home_member_state",
}


def test_database_migrates_portugal_columns(tmp_path: Path) -> None:
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
    assert PORTUGAL_COLUMNS.issubset(columns)


def test_portugal_defaults_and_resolution(tmp_path: Path) -> None:
    database = Database(tmp_path / "portugal.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Altri SGPS SA",
                "PTALT0AE0002",
                "ALTR",
                "Euronext Lisbon",
            )
        ]
    )
    imported = database.list_issuers("Euronext Lisbon")[0]
    assert imported.portugal_cmvm_sdi_url
    assert imported.portugal_home_member_state == "Portugal"

    stored = database.store_portugal_issuer_resolution(
        name="Altri SGPS SA",
        symbol="ALTR",
        sdi_url="https://www.cmvm.pt/PInstitucional/Content",
        detail_url="https://www.cmvm.pt/PInstitucional/PdfViewerInfPriv?Input=X",
        home_member_state="Portugal",
        cmvm_record_id="1383791",
    )
    assert stored is not None
    assert stored.portugal_cmvm_record_id == "1383791"
