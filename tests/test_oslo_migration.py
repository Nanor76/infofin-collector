from pathlib import Path
import sqlite3
from db import Database

def test_database_migration_adds_oslo_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "test_migration.sqlite3"
    
    # 1. Create a database with the OLD schema (no Oslo columns)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE issuers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                isin TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
    
    # 2. Initialize it via the Database class
    database = Database(db_path)
    database.initialize()
    
    # 3. Verify columns exist
    with database.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(issuers)").fetchall()}
    
    assert "oslo_issuer_id" in columns
    assert "newsweb_url" in columns
    assert "euronext_company_url" in columns

def test_oslo_columns_persistence(tmp_path: Path) -> None:
    database = Database(tmp_path / "test_persistence.sqlite3")
    database.initialize()
    
    # Test through store_oslo_issuer_resolution (which is already tested, but let's be thorough)
    issuer = database.store_oslo_issuer_resolution(
        name="Test Oslo",
        symbol="TOSL",
        isin="NO0010000001",
        oslo_issuer_id="12345",
        newsweb_url="https://newsweb.example.com/12345",
        euronext_company_url="https://live.euronext.com/test"
    )
    
    # Re-list to check persistence
    issuers = database.list_issuers("Oslo Børs")
    assert len(issuers) == 1
    assert issuers[0].oslo_issuer_id == "12345"
    assert issuers[0].newsweb_url == "https://newsweb.example.com/12345"
    assert issuers[0].euronext_company_url == "https://live.euronext.com/test"
    
    # Test update via store_oslo_issuer_resolution
    updated_issuer = database.store_oslo_issuer_resolution(
        name="Test Oslo Updated",
        symbol="TOSL",
        isin="NO0010000001",
        oslo_issuer_id="12345-updated",
        newsweb_url="https://newsweb.example.com/12345-updated",
        euronext_company_url="https://live.euronext.com/test-updated"
    )
    
    assert updated_issuer.id == issuer.id
    assert updated_issuer.name == "Test Oslo Updated"
    assert updated_issuer.oslo_issuer_id == "12345-updated"
