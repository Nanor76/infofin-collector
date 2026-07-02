import os
import json
import logging
from datetime import date
from urllib.parse import quote
import requests
from db import Database
from models import Issuer

LOGGER = logging.getLogger(__name__)

CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "lei_cache.json"
)

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
    3. Query GLEIF API by exact legalName
    4. Query GLEIF API full-text search by name
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

    # 3. Query GLEIF API by exact legalName
    try:
        url = f"https://api.gleif.org/api/v1/lei-records?filter[entity.legalName]={quote(name)}"
        LOGGER.info(f"Querying GLEIF by legalName: {name}")
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
        LOGGER.warning(f"GLEIF legalName search failed for {name}: {e}")

    # 4. Query GLEIF API full-text search by name (fallback/fuzzy)
    try:
        url = f"https://api.gleif.org/api/v1/lei-records?q={quote(name)}"
        LOGGER.info(f"Querying GLEIF by full-text name search: {name}")
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
        LOGGER.warning(f"GLEIF full-text search failed for {name}: {e}")

    return None

def sync_database_leis(database: Database, session: requests.Session) -> int:
    """
    Find all issuers in the database that do not have a LEI yet,
    resolve their LEIs via GLEIF and the local cache, and persist them in the database.
    """
    issuers = database.list_issuers()
    cache = load_lei_cache()
    updated_count = 0
    
    # We query the database directly to find issuers with NULL or empty LEI
    # database.list_issuers() loads them into memory
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
