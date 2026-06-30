from __future__ import annotations

import csv
import logging
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests

from config import Settings
from connectors import SUPPORTED_WATCH_MARKETS, connector_for_market
from connectors.belgium_fsma_stori import BelgiumFsmaStoriConnector
from connectors.bulgaria_bse_x3news import (
    BulgariaBseX3NewsConnector,
    select_active_buckets,
)
from connectors.finland_oam import FinlandOamConnector
from connectors.lithuania_oam import LithuaniaOamConnector
from connectors.base import DocumentCandidate
from http_client import build_http_session
from load_watchlist import is_valid_isin, load_watchlist, normalize_market

LOGGER = logging.getLogger(__name__)

CSV_FIELDS = ("Name", "ISIN", "Symbol", "Market", "Source", "Notes")

EURONEXT_MARKETS = {
    "Euronext Paris",
    "Oslo Børs",
    "Euronext Milan",
    "Euronext Star Milan",
    "Euronext Growth Milan",
    "Euronext MIV Milan",
    "Euronext Amsterdam",
    "Euronext Brussels",
    "Euronext Growth Brussels",
    "Euronext Lisbon",
    "Euronext Dublin",
}

OAM_DERIVE_MARKETS = (
    "Bolsa de Madrid",
    "Bolsa de Barcelona",
    "Bolsa de Bilbao",
    "Bolsa de Valencia",
    "BME Growth",
    "BME Scaleup",
    "Nasdaq Stockholm",
    "Nordic Growth Market",
    "Nasdaq Copenhagen",
    "Vienna Stock Exchange",
    "Warsaw Stock Exchange",
    "Prague Stock Exchange",
    "Zagreb Stock Exchange",
    "Ljubljana Stock Exchange",
    "Tallinn Stock Exchange",
    "Riga Stock Exchange",
    "Vilnius Stock Exchange",
    "Bratislava Stock Exchange",
    "Bucharest Stock Exchange",
    "Malta Stock Exchange",
)


@dataclass(frozen=True, slots=True)
class IssuerListEntry:
    name: str
    isin: str
    symbol: str
    market: str
    source: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class SyncResult:
    market: str
    path: Path
    total_rows: int
    importable_rows: int
    source: str
    error: str | None = None


def market_slug(market: str) -> str:
    normalized = normalize_market(market)
    decomposed = unicodedata.normalize("NFKD", normalized)
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    for source, target in (("ø", "o"), ("å", "a"), ("æ", "ae")):
        ascii_value = ascii_value.replace(source, target)
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_value.casefold()).strip("_")
    return slug or "unknown"


def _normalize_identity(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9\u0400-\u04ff]+", " ", ascii_value.casefold()).strip()


def _split_market_field(market_field: str) -> tuple[str, ...]:
    parts = []
    for raw in market_field.split(","):
        cleaned = raw.strip().strip('"')
        if cleaned:
            parts.append(normalize_market(cleaned))
    return tuple(dict.fromkeys(parts))


def _dedupe_key(entry: IssuerListEntry) -> str:
    identity = _normalize_identity(entry.name)
    if identity:
        return f"name:{identity}|{entry.market.casefold()}"
    if entry.isin:
        return f"isin:{entry.isin.upper()}"
    return f"anon:{entry.symbol}|{entry.market.casefold()}"


def _dedupe_entries(entries: Iterable[IssuerListEntry]) -> list[IssuerListEntry]:
    merged: dict[str, IssuerListEntry] = {}
    for entry in entries:
        key = _dedupe_key(entry)
        current = merged.get(key)
        if current is None:
            merged[key] = entry
            continue
        if not current.isin and entry.isin:
            merged[key] = entry
        elif current.isin == entry.isin and not current.symbol and entry.symbol:
            merged[key] = IssuerListEntry(
                name=current.name or entry.name,
                isin=current.isin or entry.isin,
                symbol=entry.symbol,
                market=current.market,
                source=current.source,
                notes=current.notes or entry.notes,
            )
    return sorted(
        merged.values(),
        key=lambda item: (item.market.casefold(), item.name.casefold(), item.isin),
    )


@dataclass(frozen=True, slots=True)
class IsinIndex:
    by_name: dict[str, IssuerListEntry]
    by_symbol: dict[str, IssuerListEntry]


def _build_isin_index(entries: Iterable[IssuerListEntry]) -> IsinIndex:
    by_name: dict[str, IssuerListEntry] = {}
    by_symbol: dict[str, IssuerListEntry] = {}
    for entry in entries:
        if not entry.isin:
            continue
        name_key = _normalize_identity(entry.name)
        if name_key:
            by_name.setdefault(name_key, entry)
        symbol_key = _normalize_identity(entry.symbol).replace(" ", "")
        if symbol_key:
            by_symbol.setdefault(symbol_key, entry)
    return IsinIndex(by_name=by_name, by_symbol=by_symbol)


def _enrich_entry(entry: IssuerListEntry, index: IsinIndex) -> IssuerListEntry:
    if entry.isin:
        return entry
    name_key = _normalize_identity(entry.name)
    symbol_key = _normalize_identity(entry.symbol).replace(" ", "")
    match = index.by_name.get(name_key) or index.by_symbol.get(symbol_key)
    if match is None and name_key:
        for candidate_key, candidate in index.by_name.items():
            if name_key in candidate_key or candidate_key in name_key:
                match = candidate
                break
    if match is None:
        return entry
    return IssuerListEntry(
        name=entry.name or match.name,
        isin=match.isin,
        symbol=entry.symbol or match.symbol,
        market=entry.market,
        source=entry.source,
        notes=entry.notes,
    )


def write_market_csv(path: Path, entries: Iterable[IssuerListEntry]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _dedupe_entries(entries)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, delimiter=";")
        writer.writeheader()
        for entry in rows:
            writer.writerow(
                {
                    "Name": entry.name,
                    "ISIN": entry.isin,
                    "Symbol": entry.symbol,
                    "Market": entry.market,
                    "Source": entry.source,
                    "Notes": entry.notes,
                }
            )
    return len(rows)


def download_euronext_entries(
    settings: Settings,
    session: requests.Session,
    *,
    url: str | None = None,
) -> list[IssuerListEntry]:
    download_url = url or settings.euronext_regulated_list_url
    response = session.get(download_url, timeout=settings.http_timeout_seconds)
    response.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as temp_file:
        temp_path = Path(temp_file.name)
        temp_file.write(response.content)
    try:
        result = load_watchlist(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)

    entries: list[IssuerListEntry] = []
    for issuer in result.issuers:
        markets = _split_market_field(issuer.market)
        for market in markets:
            entries.append(
                IssuerListEntry(
                    name=issuer.name,
                    isin=issuer.isin,
                    symbol=issuer.symbol,
                    market=market,
                    source="euronext_regulated_list",
                )
            )
    return entries


def _entries_from_candidates(
    candidates: Iterable[DocumentCandidate],
    *,
    market: str,
    source: str,
) -> list[IssuerListEntry]:
    entries: list[IssuerListEntry] = []
    for candidate in candidates:
        metadata = candidate.metadata
        name = str(
            metadata.get("issuer_name")
            or metadata.get("company")
            or metadata.get("issuing_institution")
            or ""
        ).strip()
        if not name:
            continue
        raw_isins = metadata.get("issuer_isins") or metadata.get("isins") or []
        if isinstance(raw_isins, str):
            raw_isins = [raw_isins]
        isin = ""
        for value in raw_isins:
            candidate_isin = str(value).strip().upper()
            if is_valid_isin(candidate_isin):
                isin = candidate_isin
                break
        if not isin:
            single = str(
                metadata.get("issuer_isin") or metadata.get("isin") or ""
            ).strip().upper()
            if is_valid_isin(single):
                isin = single
        symbol = str(
            metadata.get("issuer_symbol") or metadata.get("symbol") or ""
        ).strip()
        entries.append(
            IssuerListEntry(
                name=name,
                isin=isin,
                symbol=symbol,
                market=market,
                source=source,
            )
        )
    return entries


def _fetch_oam_derived(
    settings: Settings,
    session: requests.Session,
    market: str,
    *,
    lookback_days: int = 365 * 3,
    limit: int = 5000,
) -> list[IssuerListEntry]:
    connector = connector_for_market(market, settings=settings, session=session)
    if connector is None:
        raise ValueError(f"Aucun connecteur pour {market}")
    since = date.today() - timedelta(days=lookback_days)
    candidates = connector.search_recent_documents(
        market,
        since=since,
        limit=limit,
    )
    return _entries_from_candidates(
        candidates,
        market=market,
        source=getattr(connector, "source_name", "oam_derived"),
    )


def fetch_belgium_entries(
    settings: Settings,
    session: requests.Session,
) -> list[IssuerListEntry]:
    connector = BelgiumFsmaStoriConnector(
        session=session,
        base_url=settings.belgium_fsma_stori_base_url,
        rate_limit_seconds=settings.belgium_rate_limit_seconds,
        lookback_days=settings.belgium_lookback_days,
        timeout=settings.http_timeout_seconds,
    )
    companies = connector._get_companies()
    entries: list[IssuerListEntry] = []
    for company in companies:
        name = str(
            company.get("abbreviation")
            or company.get("localisedName")
            or ""
        ).strip()
        if not name:
            continue
        symbol = str(company.get("abbreviation") or "").strip()
        entries.append(
            IssuerListEntry(
                name=name,
                isin="",
                symbol=symbol,
                market="Euronext Brussels",
                source="fsma_stori_companies",
                notes="ISIN à enrichir via Euronext si absent",
            )
        )
        growth_name = str(company.get("localisedName") or "").strip()
        if growth_name and growth_name != name:
            entries.append(
                IssuerListEntry(
                    name=growth_name,
                    isin="",
                    symbol=symbol,
                    market="Euronext Growth Brussels",
                    source="fsma_stori_companies",
                    notes="ISIN à enrichir via Euronext si absent",
                )
            )
    return entries


def fetch_netherlands_entries(
    settings: Settings,
    session: requests.Session,
) -> list[IssuerListEntry]:
    connector = connector_for_market(
        "Euronext Amsterdam",
        settings=settings,
        session=session,
    )
    if connector is None:
        return []
    records = connector._load_records()
    entries: list[IssuerListEntry] = []
    seen: set[str] = set()
    for record in records:
        name = str(record.issuing_institution or "").strip()
        if not name:
            continue
        key = _normalize_identity(name)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            IssuerListEntry(
                name=name,
                isin="",
                symbol="",
                market="Euronext Amsterdam",
                source="afm_financial_reporting_register",
                notes="Registre AFM — rapports financiers, pas listing pur",
            )
        )
    return entries


def fetch_finland_entries(
    settings: Settings,
    session: requests.Session,
) -> list[IssuerListEntry]:
    connector = FinlandOamConnector(
        session=session,
        base_url=settings.finland_oam_base_url,
        rate_limit_seconds=settings.finland_rate_limit_seconds,
        lookback_days=settings.finland_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.finland_verify_ssl,
    )
    _, companies, _ = connector._fetch_csrf_and_options()
    entries: list[IssuerListEntry] = []
    for company in companies:
        label = str(company.get("label") or "").strip()
        if not label or label.casefold() == "all companies":
            continue
        symbol = str(company.get("value") or "").strip()
        entries.append(
            IssuerListEntry(
                name=label,
                isin="",
                symbol=symbol,
                market="Nasdaq Helsinki",
                source="finland_oam_company_select",
                notes="ISIN à enrichir via Euronext/OAM si absent",
            )
        )
    return entries


def fetch_lithuania_entries(
    settings: Settings,
    session: requests.Session,
) -> list[IssuerListEntry]:
    connector = LithuaniaOamConnector(
        session=session,
        base_url=settings.lithuania_oam_base_url,
        rate_limit_seconds=settings.lithuania_oam_rate_limit_seconds,
        lookback_days=settings.lithuania_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.lithuania_oam_verify_ssl,
    )
    response = connector.session.get(
        connector.search_url,
        timeout=connector.timeout,
        verify=connector.verify_ssl,
    )
    response.raise_for_status()
    from bs4 import BeautifulSoup
    import json

    soup = BeautifulSoup(response.text, "html.parser")
    company_select = soup.find(id="company-select")
    companies: list[dict[str, str]] = []
    if company_select and company_select.get("options"):
        companies = json.loads(company_select.get("options"))
    entries: list[IssuerListEntry] = []
    for company in companies:
        label = str(company.get("label") or "").strip()
        if not label:
            continue
        entries.append(
            IssuerListEntry(
                name=label,
                isin="",
                symbol=str(company.get("value") or "").strip(),
                market="Vilnius Stock Exchange",
                source="lithuania_oam_company_select",
                notes="ISIN à enrichir via OAM si absent",
            )
        )
    return entries


def fetch_bulgaria_entries(
    settings: Settings,
    session: requests.Session,
) -> list[IssuerListEntry]:
    connector = BulgariaBseX3NewsConnector(
        session=session,
        base_url=settings.bulgaria_bse_x3news_base_url,
        rate_limit_seconds=settings.bulgaria_bse_x3news_rate_limit_seconds,
        lookback_days=settings.bulgaria_bse_x3news_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.bulgaria_bse_x3news_verify_ssl,
        max_active_buckets=12,
        max_issuer_scans=10_000,
        max_candidates_per_source=10_000,
    )
    root_entries = connector._fetch_index(
        connector.companies_url,
        label="BSE issuer list sync",
    )
    buckets = select_active_buckets(
        root_entries,
        since=None,
        max_buckets=12,
    )
    names: set[str] = set()
    for bucket in buckets:
        bucket_url = urljoin(connector.companies_url, bucket.href)
        issuers = connector._fetch_index(
            bucket_url,
            label=f"BSE issuer bucket {bucket.name}",
        )
        for issuer in issuers:
            cleaned = issuer.name.rstrip("/").strip()
            if cleaned:
                names.add(cleaned)
    return [
        IssuerListEntry(
            name=name,
            isin="",
            symbol="",
            market="Bulgarian Stock Exchange",
            source="bse_x3news_index",
            notes="Pas d'ISIN dans l'index BSE — compléter manuellement",
        )
        for name in sorted(names, key=lambda value: value.casefold())
    ]


def _group_euronext_entries(
    entries: Iterable[IssuerListEntry],
) -> dict[str, list[IssuerListEntry]]:
    grouped: dict[str, list[IssuerListEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.market, []).append(entry)
    return grouped


def _markets_to_sync(market_filter: str | None) -> tuple[str, ...]:
    if market_filter:
        return (normalize_market(market_filter),)
    return SUPPORTED_WATCH_MARKETS


def sync_issuer_lists(
    settings: Settings,
    *,
    output_dir: str | Path = "watchlists",
    market: str | None = None,
    import_to_db: bool = False,
    database=None,
) -> list[SyncResult]:
    output_path = Path(output_dir)
    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
        verify=settings.http_verify_ssl,
    )
    results: list[SyncResult] = []
    try:
        euronext_entries = download_euronext_entries(settings, session)
        isin_index = _build_isin_index(euronext_entries)
        grouped_euronext = _group_euronext_entries(euronext_entries)

        supplemental_fetchers: dict[str, Callable[[], list[IssuerListEntry]]] = {
            "Euronext Brussels": lambda: fetch_belgium_entries(settings, session),
            "Euronext Growth Brussels": lambda: fetch_belgium_entries(settings, session),
            "Euronext Amsterdam": lambda: fetch_netherlands_entries(settings, session),
            "Nasdaq Helsinki": lambda: fetch_finland_entries(settings, session),
            "Vilnius Stock Exchange": lambda: fetch_lithuania_entries(settings, session),
            "Bulgarian Stock Exchange": lambda: fetch_bulgaria_entries(settings, session),
        }

        for target_market in _markets_to_sync(market):
            try:
                entries: list[IssuerListEntry] = []
                source = "none"
                if target_market in EURONEXT_MARKETS:
                    entries = list(grouped_euronext.get(target_market, ()))
                    source = "euronext_regulated_list"
                elif target_market in supplemental_fetchers:
                    entries = [
                        entry
                        for entry in supplemental_fetchers[target_market]()
                        if entry.market == target_market
                    ]
                    source = entries[0].source if entries else "supplemental"
                elif target_market in OAM_DERIVE_MARKETS:
                    entries = _fetch_oam_derived(settings, session, target_market)
                    source = entries[0].source if entries else "oam_derived"
                else:
                    results.append(
                        SyncResult(
                            market=target_market,
                            path=output_path / f"{market_slug(target_market)}.csv",
                            total_rows=0,
                            importable_rows=0,
                            source="unsupported",
                            error="Aucune source de listing complète configurée",
                        )
                    )
                    continue

                entries = [_enrich_entry(entry, isin_index) for entry in entries]
                csv_path = output_path / f"{market_slug(target_market)}.csv"
                total_rows = write_market_csv(csv_path, entries)
                importable_rows = sum(
                    1 for entry in _dedupe_entries(entries) if is_valid_isin(entry.isin)
                )
                if import_to_db and database is not None and importable_rows:
                    import_result = load_watchlist(csv_path)
                    database.upsert_issuers(import_result.issuers)
                results.append(
                    SyncResult(
                        market=target_market,
                        path=csv_path,
                        total_rows=total_rows,
                        importable_rows=importable_rows,
                        source=source,
                    )
                )
                LOGGER.info(
                    "Watchlist %s: %d lignes (%d importables) -> %s",
                    target_market,
                    total_rows,
                    importable_rows,
                    csv_path,
                )
            except Exception as exc:
                LOGGER.exception(
                    "Échec synchronisation watchlist pour %s",
                    target_market,
                )
                results.append(
                    SyncResult(
                        market=target_market,
                        path=output_path / f"{market_slug(target_market)}.csv",
                        total_rows=0,
                        importable_rows=0,
                        source="error",
                        error=str(exc),
                    )
                )
    finally:
        session.close()
    return results