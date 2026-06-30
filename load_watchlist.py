from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from models import Issuer

LOGGER = logging.getLogger(__name__)
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

MARKET_ALIASES = {
    "xpar": "Euronext Paris",
    "paris": "Euronext Paris",
    "euronext paris": "Euronext Paris",
    "oslo": "Oslo Børs",
    "oslo bors": "Oslo Børs",
    "oslo børs": "Oslo Børs",
    "xosl": "Oslo Børs",
    "merkur market": "Oslo Børs",
    "euronext growth oslo": "Oslo Børs",
    "milan": "Euronext Milan",
    "italy": "Euronext Milan",
    "borsa italiana": "Euronext Milan",
    "euronext milan": "Euronext Milan",
    "euronext star milan": "Euronext Star Milan",
    "euronext growth milan": "Euronext Growth Milan",
    "euronext miv milan": "Euronext MIV Milan",
    "mta": "Euronext Milan",
    "mta - star": "Euronext Star Milan",
    "mta star": "Euronext Star Milan",
    "aim italia": "Euronext Growth Milan",
    "aim - italia": "Euronext Growth Milan",
    "aim -italia/mercato alternativo del capitale": "Euronext Growth Milan",
    "aim - italia/mercato alternativo del capitale": "Euronext Growth Milan",
    "miv - azioni": "Euronext MIV Milan",
    "miv - quote": "Euronext MIV Milan",
    "amsterdam": "Euronext Amsterdam",
    "ams": "Euronext Amsterdam",
    "xams": "Euronext Amsterdam",
    "euronext amsterdam": "Euronext Amsterdam",
    "euronext growth amsterdam": "Euronext Amsterdam",
    "brussels": "Euronext Brussels",
    "bru": "Euronext Brussels",
    "xbru": "Euronext Brussels",
    "euronext brussels": "Euronext Brussels",
    "growth brussels": "Euronext Growth Brussels",
    "euronext growth brussels": "Euronext Growth Brussels",
    "alternext": "Euronext Growth Brussels",
    "alternext brussels": "Euronext Growth Brussels",
    "lisbon": "Euronext Lisbon",
    "lis": "Euronext Lisbon",
    "xlis": "Euronext Lisbon",
    "euronext lisbon": "Euronext Lisbon",
    "euronext growth lisbon": "Euronext Lisbon",
    "psi": "Euronext Lisbon",
    "bolsa de lisboa": "Euronext Lisbon",
    "dublin": "Euronext Dublin",
    "euronext dublin": "Euronext Dublin",
    "euronext growth dublin": "Euronext Dublin",
    "global exchange market": "Euronext Dublin",
    "irish stock exchange": "Euronext Dublin",
    "ise": "Euronext Dublin",
    "xmsm": "Euronext Dublin",
    "bolsa de madrid": "Bolsa de Madrid",
    "bme": "Bolsa de Madrid",
    "madrid": "Bolsa de Madrid",
    "spanish stock exchange": "Bolsa de Madrid",
    "bolsa de barcelona": "Bolsa de Barcelona",
    "bolsa de bilbao": "Bolsa de Bilbao",
    "bolsa de valencia": "Bolsa de Valencia",
    "bme growth": "BME Growth",
    "bme scaleup": "BME Scaleup",
    "nasdaq stockholm": "Nasdaq Stockholm",
    "stockholm": "Nasdaq Stockholm",
    "omx stockholm": "Nasdaq Stockholm",
    "nasdaq omx stockholm": "Nasdaq Stockholm",
    "swedish stock exchange": "Nasdaq Stockholm",
    "ngm": "Nordic Growth Market",
    "nordic growth market": "Nordic Growth Market",
    "nasdaq copenhagen": "Nasdaq Copenhagen",
    "copenhagen": "Nasdaq Copenhagen",
    "omx copenhagen": "Nasdaq Copenhagen",
    "nasdaq omx copenhagen": "Nasdaq Copenhagen",
    "copenhagen stock exchange": "Nasdaq Copenhagen",
    "danish stock exchange": "Nasdaq Copenhagen",
    "first north denmark": "Nasdaq Copenhagen",
    "nasdaq first north copenhagen": "Nasdaq Copenhagen",
    "helsinki": "Nasdaq Helsinki",
    "nasdaq helsinki": "Nasdaq Helsinki",
    "omx helsinki": "Nasdaq Helsinki",
    "nasdaq omx helsinki": "Nasdaq Helsinki",
    "xhel": "Nasdaq Helsinki",
    "vienna": "Vienna Stock Exchange",
    "vienna stock exchange": "Vienna Stock Exchange",
    "wiener borse": "Vienna Stock Exchange",
    "wiener börse": "Vienna Stock Exchange",
    "austria": "Vienna Stock Exchange",
    "xwbo": "Vienna Stock Exchange",
    "warsaw": "Warsaw Stock Exchange",
    "warsaw stock exchange": "Warsaw Stock Exchange",
    "gpw": "Warsaw Stock Exchange",
    "poland": "Warsaw Stock Exchange",
    "xwar": "Warsaw Stock Exchange",
    "zagreb": "Zagreb Stock Exchange",
    "zagreb stock exchange": "Zagreb Stock Exchange",
    "croatia": "Zagreb Stock Exchange",
    "xzag": "Zagreb Stock Exchange",
    "ljubljana": "Ljubljana Stock Exchange",
    "ljubljana stock exchange": "Ljubljana Stock Exchange",
    "slovenia": "Ljubljana Stock Exchange",
    "xljubljana": "Ljubljana Stock Exchange",
    "xlju": "Ljubljana Stock Exchange",
    "tallinn": "Tallinn Stock Exchange",
    "xtal": "Tallinn Stock Exchange",
    "tallinn stock exchange": "Tallinn Stock Exchange",
    "riga": "Riga Stock Exchange",
    "xrse": "Riga Stock Exchange",
    "riga stock exchange": "Riga Stock Exchange",
    "latvia": "Riga Stock Exchange",
    "vilnius": "Vilnius Stock Exchange",
    "xlit": "Vilnius Stock Exchange",
    "vilnius stock exchange": "Vilnius Stock Exchange",
    "lithuania": "Vilnius Stock Exchange",
    "bratislava": "Bratislava Stock Exchange",
    "xbsk": "Bratislava Stock Exchange",
    "bratislava stock exchange": "Bratislava Stock Exchange",
    "slovakia": "Bratislava Stock Exchange",
    "bulgaria": "Bulgarian Stock Exchange",
    "xbul": "Bulgarian Stock Exchange",
    "bse sofia": "Bulgarian Stock Exchange",
    "bulgarian stock exchange": "Bulgarian Stock Exchange",
}


@dataclass(frozen=True, slots=True)
class ImportResult:
    issuers: tuple[Issuer, ...]
    rows_read: int
    invalid_rows: int
    duplicate_rows: int


def normalize_market(value: str) -> str:
    normalized = " ".join((value or "").strip().split())
    if not normalized:
        return "Unknown"
    return MARKET_ALIASES.get(normalized.casefold(), normalized)


def is_valid_isin(value: str) -> bool:
    return bool(ISIN_RE.fullmatch((value or "").strip().upper()))


def _read_csv(path: Path, encoding: str) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        if not reader.fieldnames:
            raise ValueError("Le CSV ne contient pas d'en-tête")
        return reader.fieldnames, list(reader)


def load_watchlist(path: str | Path) -> ImportResult:
    csv_path = Path(path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV introuvable: {csv_path}")

    try:
        fieldnames, rows = _read_csv(csv_path, "utf-8-sig")
    except UnicodeDecodeError:
        LOGGER.warning("CSV non UTF-8, nouvel essai en cp1252: %s", csv_path)
        fieldnames, rows = _read_csv(csv_path, "cp1252")

    field_map = {name.strip().casefold(): name for name in fieldnames if name}
    required = ("name", "isin", "symbol", "market")
    missing = [name for name in required if name not in field_map]
    if missing:
        raise ValueError(
            "Colonnes obligatoires absentes: " + ", ".join(sorted(missing))
        )

    issuers_by_isin: dict[str, Issuer] = {}
    invalid_rows = 0
    duplicate_rows = 0

    for row_number, row in enumerate(rows, start=2):
        isin = (row.get(field_map["isin"]) or "").strip().upper()
        if not is_valid_isin(isin):
            invalid_rows += 1
            LOGGER.debug("Ligne %d ignorée: ISIN absent ou invalide", row_number)
            continue

        market = normalize_market(row.get(field_map["market"]) or "")
        issuer_country = (
            (row.get(field_map["issuer_country"]) or "").strip()
            if "issuer_country" in field_map
            else ""
        )
        issuer = Issuer(
            name=(row.get(field_map["name"]) or "").strip(),
            isin=isin,
            symbol=(row.get(field_map["symbol"]) or "").strip(),
            market=market,
            austria_home_member_state=(
                issuer_country
                if market == "Vienna Stock Exchange"
                and issuer_country.casefold() == "austria"
                else None
            ),
            austria_pea_country_check=(
                "eu_candidate"
                if market == "Vienna Stock Exchange"
                else None
            ),
            investor_relations_url=(row.get(field_map.get("investor_relations_url", "")) or "").strip() or None if "investor_relations_url" in field_map else None,
            reports_url=(row.get(field_map.get("reports_url", "")) or "").strip() or None if "reports_url" in field_map else None,
            pea_geography_status=(row.get(field_map.get("pea_geography_status", "")) or "").strip() or None if "pea_geography_status" in field_map else None,
        )
        if isin in issuers_by_isin:
            duplicate_rows += 1
        issuers_by_isin[isin] = issuer

    return ImportResult(
        issuers=tuple(issuers_by_isin.values()),
        rows_read=len(rows),
        invalid_rows=invalid_rows,
        duplicate_rows=duplicate_rows,
    )
