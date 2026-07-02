import os
import json
import logging
import re
from datetime import date
from urllib.parse import quote
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
import requests
from db import Database
from models import Issuer

LOGGER = logging.getLogger(__name__)

CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "lei_cache.json"
)

# Background executor for non-blocking resolution in webapp
_RESOLUTION_EXECUTOR = ThreadPoolExecutor(max_workers=1)

def load_lei_cache() -> dict[str, str]:
    """Load the local LEI cache from JSON."""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            LOGGER.error(f"Failed to load LEI cache file {CACHE_FILE}: {e}")
    return {}

def save_lei_cache(cache: dict[str, str]) -> None:
    """Save the local LEI cache to JSON."""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        LOGGER.error(f"Failed to save LEI cache to {CACHE_FILE}: {e}")

def clean_issuer_name(name: str) -> str:
    """Clean company name by removing bond suffixes, legal forms, and noise."""
    name_upper = name.upper().strip()
    
    # 1. Remove bond suffixes (e.g. 25/30, FRN, EUR, FLOOR, etc.)
    name_upper = re.sub(r'\b\d{2}/\d{2}\b.*', '', name_upper)
    name_upper = re.sub(r'\b(FRN|EUR|FLOOR|BOND|DEBT|NOTES|CAPITAL)\b.*', '', name_upper)
    
    # 2. Remove common legal suffixes and suffixes with parentheses
    name_upper = re.sub(r'\(PUB(L|\.)?\)', '', name_upper)
    name_upper = re.sub(r'\b(AB|SA|S\.A\.|LTD|LT|INC|CORP|CO|GMBH|AG|PLC|NV|BV|SGPS|A/S|AS|PUBL)\b', '', name_upper)
    
    # Remove extra spaces
    name_upper = re.sub(r'\s+', ' ', name_upper).strip()
    return name_upper

def calculate_similarity(s1: str, s2: str) -> float:
    """Calculate similarity ratio between two strings (stripping non-alphanumeric)."""
    s1_clean = re.sub(r'[^A-Z0-9]', '', s1.upper())
    s2_clean = re.sub(r'[^A-Z0-9]', '', s2.upper())
    return SequenceMatcher(None, s1_clean, s2_clean).ratio()

def resolve_lei(
    isin: str | None,
    name: str,
    session: requests.Session,
    cache: dict[str, str] | None = None
) -> str | None:
    """
    Resolve LEI for a company using a prioritized approach:
    1. Check local cache (by ISIN or normalized name)
    2. Query GLEIF API by ISIN (most specific)
    3. Query GLEIF API by exact legalName (using cleaned name)
    4. Query GLEIF API by fulltext (using cleaned name)
    5. Tokenized fallback (using first token + similarity score)
    """
    if cache is None:
        cache = load_lei_cache()

    # 1. Check local cache
    isin_key = isin.upper().strip() if isin else None
    name_key = name.upper().strip()
    
    if isin_key and isin_key in cache:
        return cache[isin_key]
    if name_key in cache:
        return cache[name_key]

    # 2. Query GLEIF API by ISIN
    if isin_key:
        try:
            url = f"https://api.gleif.org/api/v1/lei-records?filter[isin]={isin_key}"
            LOGGER.info(f"Querying GLEIF by ISIN: {isin_key}")
            response = session.get(url, timeout=15)
            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data", [])
                if data:
                    lei = data[0].get("id")
                    if lei:
                        LOGGER.info(f"Found LEI via ISIN search: {lei}")
                        cache[isin_key] = lei
                        cache[name_key] = lei
                        save_lei_cache(cache)
                        return lei
        except Exception as e:
            LOGGER.warning(f"GLEIF ISIN search failed for {isin_key}: {e}")

    # Clean name for text search
    clean_name = clean_issuer_name(name)
    if not clean_name:
        clean_name = name.upper().strip()

    # 3. Query GLEIF API by exact legalName (using cleaned name)
    try:
        url = f"https://api.gleif.org/api/v1/lei-records?filter[entity.legalName]={quote(clean_name)}"
        LOGGER.info(f"Querying GLEIF by legalName: {clean_name}")
        response = session.get(url, timeout=15)
        if response.status_code == 200:
            payload = response.json()
            data = payload.get("data", [])
            if data:
                lei = data[0].get("id")
                if lei:
                    LOGGER.info(f"Found LEI via legalName search: {lei}")
                    if isin_key:
                        cache[isin_key] = lei
                    cache[name_key] = lei
                    save_lei_cache(cache)
                    return lei
    except Exception as e:
        LOGGER.warning(f"GLEIF legalName search failed for {clean_name}: {e}")

    # 4. Query GLEIF API full-text search by name (using corrected parameter filter[fulltext])
    try:
        url = f"https://api.gleif.org/api/v1/lei-records?filter[fulltext]={quote(clean_name)}"
        LOGGER.info(f"Querying GLEIF by filter[fulltext]: {clean_name}")
        response = session.get(url, timeout=15)
        if response.status_code == 200:
            payload = response.json()
            data = payload.get("data", [])
            if data:
                lei = data[0].get("id")
                if lei:
                    LOGGER.info(f"Found LEI via full-text search: {lei}")
                    if isin_key:
                        cache[isin_key] = lei
                    cache[name_key] = lei
                    save_lei_cache(cache)
                    return lei
    except Exception as e:
        LOGGER.warning(f"GLEIF fulltext search failed for {clean_name}: {e}")

    # 5. Tokenized fallback (using first token + similarity score)
    try:
        words = clean_name.split()
        if words:
            first_word = words[0]
            if len(first_word) >= 3:
                url = f"https://api.gleif.org/api/v1/lei-records?filter[fulltext]={quote(first_word)}&page[size]=15"
                LOGGER.info(f"Querying GLEIF by fallback first token '{first_word}' for '{clean_name}'")
                response = session.get(url, timeout=15)
                if response.status_code == 200:
                    payload = response.json()
                    data = payload.get("data", [])
                    best_match = None
                    best_score = 0.0
                    
                    for item in data:
                        candidate_name = item.get("attributes", {}).get("entity", {}).get("legalName", {}).get("name", "")
                        candidate_clean = clean_issuer_name(candidate_name)
                        score = calculate_similarity(clean_name, candidate_clean)
                        if score > best_score:
                            best_score = score
                            best_match = (item.get("id"), candidate_name)
                    
                    if best_match and best_score >= 0.5:
                        lei = best_match[0]
                        LOGGER.info(f"Found LEI via tokenized fallback (score {best_score:.2f}): {lei} ({best_match[1]})")
                        if isin_key:
                            cache[isin_key] = lei
                        cache[name_key] = lei
                        save_lei_cache(cache)
                        return lei
    except Exception as e:
        LOGGER.warning(f"GLEIF tokenized fallback search failed for {clean_name}: {e}")

    return None

def sync_database_leis(database: Database, session: requests.Session) -> int:
    """
    Find all issuers in the database that do not have a LEI yet,
    resolve their LEIs via GLEIF and the local cache, and persist them in the database.
    """
    issuers = database.list_issuers()
    cache = load_lei_cache()
    updated_count = 0
    
    issuers_to_update = [i for i in issuers if not getattr(i, "lei", None)]
    
    if not issuers_to_update:
        LOGGER.info("All issuers already have a LEI in the database.")
        return 0
        
    LOGGER.info(f"Attempting to resolve LEIs for {len(issuers_to_update)} issuers...")
    
    with database.connect() as connection:
        for issuer in issuers_to_update:
            lei = resolve_lei(issuer.isin, issuer.name, session, cache)
            if lei:
                connection.execute(
                    "UPDATE issuers SET lei = ?, updated_at = ? WHERE isin = ?",
                    (lei, date.today().isoformat(), issuer.isin)
                )
                updated_count += 1
                LOGGER.info(f"Updated LEI in DB for {issuer.name} -> {lei}")
                
    LOGGER.info(f"Successfully resolved and updated {updated_count} LEIs in the database.")
    return updated_count

def queue_background_resolution(database: Database, isin: str | None, name: str) -> None:
    """Submit a background LEI resolution task to prevent blocking the search link collection."""
    _RESOLUTION_EXECUTOR.submit(_resolve_and_update, database, isin, name)

def _resolve_and_update(database: Database, isin: str | None, name: str) -> None:
    """Background worker method to query GLEIF, update issuers, and update search results."""
    try:
        with requests.Session() as session:
            lei = resolve_lei(isin, name, session)
            if lei:
                with database.connect() as connection:
                    # 1. Update the central issuers table
                    if isin:
                        try:
                            connection.execute(
                                "UPDATE issuers SET lei = ?, updated_at = ? WHERE isin = ?",
                                (lei, date.today().isoformat(), isin)
                            )
                        except Exception:
                            pass
                    
                    # 2. Update the web_search_results table for any matching rows
                    if isin:
                        connection.execute(
                            "UPDATE web_search_results SET issuer_lei = ? WHERE issuer_isin = ?",
                            (lei, isin)
                        )
                    else:
                        connection.execute(
                            "UPDATE web_search_results SET issuer_lei = ? WHERE issuer_name = ? COLLATE NOCASE",
                            (lei, name)
                        )
    except Exception as e:
        LOGGER.error(f"Asynchronous background LEI resolution failed for name='{name}': {e}")
