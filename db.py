from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterable, Mapping

from connectors.base import DocumentCandidate
from models import Issuer


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_value(row: sqlite3.Row, name: str) -> str | None:
    return row[name] if name in row.keys() else None


def _issuer_from_row(row: sqlite3.Row) -> Issuer:
    return Issuer(
        id=row["id"],
        name=row["name"],
        isin=row["isin"],
        symbol=row["symbol"],
        market=row["market"],
        oslo_issuer_id=row["oslo_issuer_id"],
        newsweb_url=row["newsweb_url"],
        euronext_company_url=row["euronext_company_url"],
        italy_storage_provider=row["italy_storage_provider"],
        italy_emarket_url=row["italy_emarket_url"],
        italy_1info_url=row["italy_1info_url"],
        borsa_italiana_company_url=row["borsa_italiana_company_url"],
        netherlands_afm_issuer_url=row["netherlands_afm_issuer_url"],
        netherlands_afm_detail_url=row["netherlands_afm_detail_url"],
        netherlands_home_member_state=row[
            "netherlands_home_member_state"
        ],
        netherlands_afm_record_id=row["netherlands_afm_record_id"],
        belgium_fsma_stori_url=_row_value(row, "belgium_fsma_stori_url"),
        belgium_fsma_detail_url=_row_value(row, "belgium_fsma_detail_url"),
        belgium_home_member_state=_row_value(
            row, "belgium_home_member_state"
        ),
        belgium_fsma_record_id=_row_value(row, "belgium_fsma_record_id"),
        portugal_cmvm_sdi_url=_row_value(row, "portugal_cmvm_sdi_url"),
        portugal_cmvm_detail_url=_row_value(
            row, "portugal_cmvm_detail_url"
        ),
        portugal_cmvm_record_id=_row_value(
            row, "portugal_cmvm_record_id"
        ),
        portugal_home_member_state=_row_value(
            row, "portugal_home_member_state"
        ),
        ireland_euronext_oam_url=_row_value(
            row, "ireland_euronext_oam_url"
        ),
        ireland_euronext_direct_url=_row_value(
            row, "ireland_euronext_direct_url"
        ),
        ireland_detail_url=_row_value(row, "ireland_detail_url"),
        ireland_record_id=_row_value(row, "ireland_record_id"),
        ireland_home_member_state=_row_value(
            row, "ireland_home_member_state"
        ),
        spain_cnmv_entity_url=_row_value(row, "spain_cnmv_entity_url"),
        spain_cnmv_nif=_row_value(row, "spain_cnmv_nif"),
        spain_cnmv_record_id=_row_value(row, "spain_cnmv_record_id"),
        spain_bme_company_url=_row_value(row, "spain_bme_company_url"),
        spain_home_member_state=_row_value(row, "spain_home_member_state"),
        spain_pea_country_check=_row_value(row, "spain_pea_country_check"),
        sweden_fi_issuer_url=_row_value(row, "sweden_fi_issuer_url"),
        sweden_fi_record_id=_row_value(row, "sweden_fi_record_id"),
        sweden_fi_detail_url=_row_value(row, "sweden_fi_detail_url"),
        sweden_home_member_state=_row_value(row, "sweden_home_member_state"),
        sweden_nasdaq_company_url=_row_value(row, "sweden_nasdaq_company_url"),
        sweden_pea_country_check=_row_value(row, "sweden_pea_country_check"),
        denmark_dfsa_issuer_url=_row_value(row, "denmark_dfsa_issuer_url"),
        denmark_dfsa_record_id=_row_value(row, "denmark_dfsa_record_id"),
        denmark_dfsa_detail_url=_row_value(row, "denmark_dfsa_detail_url"),
        denmark_home_member_state=_row_value(row, "denmark_home_member_state"),
        denmark_nasdaq_company_url=_row_value(
            row, "denmark_nasdaq_company_url"
        ),
        denmark_pea_country_check=_row_value(
            row, "denmark_pea_country_check"
        ),
        finland_oam_company_id=_row_value(row, "finland_oam_company_id"),
        finland_oam_issuer_url=_row_value(row, "finland_oam_issuer_url"),
        finland_oam_detail_url=_row_value(row, "finland_oam_detail_url"),
        finland_home_member_state=_row_value(row, "finland_home_member_state"),
        finland_nasdaq_company_url=_row_value(row, "finland_nasdaq_company_url"),
        finland_pea_country_check=_row_value(row, "finland_pea_country_check"),
        austria_oekb_oam_id=_row_value(row, "austria_oekb_oam_id"),
        austria_oekb_oam_issuer_url=_row_value(row, "austria_oekb_oam_issuer_url"),
        austria_oekb_oam_detail_url=_row_value(row, "austria_oekb_oam_detail_url"),
        austria_home_member_state=_row_value(row, "austria_home_member_state"),
        austria_pea_country_check=_row_value(row, "austria_pea_country_check"),
        investor_relations_url=_row_value(row, "investor_relations_url"),
        reports_url=_row_value(row, "reports_url"),
        pea_geography_status=_row_value(row, "pea_geography_status"),
    )


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS issuers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            isin TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            oslo_issuer_id TEXT,
            newsweb_url TEXT,
            euronext_company_url TEXT,
            italy_storage_provider TEXT,
            italy_emarket_url TEXT,
            italy_1info_url TEXT,
            borsa_italiana_company_url TEXT,
            netherlands_afm_issuer_url TEXT,
            netherlands_afm_detail_url TEXT,
            netherlands_home_member_state TEXT,
            netherlands_afm_record_id TEXT,
            belgium_fsma_stori_url TEXT,
            belgium_fsma_detail_url TEXT,
            belgium_home_member_state TEXT,
            belgium_fsma_record_id TEXT,
            portugal_cmvm_sdi_url TEXT,
            portugal_cmvm_detail_url TEXT,
            portugal_cmvm_record_id TEXT,
            portugal_home_member_state TEXT,
            ireland_euronext_oam_url TEXT,
            ireland_euronext_direct_url TEXT,
            ireland_detail_url TEXT,
            ireland_record_id TEXT,
            ireland_home_member_state TEXT,
            spain_cnmv_entity_url TEXT,
            spain_cnmv_nif TEXT,
            spain_cnmv_record_id TEXT,
            spain_bme_company_url TEXT,
            spain_home_member_state TEXT,
            spain_pea_country_check TEXT,
            denmark_dfsa_issuer_url TEXT,
            denmark_dfsa_record_id TEXT,
            denmark_dfsa_detail_url TEXT,
            denmark_home_member_state TEXT,
            denmark_nasdaq_company_url TEXT,
            denmark_pea_country_check TEXT,
            investor_relations_url TEXT,
            reports_url TEXT,
            pea_geography_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_issuers_market ON issuers(market);

        CREATE TABLE IF NOT EXISTS issuer_source_resolutions (
            issuer_id INTEGER NOT NULL
                REFERENCES issuers(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            source_name TEXT,
            source_url TEXT,
            detail_url TEXT,
            source_record_id TEXT,
            home_member_state TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(issuer_id, source)
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            issuer_id INTEGER NOT NULL REFERENCES issuers(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            source_document_id TEXT,
            report_number TEXT,
            title TEXT NOT NULL,
            published_at TEXT,
            document_type TEXT NOT NULL,
            source_url TEXT NOT NULL,
            local_path TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE,
            content_type TEXT,
            format TEXT,
            file_size INTEGER NOT NULL,
            downloaded_at TEXT NOT NULL,
            period_end_date TEXT,
            reporting_year INTEGER,
            source_publication_date_raw TEXT,
            source_period_date_raw TEXT,
            date_confidence TEXT,
            date_extraction_reason TEXT,
            official_source INTEGER DEFAULT 1,
            validation_status TEXT,
            confidence TEXT,
            parent_page_url TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_documents_issuer
            ON documents(issuer_id);
        CREATE INDEX IF NOT EXISTS idx_documents_source_url
            ON documents(source_url);

        CREATE TABLE IF NOT EXISTS document_urls (
            source_url TEXT PRIMARY KEY,
            document_id INTEGER NOT NULL
                REFERENCES documents(id) ON DELETE CASCADE,
            first_seen_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_document_urls_document
            ON document_urls(document_id);

        INSERT OR IGNORE INTO document_urls(
            source_url, document_id, first_seen_at
        )
        SELECT source_url, id, downloaded_at FROM documents;

        CREATE TABLE IF NOT EXISTS download_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            issuers_checked INTEGER NOT NULL DEFAULT 0,
            candidates_found INTEGER NOT NULL DEFAULT 0,
            documents_downloaded INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS watch_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            market TEXT NOT NULL,
            issuers_checked INTEGER NOT NULL DEFAULT 0,
            candidates_found INTEGER NOT NULL DEFAULT 0,
            downloaded INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            skipped_too_large INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            report_path TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_watch_runs_market_started
            ON watch_runs(market, started_at);

        CREATE TABLE IF NOT EXISTS watch_run_markets (
            run_id INTEGER NOT NULL
                REFERENCES watch_runs(id) ON DELETE CASCADE,
            market TEXT NOT NULL,
            issuers_checked INTEGER NOT NULL DEFAULT 0,
            candidates_found INTEGER NOT NULL DEFAULT 0,
            downloaded INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            skipped_too_large INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            PRIMARY KEY(run_id, market)
        );

        CREATE INDEX IF NOT EXISTS idx_watch_run_markets_market
            ON watch_run_markets(market, run_id);

        CREATE TABLE IF NOT EXISTS operational_events (
            id INTEGER PRIMARY KEY,
            watch_run_id INTEGER
                REFERENCES watch_runs(id) ON DELETE SET NULL,
            issuer_id INTEGER
                REFERENCES issuers(id) ON DELETE SET NULL,
            market TEXT,
            source TEXT,
            source_document_id TEXT,
            title TEXT,
            published_at TEXT,
            document_type TEXT,
            source_url TEXT,
            event_status TEXT NOT NULL,
            local_path TEXT,
            sha256 TEXT,
            file_size INTEGER,
            message TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_operational_events_status_created
            ON operational_events(event_status, created_at);

        CREATE TABLE IF NOT EXISTS healthcheck_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            report_path TEXT
        );

        CREATE TABLE IF NOT EXISTS source_health_checks (
            id INTEGER PRIMARY KEY,
            healthcheck_run_id INTEGER NOT NULL
                REFERENCES healthcheck_runs(id) ON DELETE CASCADE,
            checked_at TEXT NOT NULL,
            source TEXT NOT NULL,
            market TEXT NOT NULL,
            state TEXT NOT NULL,
            critical INTEGER NOT NULL DEFAULT 1,
            error TEXT,
            details_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_source_health_checks_source
            ON source_health_checks(source, checked_at);

        CREATE TABLE IF NOT EXISTS source_states (
            source TEXT PRIMARY KEY,
            market TEXT NOT NULL,
            state TEXT NOT NULL,
            error TEXT,
            checked_at TEXT NOT NULL,
            context TEXT NOT NULL
        );
        """
        with self.connect() as connection:
            connection.executescript(schema)
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(issuers)"
                ).fetchall()
            }
            for name, definition in (
                ("oslo_issuer_id", "TEXT"),
                ("newsweb_url", "TEXT"),
                ("euronext_company_url", "TEXT"),
                ("italy_storage_provider", "TEXT"),
                ("italy_emarket_url", "TEXT"),
                ("italy_1info_url", "TEXT"),
                ("borsa_italiana_company_url", "TEXT"),
                ("netherlands_afm_issuer_url", "TEXT"),
                ("netherlands_afm_detail_url", "TEXT"),
                ("netherlands_home_member_state", "TEXT"),
                ("netherlands_afm_record_id", "TEXT"),
                ("belgium_fsma_stori_url", "TEXT"),
                ("belgium_fsma_detail_url", "TEXT"),
                ("belgium_home_member_state", "TEXT"),
                ("belgium_fsma_record_id", "TEXT"),
                ("portugal_cmvm_sdi_url", "TEXT"),
                ("portugal_cmvm_detail_url", "TEXT"),
                ("portugal_cmvm_record_id", "TEXT"),
                ("portugal_home_member_state", "TEXT"),
                ("ireland_euronext_oam_url", "TEXT"),
                ("ireland_euronext_direct_url", "TEXT"),
                ("ireland_detail_url", "TEXT"),
                ("ireland_record_id", "TEXT"),
                ("ireland_home_member_state", "TEXT"),
                ("spain_cnmv_entity_url", "TEXT"),
                ("spain_cnmv_nif", "TEXT"),
                ("spain_cnmv_record_id", "TEXT"),
                ("spain_bme_company_url", "TEXT"),
                ("spain_home_member_state", "TEXT"),
                ("spain_pea_country_check", "TEXT"),
                ("sweden_fi_issuer_url", "TEXT"),
                ("sweden_fi_record_id", "TEXT"),
                ("sweden_fi_detail_url", "TEXT"),
                ("sweden_home_member_state", "TEXT"),
                ("sweden_nasdaq_company_url", "TEXT"),
                ("sweden_pea_country_check", "TEXT"),
                ("denmark_dfsa_issuer_url", "TEXT"),
                ("denmark_dfsa_record_id", "TEXT"),
                ("denmark_dfsa_detail_url", "TEXT"),
                ("denmark_home_member_state", "TEXT"),
                ("denmark_nasdaq_company_url", "TEXT"),
                ("denmark_pea_country_check", "TEXT"),
                ("finland_oam_company_id", "TEXT"),
                ("finland_oam_issuer_url", "TEXT"),
                ("finland_oam_detail_url", "TEXT"),
                ("finland_home_member_state", "TEXT"),
                ("finland_nasdaq_company_url", "TEXT"),
                ("finland_pea_country_check", "TEXT"),
                ("austria_oekb_oam_id", "TEXT"),
                ("austria_oekb_oam_issuer_url", "TEXT"),
                ("austria_oekb_oam_detail_url", "TEXT"),
                ("austria_home_member_state", "TEXT"),
                ("austria_pea_country_check", "TEXT"),
                ("investor_relations_url", "TEXT"),
                ("reports_url", "TEXT"),
                ("pea_geography_status", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE issuers ADD COLUMN {name} {definition}"
                    )
            watch_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(watch_runs)"
                ).fetchall()
            }
            for name, definition in (
                ("skipped_too_large", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
            ):
                if name not in watch_columns:
                    connection.execute(
                        f"ALTER TABLE watch_runs ADD COLUMN {name} {definition}"
                    )
            doc_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(documents)"
                ).fetchall()
            }
            for name, definition in (
                ("period_end_date", "TEXT"),
                ("report_number", "TEXT"),
                ("reporting_year", "INTEGER"),
                ("source_publication_date_raw", "TEXT"),
                ("source_period_date_raw", "TEXT"),
                ("date_confidence", "TEXT"),
                ("date_extraction_reason", "TEXT"),
                ("format", "TEXT"),
                ("official_source", "INTEGER DEFAULT 1"),
                ("validation_status", "TEXT"),
                ("confidence", "TEXT"),
                ("parent_page_url", "TEXT"),
            ):
                if name not in doc_columns:
                    connection.execute(
                        f"ALTER TABLE documents ADD COLUMN {name} {definition}"
                    )

    def upsert_issuers(self, issuers: Iterable[Issuer]) -> int:
        now = utc_now()
        values = []
        for issuer in issuers:
            is_italy = issuer.market.casefold() in {
                "euronext milan",
                "euronext star milan",
                "euronext growth milan",
                "euronext miv milan",
            }
            is_netherlands = (
                issuer.market.casefold() == "euronext amsterdam"
            )
            is_belgium = issuer.market.casefold() in {
                "euronext brussels",
                "euronext growth brussels",
            }
            is_portugal = issuer.market.casefold() == "euronext lisbon"
            is_ireland = issuer.market.casefold() == "euronext dublin"
            is_spain = issuer.market.casefold() in {
                "bolsa de madrid",
                "bolsa de barcelona",
                "bolsa de bilbao",
                "bolsa de valencia",
                "bme growth",
                "bme scaleup",
            }
            is_sweden = issuer.market.casefold() in {
                "nasdaq stockholm",
                "nordic growth market",
            }
            is_denmark = issuer.market.casefold() == "nasdaq copenhagen"
            is_finland = issuer.market.casefold() == "nasdaq helsinki"
            is_austria = issuer.market.casefold() == "vienna stock exchange"
            values.append(
                (
                    issuer.name,
                    issuer.isin,
                    issuer.symbol,
                    issuer.market,
                    issuer.italy_storage_provider
                    or ("emarketstorage" if is_italy else None),
                    issuer.italy_emarket_url
                    or (
                        "https://www.emarketstorage.it/it/documenti"
                        if is_italy
                        else None
                    ),
                    issuer.italy_1info_url
                    or (
                        "https://www.1info.it/PORTALE1INFO"
                        if is_italy
                        else None
                    ),
                    issuer.borsa_italiana_company_url
                    or (
                        "https://www.borsaitaliana.it/borsa/actions/"
                        f"scheda/{issuer.isin}.html?lang=it"
                        if is_italy
                        else None
                    ),
                    issuer.netherlands_afm_issuer_url
                    or (
                        "https://www.afm.nl/en/sector/registers/"
                        "meldingenregisters/financiele-verslaggeving"
                        if is_netherlands
                        else None
                    ),
                    issuer.netherlands_afm_detail_url,
                    issuer.netherlands_home_member_state,
                    issuer.netherlands_afm_record_id,
                    issuer.belgium_fsma_stori_url
                    or (
                        "https://www.fsma.be/en/stori"
                        if is_belgium
                        else None
                    ),
                    issuer.belgium_fsma_detail_url,
                    issuer.belgium_home_member_state
                    or ("Belgium" if is_belgium else None),
                    issuer.belgium_fsma_record_id,
                    issuer.portugal_cmvm_sdi_url
                    or (
                        "https://www.cmvm.pt/PInstitucional/Content?"
                        "Input=BD77C8DEEB2702712300D99098915461"
                        "C2A4F65FE4368A561E6AB83D1E580C4D"
                        if is_portugal
                        else None
                    ),
                    issuer.portugal_cmvm_detail_url,
                    issuer.portugal_cmvm_record_id,
                    issuer.portugal_home_member_state
                    or ("Portugal" if is_portugal else None),
                    issuer.ireland_euronext_oam_url
                    or (
                        "https://direct.euronext.com/#/oamfiling"
                        if is_ireland
                        else None
                    ),
                    issuer.ireland_euronext_direct_url
                    or (
                        "https://direct.euronext.com"
                        if is_ireland
                        else None
                    ),
                    issuer.ireland_detail_url,
                    issuer.ireland_record_id,
                    issuer.ireland_home_member_state
                    or ("Ireland" if is_ireland else None),
                    issuer.spain_cnmv_entity_url,
                    issuer.spain_cnmv_nif,
                    issuer.spain_cnmv_record_id,
                    issuer.spain_bme_company_url,
                    issuer.spain_home_member_state
                    or ("Spain" if (is_spain and issuer.spain_home_member_state) else None),
                    issuer.spain_pea_country_check
                    or ("eu_candidate" if is_spain else None),
                    issuer.sweden_fi_issuer_url,
                    issuer.sweden_fi_record_id,
                    issuer.sweden_fi_detail_url,
                    issuer.sweden_home_member_state
                    or ("Sweden" if (is_sweden and issuer.sweden_home_member_state) else None),
                    issuer.sweden_nasdaq_company_url,
                    issuer.sweden_pea_country_check
                    or ("eu_candidate" if is_sweden else None),
                    issuer.denmark_dfsa_issuer_url,
                    issuer.denmark_dfsa_record_id,
                    issuer.denmark_dfsa_detail_url,
                    issuer.denmark_home_member_state,
                    issuer.denmark_nasdaq_company_url,
                    issuer.denmark_pea_country_check
                    or ("eu_candidate" if is_denmark else None),
                    issuer.finland_oam_company_id,
                    issuer.finland_oam_issuer_url,
                    issuer.finland_oam_detail_url,
                    issuer.finland_home_member_state,
                    issuer.finland_nasdaq_company_url,
                    issuer.finland_pea_country_check
                    or ("eu_candidate" if is_finland else None),
                    issuer.austria_oekb_oam_id,
                    issuer.austria_oekb_oam_issuer_url,
                    issuer.austria_oekb_oam_detail_url,
                    issuer.austria_home_member_state or ("Austria" if is_austria else None),
                    issuer.austria_pea_country_check or ("eu_candidate" if is_austria else None),
                    getattr(issuer, "investor_relations_url", None),
                    getattr(issuer, "reports_url", None),
                    getattr(issuer, "pea_geography_status", None),
                    now,
                    now,
                )
            )
        if not values:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO issuers(
                    name, isin, symbol, market, italy_storage_provider,
                    italy_emarket_url, italy_1info_url,
                    borsa_italiana_company_url,
                    netherlands_afm_issuer_url,
                    netherlands_afm_detail_url,
                    netherlands_home_member_state,
                    netherlands_afm_record_id,
                    belgium_fsma_stori_url,
                    belgium_fsma_detail_url,
                    belgium_home_member_state,
                    belgium_fsma_record_id,
                    portugal_cmvm_sdi_url,
                    portugal_cmvm_detail_url,
                    portugal_cmvm_record_id,
                    portugal_home_member_state,
                    ireland_euronext_oam_url,
                    ireland_euronext_direct_url,
                    ireland_detail_url,
                    ireland_record_id,
                    ireland_home_member_state,
                    spain_cnmv_entity_url,
                    spain_cnmv_nif,
                    spain_cnmv_record_id,
                    spain_bme_company_url,
                    spain_home_member_state,
                    spain_pea_country_check,
                    sweden_fi_issuer_url,
                    sweden_fi_record_id,
                    sweden_fi_detail_url,
                    sweden_home_member_state,
                    sweden_nasdaq_company_url,
                    sweden_pea_country_check,
                    denmark_dfsa_issuer_url,
                    denmark_dfsa_record_id,
                    denmark_dfsa_detail_url,
                    denmark_home_member_state,
                    denmark_nasdaq_company_url,
                    denmark_pea_country_check,
                    finland_oam_company_id,
                    finland_oam_issuer_url,
                    finland_oam_detail_url,
                    finland_home_member_state,
                    finland_nasdaq_company_url,
                    finland_pea_country_check,
                    austria_oekb_oam_id,
                    austria_oekb_oam_issuer_url,
                    austria_oekb_oam_detail_url,
                    austria_home_member_state,
                    austria_pea_country_check,
                    investor_relations_url,
                    reports_url,
                    pea_geography_status,
                    created_at, updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(isin) DO UPDATE SET
                    name = excluded.name,
                    symbol = excluded.symbol,
                    market = excluded.market,
                    italy_storage_provider = COALESCE(
                        issuers.italy_storage_provider,
                        excluded.italy_storage_provider
                    ),
                    italy_emarket_url = COALESCE(
                        issuers.italy_emarket_url,
                        excluded.italy_emarket_url
                    ),
                    italy_1info_url = COALESCE(
                        issuers.italy_1info_url,
                        excluded.italy_1info_url
                    ),
                    borsa_italiana_company_url = COALESCE(
                        issuers.borsa_italiana_company_url,
                        excluded.borsa_italiana_company_url
                    ),
                    netherlands_afm_issuer_url = COALESCE(
                        issuers.netherlands_afm_issuer_url,
                        excluded.netherlands_afm_issuer_url
                    ),
                    netherlands_afm_detail_url = COALESCE(
                        issuers.netherlands_afm_detail_url,
                        excluded.netherlands_afm_detail_url
                    ),
                    netherlands_home_member_state = COALESCE(
                        issuers.netherlands_home_member_state,
                        excluded.netherlands_home_member_state
                    ),
                    netherlands_afm_record_id = COALESCE(
                        issuers.netherlands_afm_record_id,
                        excluded.netherlands_afm_record_id
                    ),
                    belgium_fsma_stori_url = COALESCE(
                        issuers.belgium_fsma_stori_url,
                        excluded.belgium_fsma_stori_url
                    ),
                    belgium_fsma_detail_url = COALESCE(
                        issuers.belgium_fsma_detail_url,
                        excluded.belgium_fsma_detail_url
                    ),
                    belgium_home_member_state = COALESCE(
                        issuers.belgium_home_member_state,
                        excluded.belgium_home_member_state
                    ),
                    belgium_fsma_record_id = COALESCE(
                        issuers.belgium_fsma_record_id,
                        excluded.belgium_fsma_record_id
                    ),
                    portugal_cmvm_sdi_url = COALESCE(
                        issuers.portugal_cmvm_sdi_url,
                        excluded.portugal_cmvm_sdi_url
                    ),
                    portugal_cmvm_detail_url = COALESCE(
                        issuers.portugal_cmvm_detail_url,
                        excluded.portugal_cmvm_detail_url
                    ),
                    portugal_cmvm_record_id = COALESCE(
                        issuers.portugal_cmvm_record_id,
                        excluded.portugal_cmvm_record_id
                    ),
                    portugal_home_member_state = COALESCE(
                        issuers.portugal_home_member_state,
                        excluded.portugal_home_member_state
                    ),
                    ireland_euronext_oam_url = COALESCE(
                        issuers.ireland_euronext_oam_url,
                        excluded.ireland_euronext_oam_url
                    ),
                    ireland_euronext_direct_url = COALESCE(
                        issuers.ireland_euronext_direct_url,
                        excluded.ireland_euronext_direct_url
                    ),
                    ireland_detail_url = COALESCE(
                        issuers.ireland_detail_url,
                        excluded.ireland_detail_url
                    ),
                    ireland_record_id = COALESCE(
                        issuers.ireland_record_id,
                        excluded.ireland_record_id
                    ),
                    ireland_home_member_state = COALESCE(
                        issuers.ireland_home_member_state,
                        excluded.ireland_home_member_state
                    ),
                    spain_cnmv_entity_url = COALESCE(
                        issuers.spain_cnmv_entity_url,
                        excluded.spain_cnmv_entity_url
                    ),
                    spain_cnmv_nif = COALESCE(
                        issuers.spain_cnmv_nif,
                        excluded.spain_cnmv_nif
                    ),
                    spain_cnmv_record_id = COALESCE(
                        issuers.spain_cnmv_record_id,
                        excluded.spain_cnmv_record_id
                    ),
                    spain_bme_company_url = COALESCE(
                        issuers.spain_bme_company_url,
                        excluded.spain_bme_company_url
                    ),
                    spain_home_member_state = COALESCE(
                        issuers.spain_home_member_state,
                        excluded.spain_home_member_state
                    ),
                    spain_pea_country_check = COALESCE(
                        issuers.spain_pea_country_check,
                        excluded.spain_pea_country_check
                    ),
                    sweden_fi_issuer_url = COALESCE(
                        issuers.sweden_fi_issuer_url,
                        excluded.sweden_fi_issuer_url
                    ),
                    sweden_fi_record_id = COALESCE(
                        issuers.sweden_fi_record_id,
                        excluded.sweden_fi_record_id
                    ),
                    sweden_fi_detail_url = COALESCE(
                        issuers.sweden_fi_detail_url,
                        excluded.sweden_fi_detail_url
                    ),
                    sweden_home_member_state = COALESCE(
                        issuers.sweden_home_member_state,
                        excluded.sweden_home_member_state
                    ),
                    sweden_nasdaq_company_url = COALESCE(
                        issuers.sweden_nasdaq_company_url,
                        excluded.sweden_nasdaq_company_url
                    ),
                    sweden_pea_country_check = COALESCE(
                        issuers.sweden_pea_country_check,
                        excluded.sweden_pea_country_check
                    ),
                    denmark_dfsa_issuer_url = COALESCE(
                        issuers.denmark_dfsa_issuer_url,
                        excluded.denmark_dfsa_issuer_url
                    ),
                    denmark_dfsa_record_id = COALESCE(
                        issuers.denmark_dfsa_record_id,
                        excluded.denmark_dfsa_record_id
                    ),
                    denmark_dfsa_detail_url = COALESCE(
                        issuers.denmark_dfsa_detail_url,
                        excluded.denmark_dfsa_detail_url
                    ),
                    denmark_home_member_state = COALESCE(
                        issuers.denmark_home_member_state,
                        excluded.denmark_home_member_state
                    ),
                    denmark_nasdaq_company_url = COALESCE(
                        issuers.denmark_nasdaq_company_url,
                        excluded.denmark_nasdaq_company_url
                    ),
                    denmark_pea_country_check = COALESCE(
                        issuers.denmark_pea_country_check,
                        excluded.denmark_pea_country_check
                    ),
                    finland_oam_company_id = COALESCE(
                        issuers.finland_oam_company_id,
                        excluded.finland_oam_company_id
                    ),
                    finland_oam_issuer_url = COALESCE(
                        issuers.finland_oam_issuer_url,
                        excluded.finland_oam_issuer_url
                    ),
                    finland_oam_detail_url = COALESCE(
                        issuers.finland_oam_detail_url,
                        excluded.finland_oam_detail_url
                    ),
                    finland_home_member_state = COALESCE(
                        issuers.finland_home_member_state,
                        excluded.finland_home_member_state
                    ),
                    finland_nasdaq_company_url = COALESCE(
                        issuers.finland_nasdaq_company_url,
                        excluded.finland_nasdaq_company_url
                    ),
                    finland_pea_country_check = COALESCE(
                        issuers.finland_pea_country_check,
                        excluded.finland_pea_country_check
                    ),
                    austria_oekb_oam_id = COALESCE(
                        issuers.austria_oekb_oam_id,
                        excluded.austria_oekb_oam_id
                    ),
                    austria_oekb_oam_issuer_url = COALESCE(
                        issuers.austria_oekb_oam_issuer_url,
                        excluded.austria_oekb_oam_issuer_url
                    ),
                    austria_oekb_oam_detail_url = COALESCE(
                        issuers.austria_oekb_oam_detail_url,
                        excluded.austria_oekb_oam_detail_url
                    ),
                    austria_home_member_state = COALESCE(
                        issuers.austria_home_member_state,
                        excluded.austria_home_member_state
                    ),
                    austria_pea_country_check = COALESCE(
                        issuers.austria_pea_country_check,
                        excluded.austria_pea_country_check
                    ),
                    investor_relations_url = COALESCE(
                        issuers.investor_relations_url,
                        excluded.investor_relations_url
                    ),
                    reports_url = COALESCE(
                        issuers.reports_url,
                        excluded.reports_url
                    ),
                    pea_geography_status = COALESCE(
                        issuers.pea_geography_status,
                        excluded.pea_geography_status
                    ),
                    updated_at = excluded.updated_at
                """,
                values,
            )
        return len(values)

    def list_issuers(self, market: str | None = None) -> list[Issuer]:
        query = """
            SELECT * FROM issuers
        """
        parameters: tuple[str, ...] = ()
        if market is not None:
            query += " WHERE market = ? COLLATE NOCASE"
            parameters = (market,)
        query += " ORDER BY market, name, isin"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_issuer_from_row(row) for row in rows]

    def store_italy_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        storage_provider: str,
        emarket_url: str,
        oneinfo_url: str,
        borsa_italiana_company_url: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market IN (
                    'Euronext Milan',
                    'Euronext Star Milan',
                    'Euronext Growth Milan',
                    'Euronext MIV Milan'
                )
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET italy_storage_provider = ?, italy_emarket_url = ?,
                    italy_1info_url = ?,
                    borsa_italiana_company_url = COALESCE(?, borsa_italiana_company_url),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    storage_provider,
                    emarket_url,
                    oneinfo_url,
                    borsa_italiana_company_url,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                """
                SELECT id, name, isin, symbol, market, oslo_issuer_id,
                       newsweb_url, euronext_company_url,
                       italy_storage_provider, italy_emarket_url,
                       italy_1info_url, borsa_italiana_company_url,
                       netherlands_afm_issuer_url,
                       netherlands_afm_detail_url,
                       netherlands_home_member_state,
                       netherlands_afm_record_id,
                       belgium_fsma_stori_url,
                       belgium_fsma_detail_url,
                       belgium_home_member_state,
                       belgium_fsma_record_id
                FROM issuers WHERE id = ?
                """,
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_oslo_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        isin: str,
        oslo_issuer_id: str | None,
        newsweb_url: str | None,
        euronext_company_url: str,
    ) -> Issuer:
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM issuers
                WHERE isin = ?
                   OR (
                       market = 'Oslo Børs' COLLATE NOCASE
                       AND (
                           symbol = ? COLLATE NOCASE
                           OR name = ? COLLATE NOCASE
                       )
                   )
                ORDER BY CASE WHEN isin = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (isin, symbol, name, isin),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO issuers(
                        name, isin, symbol, market, oslo_issuer_id,
                        newsweb_url, euronext_company_url, created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, 'Oslo Børs', ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        isin,
                        symbol,
                        oslo_issuer_id,
                        newsweb_url,
                        euronext_company_url,
                        now,
                        now,
                    ),
                )
                issuer_id = int(cursor.lastrowid)
            else:
                issuer_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE issuers
                    SET name = ?, isin = ?, symbol = ?, market = 'Oslo Børs',
                        oslo_issuer_id = ?, newsweb_url = ?,
                        euronext_company_url = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        isin,
                        symbol,
                        oslo_issuer_id,
                        newsweb_url,
                        euronext_company_url,
                        now,
                        issuer_id,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(row)

    def store_netherlands_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        issuer_url: str,
        detail_url: str,
        home_member_state: str | None,
        afm_record_id: str,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Euronext Amsterdam' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET netherlands_afm_issuer_url = ?,
                    netherlands_afm_detail_url = ?,
                    netherlands_home_member_state = COALESCE(
                        ?, netherlands_home_member_state
                    ),
                    netherlands_afm_record_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    issuer_url,
                    detail_url,
                    home_member_state,
                    afm_record_id,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_belgium_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        stori_url: str,
        detail_url: str,
        home_member_state: str | None,
        fsma_record_id: str,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market IN (
                    'Euronext Brussels',
                    'Euronext Growth Brussels'
                )
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET belgium_fsma_stori_url = ?,
                    belgium_fsma_detail_url = ?,
                    belgium_home_member_state = COALESCE(
                        ?, belgium_home_member_state
                    ),
                    belgium_fsma_record_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    stori_url,
                    detail_url,
                    home_member_state,
                    fsma_record_id,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_portugal_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        sdi_url: str,
        detail_url: str,
        home_member_state: str | None,
        cmvm_record_id: str,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Euronext Lisbon' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET portugal_cmvm_sdi_url = ?,
                    portugal_cmvm_detail_url = ?,
                    portugal_cmvm_record_id = ?,
                    portugal_home_member_state = COALESCE(
                        ?, portugal_home_member_state
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    sdi_url,
                    detail_url,
                    cmvm_record_id,
                    home_member_state,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_ireland_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        direct_url: str,
        oam_url: str,
        detail_url: str,
        home_member_state: str | None,
        record_id: str,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Euronext Dublin' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET ireland_euronext_direct_url = ?,
                    ireland_euronext_oam_url = ?,
                    ireland_detail_url = ?,
                    ireland_record_id = ?,
                    ireland_home_member_state = COALESCE(
                        ?, ireland_home_member_state
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    direct_url,
                    oam_url,
                    detail_url,
                    record_id,
                    home_member_state,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_spain_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        cnmv_entity_url: str,
        cnmv_nif: str | None,
        cnmv_record_id: str | None,
        bme_company_url: str | None,
        home_member_state: str | None,
        pea_country_check: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market IN (
                    'Bolsa de Madrid',
                    'Bolsa de Barcelona',
                    'Bolsa de Bilbao',
                    'Bolsa de Valencia',
                    'BME Growth',
                    'BME Scaleup'
                ) COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET spain_cnmv_entity_url = ?,
                    spain_cnmv_nif = ?,
                    spain_cnmv_record_id = ?,
                    spain_bme_company_url = ?,
                    spain_home_member_state = COALESCE(
                        ?, spain_home_member_state
                    ),
                    spain_pea_country_check = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    cnmv_entity_url,
                    cnmv_nif,
                    cnmv_record_id,
                    bme_company_url,
                    home_member_state,
                    pea_country_check,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_sweden_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        sweden_fi_issuer_url: str | None,
        sweden_fi_record_id: str | None,
        sweden_fi_detail_url: str | None,
        sweden_home_member_state: str | None,
        sweden_nasdaq_company_url: str | None,
        sweden_pea_country_check: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market IN (
                    'Nasdaq Stockholm',
                    'Nordic Growth Market'
                ) COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET sweden_fi_issuer_url = COALESCE(?, sweden_fi_issuer_url),
                    sweden_fi_record_id = COALESCE(?, sweden_fi_record_id),
                    sweden_fi_detail_url = COALESCE(?, sweden_fi_detail_url),
                    sweden_home_member_state = COALESCE(?, sweden_home_member_state),
                    sweden_nasdaq_company_url = COALESCE(?, sweden_nasdaq_company_url),
                    sweden_pea_country_check = COALESCE(?, sweden_pea_country_check),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    sweden_fi_issuer_url,
                    sweden_fi_record_id,
                    sweden_fi_detail_url,
                    sweden_home_member_state,
                    sweden_nasdaq_company_url,
                    sweden_pea_country_check,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_denmark_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        denmark_dfsa_issuer_url: str | None,
        denmark_dfsa_record_id: str | None,
        denmark_dfsa_detail_url: str | None,
        denmark_home_member_state: str | None,
        denmark_nasdaq_company_url: str | None,
        denmark_pea_country_check: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Nasdaq Copenhagen' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET denmark_dfsa_issuer_url = COALESCE(
                        ?, denmark_dfsa_issuer_url
                    ),
                    denmark_dfsa_record_id = COALESCE(
                        ?, denmark_dfsa_record_id
                    ),
                    denmark_dfsa_detail_url = COALESCE(
                        ?, denmark_dfsa_detail_url
                    ),
                    denmark_home_member_state = COALESCE(
                        ?, denmark_home_member_state
                    ),
                    denmark_nasdaq_company_url = COALESCE(
                        ?, denmark_nasdaq_company_url
                    ),
                    denmark_pea_country_check = COALESCE(
                        ?, denmark_pea_country_check
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    denmark_dfsa_issuer_url,
                    denmark_dfsa_record_id,
                    denmark_dfsa_detail_url,
                    denmark_home_member_state,
                    denmark_nasdaq_company_url,
                    denmark_pea_country_check,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_finland_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        finland_oam_company_id: str | None,
        finland_oam_issuer_url: str | None,
        finland_oam_detail_url: str | None,
        finland_home_member_state: str | None,
        finland_nasdaq_company_url: str | None,
        finland_pea_country_check: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Nasdaq Helsinki' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET finland_oam_company_id = COALESCE(
                        ?, finland_oam_company_id
                    ),
                    finland_oam_issuer_url = COALESCE(
                        ?, finland_oam_issuer_url
                    ),
                    finland_oam_detail_url = COALESCE(
                        ?, finland_oam_detail_url
                    ),
                    finland_home_member_state = COALESCE(
                        ?, finland_home_member_state
                    ),
                    finland_nasdaq_company_url = COALESCE(
                        ?, finland_nasdaq_company_url
                    ),
                    finland_pea_country_check = COALESCE(
                        ?, finland_pea_country_check
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    finland_oam_company_id,
                    finland_oam_issuer_url,
                    finland_oam_detail_url,
                    finland_home_member_state,
                    finland_nasdaq_company_url,
                    finland_pea_country_check,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_austria_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        austria_oekb_oam_id: str | None,
        austria_oekb_oam_issuer_url: str | None,
        austria_oekb_oam_detail_url: str | None,
        austria_home_member_state: str | None,
        austria_pea_country_check: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Vienna Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                UPDATE issuers
                SET austria_oekb_oam_id = COALESCE(
                        ?, austria_oekb_oam_id
                    ),
                    austria_oekb_oam_issuer_url = COALESCE(
                        ?, austria_oekb_oam_issuer_url
                    ),
                    austria_oekb_oam_detail_url = COALESCE(
                        ?, austria_oekb_oam_detail_url
                    ),
                    austria_home_member_state = COALESCE(
                        ?, austria_home_member_state
                    ),
                    austria_pea_country_check = COALESCE(
                        ?, austria_pea_country_check
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    austria_oekb_oam_id,
                    austria_oekb_oam_issuer_url,
                    austria_oekb_oam_detail_url,
                    austria_home_member_state,
                    austria_pea_country_check,
                    now,
                    issuer_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_poland_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        source_name: str | None,
        source_url: str | None,
        detail_url: str | None,
        source_record_id: str | None,
        home_member_state: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Warsaw Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO issuer_source_resolutions(
                    issuer_id, source, source_name, source_url, detail_url,
                    source_record_id, home_member_state, updated_at
                )
                VALUES (?, 'knf_oam', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id, source) DO UPDATE SET
                    source_name = COALESCE(
                        excluded.source_name,
                        issuer_source_resolutions.source_name
                    ),
                    source_url = COALESCE(
                        excluded.source_url,
                        issuer_source_resolutions.source_url
                    ),
                    detail_url = COALESCE(
                        excluded.detail_url,
                        issuer_source_resolutions.detail_url
                    ),
                    source_record_id = COALESCE(
                        excluded.source_record_id,
                        issuer_source_resolutions.source_record_id
                    ),
                    home_member_state = COALESCE(
                        excluded.home_member_state,
                        issuer_source_resolutions.home_member_state
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    issuer_id,
                    source_name,
                    source_url,
                    detail_url,
                    source_record_id,
                    home_member_state,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE issuers
                SET pea_geography_status = COALESCE(
                        pea_geography_status, 'eu_candidate'
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, issuer_id),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_czechia_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        source_name: str | None,
        source_url: str | None,
        detail_url: str | None,
        source_record_id: str | None,
        home_member_state: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Prague Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO issuer_source_resolutions(
                    issuer_id, source, source_name, source_url, detail_url,
                    source_record_id, home_member_state, updated_at
                )
                VALUES (?, 'czechia_cnb_curi', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id, source) DO UPDATE SET
                    source_name = COALESCE(
                        excluded.source_name,
                        issuer_source_resolutions.source_name
                    ),
                    source_url = COALESCE(
                        excluded.source_url,
                        issuer_source_resolutions.source_url
                    ),
                    detail_url = COALESCE(
                        excluded.detail_url,
                        issuer_source_resolutions.detail_url
                    ),
                    source_record_id = COALESCE(
                        excluded.source_record_id,
                        issuer_source_resolutions.source_record_id
                    ),
                    home_member_state = COALESCE(
                        excluded.home_member_state,
                        issuer_source_resolutions.home_member_state
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    issuer_id,
                    source_name,
                    source_url,
                    detail_url,
                    source_record_id,
                    home_member_state,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE issuers
                SET pea_geography_status = COALESCE(
                        pea_geography_status, 'eu_candidate'
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, issuer_id),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_croatia_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        source_name: str | None,
        source_url: str | None,
        detail_url: str | None,
        source_record_id: str | None,
        home_member_state: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Zagreb Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO issuer_source_resolutions(
                    issuer_id, source, source_name, source_url, detail_url,
                    source_record_id, home_member_state, updated_at
                )
                VALUES (?, 'croatia_hanfa_srpi', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id, source) DO UPDATE SET
                    source_name = COALESCE(
                        excluded.source_name,
                        issuer_source_resolutions.source_name
                    ),
                    source_url = COALESCE(
                        excluded.source_url,
                        issuer_source_resolutions.source_url
                    ),
                    detail_url = COALESCE(
                        excluded.detail_url,
                        issuer_source_resolutions.detail_url
                    ),
                    source_record_id = COALESCE(
                        excluded.source_record_id,
                        issuer_source_resolutions.source_record_id
                    ),
                    home_member_state = COALESCE(
                        excluded.home_member_state,
                        issuer_source_resolutions.home_member_state
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    issuer_id,
                    source_name,
                    source_url,
                    detail_url,
                    source_record_id,
                    home_member_state,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE issuers
                SET pea_geography_status = COALESCE(
                        pea_geography_status, 'eu_candidate'
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, issuer_id),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_slovenia_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        source_name: str | None,
        source_url: str | None,
        detail_url: str | None,
        source_record_id: str | None,
        home_member_state: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Ljubljana Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO issuer_source_resolutions(
                    issuer_id, source, source_name, source_url, detail_url,
                    source_record_id, home_member_state, updated_at
                )
                VALUES (?, 'slovenia_oam', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id, source) DO UPDATE SET
                    source_name = COALESCE(
                        excluded.source_name,
                        issuer_source_resolutions.source_name
                    ),
                    source_url = COALESCE(
                        excluded.source_url,
                        issuer_source_resolutions.source_url
                    ),
                    detail_url = COALESCE(
                        excluded.detail_url,
                        issuer_source_resolutions.detail_url
                    ),
                    source_record_id = COALESCE(
                        excluded.source_record_id,
                        issuer_source_resolutions.source_record_id
                    ),
                    home_member_state = COALESCE(
                        excluded.home_member_state,
                        issuer_source_resolutions.home_member_state
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    issuer_id,
                    source_name,
                    source_url,
                    detail_url,
                    source_record_id,
                    home_member_state,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE issuers
                SET pea_geography_status = COALESCE(
                        pea_geography_status, 'eu_candidate'
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, issuer_id),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_estonia_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        source_name: str | None,
        source_url: str | None,
        detail_url: str | None,
        source_record_id: str | None,
        home_member_state: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Tallinn Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO issuer_source_resolutions(
                    issuer_id, source, source_name, source_url, detail_url,
                    source_record_id, home_member_state, updated_at
                )
                VALUES (?, 'estonia_oam', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id, source) DO UPDATE SET
                    source_name = COALESCE(
                        excluded.source_name,
                        issuer_source_resolutions.source_name
                    ),
                    source_url = COALESCE(
                        excluded.source_url,
                        issuer_source_resolutions.source_url
                    ),
                    detail_url = COALESCE(
                        excluded.detail_url,
                        issuer_source_resolutions.detail_url
                    ),
                    source_record_id = COALESCE(
                        excluded.source_record_id,
                        issuer_source_resolutions.source_record_id
                    ),
                    home_member_state = COALESCE(
                        excluded.home_member_state,
                        issuer_source_resolutions.home_member_state
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    issuer_id,
                    source_name,
                    source_url,
                    detail_url,
                    source_record_id,
                    home_member_state,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE issuers
                SET pea_geography_status = COALESCE(
                        pea_geography_status, 'eu_candidate'
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, issuer_id),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_latvia_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        source_name: str | None,
        source_url: str | None,
        detail_url: str | None,
        source_record_id: str | None,
        home_member_state: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Riga Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO issuer_source_resolutions(
                    issuer_id, source, source_name, source_url, detail_url,
                    source_record_id, home_member_state, updated_at
                )
                VALUES (?, 'latvia_oam', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id, source) DO UPDATE SET
                    source_name = COALESCE(
                        excluded.source_name,
                        issuer_source_resolutions.source_name
                    ),
                    source_url = COALESCE(
                        excluded.source_url,
                        issuer_source_resolutions.source_url
                    ),
                    detail_url = COALESCE(
                        excluded.detail_url,
                        issuer_source_resolutions.detail_url
                    ),
                    source_record_id = COALESCE(
                        excluded.source_record_id,
                        issuer_source_resolutions.source_record_id
                    ),
                    home_member_state = COALESCE(
                        excluded.home_member_state,
                        issuer_source_resolutions.home_member_state
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    issuer_id,
                    source_name,
                    source_url,
                    detail_url,
                    source_record_id,
                    home_member_state,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE issuers
                SET pea_geography_status = COALESCE(
                        pea_geography_status, 'eu_candidate'
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, issuer_id),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_lithuania_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        source_name: str | None,
        source_url: str | None,
        detail_url: str | None,
        source_record_id: str | None,
        home_member_state: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Vilnius Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO issuer_source_resolutions(
                    issuer_id, source, source_name, source_url, detail_url,
                    source_record_id, home_member_state, updated_at
                )
                VALUES (?, 'lithuania_oam', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id, source) DO UPDATE SET
                    source_name = COALESCE(
                        excluded.source_name,
                        issuer_source_resolutions.source_name
                    ),
                    source_url = COALESCE(
                        excluded.source_url,
                        issuer_source_resolutions.source_url
                    ),
                    detail_url = COALESCE(
                        excluded.detail_url,
                        issuer_source_resolutions.detail_url
                    ),
                    source_record_id = COALESCE(
                        excluded.source_record_id,
                        issuer_source_resolutions.source_record_id
                    ),
                    home_member_state = COALESCE(
                        excluded.home_member_state,
                        issuer_source_resolutions.home_member_state
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    issuer_id,
                    source_name,
                    source_url,
                    detail_url,
                    source_record_id,
                    home_member_state,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE issuers
                SET pea_geography_status = COALESCE(
                        pea_geography_status, 'eu_candidate'
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, issuer_id),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_slovakia_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        source_name: str | None,
        source_url: str | None,
        detail_url: str | None,
        source_record_id: str | None,
        home_member_state: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Bratislava Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO issuer_source_resolutions(
                    issuer_id, source, source_name, source_url, detail_url,
                    source_record_id, home_member_state, updated_at
                )
                VALUES (?, 'slovakia_nbs_ceri', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id, source) DO UPDATE SET
                    source_name = COALESCE(
                        excluded.source_name,
                        issuer_source_resolutions.source_name
                    ),
                    source_url = COALESCE(
                        excluded.source_url,
                        issuer_source_resolutions.source_url
                    ),
                    detail_url = COALESCE(
                        excluded.detail_url,
                        issuer_source_resolutions.detail_url
                    ),
                    source_record_id = COALESCE(
                        excluded.source_record_id,
                        issuer_source_resolutions.source_record_id
                    ),
                    home_member_state = COALESCE(
                        excluded.home_member_state,
                        issuer_source_resolutions.home_member_state
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    issuer_id,
                    source_name,
                    source_url,
                    detail_url,
                    source_record_id,
                    home_member_state,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE issuers
                SET pea_geography_status = COALESCE(
                        pea_geography_status, 'eu_candidate'
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, issuer_id),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_romania_issuer_resolution(
        self,
        *,
        name: str,
        symbol: str,
        source_name: str | None,
        source_url: str | None,
        detail_url: str | None,
        source_record_id: str | None,
        home_member_state: str | None,
    ) -> Issuer | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE market = 'Bucharest Stock Exchange' COLLATE NOCASE
                  AND (
                    symbol = ? COLLATE NOCASE
                    OR name = ? COLLATE NOCASE
                  )
                ORDER BY CASE
                    WHEN symbol = ? COLLATE NOCASE THEN 0
                    ELSE 1
                END
                LIMIT 1
                """,
                (symbol, name, symbol),
            ).fetchone()
            if row is None:
                return None
            issuer_id = int(row["id"])
            connection.execute(
                """
                INSERT INTO issuer_source_resolutions(
                    issuer_id, source, source_name, source_url, detail_url,
                    source_record_id, home_member_state, updated_at
                )
                VALUES (?, 'romania_asf_oam', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id, source) DO UPDATE SET
                    source_name = COALESCE(
                        excluded.source_name,
                        issuer_source_resolutions.source_name
                    ),
                    source_url = COALESCE(
                        excluded.source_url,
                        issuer_source_resolutions.source_url
                    ),
                    detail_url = COALESCE(
                        excluded.detail_url,
                        issuer_source_resolutions.detail_url
                    ),
                    source_record_id = COALESCE(
                        excluded.source_record_id,
                        issuer_source_resolutions.source_record_id
                    ),
                    home_member_state = COALESCE(
                        excluded.home_member_state,
                        issuer_source_resolutions.home_member_state
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    issuer_id,
                    source_name,
                    source_url,
                    detail_url,
                    source_record_id,
                    home_member_state,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE issuers
                SET pea_geography_status = COALESCE(
                        pea_geography_status, 'eu_candidate'
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, issuer_id),
            )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def store_issuer_website(
        self,
        *,
        isin: str,
        name: str,
        market: str,
        url: str,
    ) -> Issuer:
        now = utc_now()
        normalized_market = "Xetra"
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM issuers
                WHERE isin = ?
                   OR (
                       market = 'Xetra' COLLATE NOCASE
                       AND name = ? COLLATE NOCASE
                   )
                LIMIT 1
                """,
                (isin, name),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO issuers(
                        name, isin, symbol, market, investor_relations_url,
                        reports_url, pea_geography_status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'non_eligible', ?, ?)
                    """,
                    (
                        name,
                        isin,
                        "",
                        normalized_market,
                        url,
                        url,
                        now,
                        now,
                    ),
                )
                issuer_id = int(cursor.lastrowid)
            else:
                issuer_id = int(row["id"])
                connection.execute(
                    """
                    UPDATE issuers
                    SET name = ?, isin = ?, market = ?,
                        investor_relations_url = ?, reports_url = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        isin,
                        normalized_market,
                        url,
                        url,
                        now,
                        issuer_id,
                    ),
                )
            stored = connection.execute(
                "SELECT * FROM issuers WHERE id = ?",
                (issuer_id,),
            ).fetchone()
        return _issuer_from_row(stored)

    def create_run(self, scope: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO download_runs(started_at, scope, status)
                VALUES (?, ?, 'running')
                """,
                (utc_now(), scope),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        issuers_checked: int,
        candidates_found: int,
        documents_downloaded: int,
        duplicates: int,
        errors: int,
        message: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE download_runs
                SET finished_at = ?, status = ?, issuers_checked = ?,
                    candidates_found = ?, documents_downloaded = ?,
                    duplicates = ?, errors = ?, message = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    status,
                    issuers_checked,
                    candidates_found,
                    documents_downloaded,
                    duplicates,
                    errors,
                    message,
                    run_id,
                ),
            )

    def create_watch_run(
        self,
        market: str,
        *,
        started_at: str | None = None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO watch_runs(started_at, market, status)
                VALUES (?, ?, 'running')
                """,
                (started_at or utc_now(), market),
            )
            return int(cursor.lastrowid)

    def finish_watch_run(
        self,
        run_id: int,
        *,
        status: str,
        issuers_checked: int,
        candidates_found: int,
        downloaded: int,
        duplicates: int,
        errors: int,
        skipped_too_large: int = 0,
        report_path: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE watch_runs
                SET ended_at = ?, status = ?, issuers_checked = ?,
                    candidates_found = ?, downloaded = ?, duplicates = ?,
                    skipped_too_large = ?, errors = ?, report_path = ?
                WHERE id = ?
                """,
                (
                    ended_at or utc_now(),
                    status,
                    issuers_checked,
                    candidates_found,
                    downloaded,
                    duplicates,
                    skipped_too_large,
                    errors,
                    report_path,
                    run_id,
                ),
            )

    def record_watch_market_stats(
        self,
        run_id: int,
        market_stats: Mapping[str, object],
    ) -> None:
        rows = []
        for market, stats in market_stats.items():
            errors = int(getattr(stats, "errors", 0))
            rows.append(
                (
                    run_id,
                    market,
                    int(getattr(stats, "issuers_checked", 0)),
                    int(getattr(stats, "candidates_found", 0)),
                    int(getattr(stats, "downloaded", 0)),
                    int(getattr(stats, "duplicates", 0)),
                    int(getattr(stats, "skipped_too_large", 0)),
                    errors,
                    "success" if errors == 0 else "partial",
                )
            )
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO watch_run_markets(
                    run_id, market, issuers_checked, candidates_found,
                    downloaded, duplicates, skipped_too_large, errors, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def add_operational_event(
        self,
        *,
        event_status: str,
        watch_run_id: int | None = None,
        issuer: Issuer | None = None,
        candidate: DocumentCandidate | None = None,
        market: str | None = None,
        source: str | None = None,
        local_path: str | None = None,
        sha256: str | None = None,
        file_size: int | None = None,
        message: str | None = None,
        created_at: str | None = None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO operational_events(
                    watch_run_id, issuer_id, market, source,
                    source_document_id, title, published_at, document_type,
                    source_url, event_status, local_path, sha256, file_size,
                    message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    watch_run_id,
                    issuer.id if issuer else None,
                    issuer.market if issuer else market,
                    candidate.source if candidate else source,
                    candidate.source_document_id if candidate else None,
                    candidate.title if candidate else None,
                    (
                        candidate.published_date.isoformat()
                        if candidate and candidate.published_date
                        else None
                    ),
                    candidate.document_type if candidate else None,
                    candidate.url if candidate else None,
                    event_status,
                    local_path,
                    sha256,
                    file_size,
                    message,
                    created_at or utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def create_healthcheck_run(
        self,
        *,
        started_at: str | None = None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO healthcheck_runs(started_at, status)
                VALUES (?, 'running')
                """,
                (started_at or utc_now(),),
            )
            return int(cursor.lastrowid)

    def finish_healthcheck_run(
        self,
        run_id: int,
        *,
        status: str,
        report_path: str,
        ended_at: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE healthcheck_runs
                SET ended_at = ?, status = ?, report_path = ?
                WHERE id = ?
                """,
                (ended_at or utc_now(), status, report_path, run_id),
            )

    def add_source_health_check(
        self,
        *,
        healthcheck_run_id: int,
        checked_at: str,
        source: str,
        market: str,
        state: str,
        critical: bool,
        error: str | None,
        details: Mapping[str, object],
    ) -> None:
        details_json = json.dumps(
            details,
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_health_checks(
                    healthcheck_run_id, checked_at, source, market, state,
                    critical, error, details_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    healthcheck_run_id,
                    checked_at,
                    source,
                    market,
                    state,
                    int(critical),
                    error,
                    details_json,
                ),
            )
        self.set_source_state(
            source=source,
            market=market,
            state=state,
            error=error,
            checked_at=checked_at,
            context="healthcheck",
        )

    def set_source_state(
        self,
        *,
        source: str,
        market: str,
        state: str,
        error: str | None,
        context: str,
        checked_at: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_states(
                    source, market, state, error, checked_at, context
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    market = excluded.market,
                    state = excluded.state,
                    error = excluded.error,
                    checked_at = excluded.checked_at,
                    context = excluded.context
                """,
                (
                    source,
                    market,
                    state,
                    error,
                    checked_at or utc_now(),
                    context,
                ),
            )

    def issuer_counts_by_market(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT market, COUNT(*) AS issuer_count
                FROM issuers
                GROUP BY market
                ORDER BY market
                """
            ).fetchall()

    def document_counts(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT issuers.market, documents.source,
                       documents.document_type, COUNT(*) AS document_count
                FROM documents
                JOIN issuers ON issuers.id = documents.issuer_id
                GROUP BY issuers.market, documents.source,
                         documents.document_type
                ORDER BY issuers.market, documents.source,
                         documents.document_type
                """
            ).fetchall()

    def latest_watch_runs_by_market(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                WITH per_market AS (
                    SELECT wrm.market, wr.id AS run_id, wr.started_at,
                           wr.ended_at, wrm.status, wrm.issuers_checked,
                           wrm.candidates_found, wrm.downloaded,
                           wrm.duplicates, wrm.skipped_too_large,
                           wrm.errors, wr.report_path
                    FROM watch_run_markets AS wrm
                    JOIN watch_runs AS wr ON wr.id = wrm.run_id
                    UNION ALL
                    SELECT wr.market, wr.id AS run_id, wr.started_at,
                           wr.ended_at, wr.status, wr.issuers_checked,
                           wr.candidates_found, wr.downloaded,
                           wr.duplicates, wr.skipped_too_large,
                           wr.errors, wr.report_path
                    FROM watch_runs AS wr
                    WHERE wr.market NOT LIKE '% + %'
                      AND NOT EXISTS (
                          SELECT 1 FROM watch_run_markets AS wrm
                          WHERE wrm.run_id = wr.id
                      )
                ),
                ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY market
                               ORDER BY started_at DESC, run_id DESC
                           ) AS row_number
                    FROM per_market
                )
                SELECT * FROM ranked
                WHERE row_number = 1
                ORDER BY market
                """
            ).fetchall()

    def recent_documents(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT documents.*, issuers.name AS issuer_name,
                       issuers.isin, issuers.market
                FROM documents
                JOIN issuers ON issuers.id = documents.issuer_id
                ORDER BY documents.downloaded_at DESC, documents.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def recent_errors(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT operational_events.*, issuers.name AS issuer_name,
                       issuers.isin
                FROM operational_events
                LEFT JOIN issuers ON issuers.id = operational_events.issuer_id
                WHERE event_status = 'error'
                ORDER BY created_at DESC, operational_events.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def unhealthy_source_states(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT source, market, state, error, checked_at, context
                FROM source_states
                WHERE state IN ('degraded', 'unavailable')
                ORDER BY state DESC, market, source
                """
            ).fetchall()

    def latest_documents_for_export(self) -> tuple[str | None, list[sqlite3.Row]]:
        with self.connect() as connection:
            latest_date_row = connection.execute(
                "SELECT MAX(date(downloaded_at)) AS latest_date FROM documents"
            ).fetchone()
            latest_date = latest_date_row["latest_date"]
            if latest_date is None:
                return None, []
            rows = self.documents_for_export_by_download_date(
                connection,
                where_clause="date(documents.downloaded_at) = ?",
                parameters=(latest_date,),
            )
            return str(latest_date), rows

    def documents_since_for_export(self, since: date) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return self.documents_for_export_by_download_date(
                connection,
                where_clause="date(documents.downloaded_at) >= ?",
                parameters=(since.isoformat(),),
            )

    def documents_for_export_by_download_date(
        self,
        connection: sqlite3.Connection,
        *,
        where_clause: str,
        parameters: tuple[object, ...],
    ) -> list[sqlite3.Row]:
        return connection.execute(
            f"""
            SELECT documents.downloaded_at, issuers.name AS company,
                   issuers.isin, issuers.market, documents.source,
                   documents.source_document_id,
                   documents.report_number,
                   documents.document_type, documents.title,
                   documents.published_at, documents.period_end_date,
                   documents.reporting_year,
                   documents.source_url, documents.local_path,
                   documents.sha256, documents.content_type,
                   documents.format, documents.file_size,
                   documents.official_source
            FROM documents
            JOIN issuers ON issuers.id = documents.issuer_id
            WHERE {where_clause}
              AND (documents.validation_status IS NULL OR documents.validation_status != 'rejected_false_positive')
            ORDER BY documents.downloaded_at DESC, documents.id DESC
            """,
            parameters,
        ).fetchall()

    def get_document_by_url(
        self, issuer_id: int, source_url: str
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM documents
                WHERE issuer_id = ? AND source_url = ?
                ORDER BY id DESC LIMIT 1
                """,
                (issuer_id, source_url),
            ).fetchone()

    def get_document_by_source_url(
        self,
        source_url: str,
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT documents.*
                FROM document_urls
                JOIN documents ON documents.id = document_urls.document_id
                WHERE document_urls.source_url = ?
                LIMIT 1
                """,
                (source_url,),
            ).fetchone()

    def add_document_url_alias(
        self,
        *,
        source_url: str,
        sha256: str,
    ) -> bool:
        with self.connect() as connection:
            document = connection.execute(
                "SELECT id FROM documents WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
            if document is None:
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO document_urls(
                    source_url, document_id, first_seen_at
                )
                VALUES (?, ?, ?)
                """,
                (source_url, document["id"], utc_now()),
            )
            return cursor.rowcount == 1

    def get_document_by_sha256(self, sha256: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM documents WHERE sha256 = ?",
                (sha256,),
            ).fetchone()

    def add_document(
        self,
        *,
        issuer_id: int,
        candidate: DocumentCandidate,
        local_path: str,
        sha256: str,
        content_type: str | None,
        file_size: int,
    ) -> bool:
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO documents(
                        issuer_id, source, source_document_id, report_number,
                        title,
                        published_at, document_type, source_url, local_path,
                        sha256, content_type, format, file_size, downloaded_at,
                        period_end_date, reporting_year, source_publication_date_raw,
                        source_period_date_raw, date_confidence, date_extraction_reason,
                        official_source, validation_status, confidence, parent_page_url
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issuer_id,
                        candidate.source,
                        candidate.source_document_id,
                        candidate.metadata.get("report_number"),
                        candidate.title,
                        candidate.published_at.isoformat()
                        if candidate.published_at
                        else None,
                        candidate.document_type,
                        candidate.url,
                        local_path,
                        sha256,
                        content_type,
                        (
                            str(candidate.metadata.get("file_format"))
                            if candidate.metadata.get("file_format")
                            else Path(local_path).suffix.lstrip(".").casefold()
                            or None
                        ),
                        file_size,
                        utc_now(),
                        candidate.period_end_date.isoformat()
                        if candidate.period_end_date
                        else None,
                        candidate.reporting_year,
                        candidate.source_publication_date_raw,
                        candidate.source_period_date_raw,
                        candidate.date_confidence,
                        candidate.date_extraction_reason,
                        candidate.metadata.get("official_source", 1),
                        candidate.metadata.get("validation_status"),
                        candidate.metadata.get("confidence"),
                        candidate.metadata.get("parent_page_url"),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO document_urls(
                        source_url, document_id, first_seen_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (candidate.url, cursor.lastrowid, utc_now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def initialize_web_search_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS web_search_jobs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            status TEXT NOT NULL,
            request_json TEXT NOT NULL,
            markets_count INTEGER NOT NULL,
            results_count INTEGER NOT NULL DEFAULT 0,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            errors_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS web_search_market_runs (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES web_search_jobs(id) ON DELETE CASCADE,
            market TEXT NOT NULL,
            source TEXT,
            status TEXT NOT NULL,
            candidates_returned INTEGER NOT NULL DEFAULT 0,
            results_count INTEGER NOT NULL DEFAULT 0,
            warning TEXT,
            error TEXT,
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS web_search_results (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES web_search_jobs(id) ON DELETE CASCADE,
            market TEXT NOT NULL,
            source TEXT NOT NULL,
            source_document_id TEXT,
            published_at TEXT,
            period_end_date TEXT,
            reporting_year INTEGER,
            document_type TEXT NOT NULL,
            classification TEXT,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            issuer_name TEXT,
            issuer_isin TEXT,
            issuer_lei TEXT,
            category TEXT,
            file_format TEXT,
            date_confidence TEXT,
            source_publication_date_raw TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_web_search_results_job
            ON web_search_results(job_id);
        CREATE INDEX IF NOT EXISTS idx_web_search_results_type
            ON web_search_results(document_type);
        CREATE INDEX IF NOT EXISTS idx_web_search_results_market
            ON web_search_results(market);
        CREATE INDEX IF NOT EXISTS idx_web_search_results_url
            ON web_search_results(url);
        """
        with self.connect() as connection:
            connection.executescript(schema)
