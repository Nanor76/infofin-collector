from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import requests
from config import Settings
from http_client import build_http_session

LOGGER = logging.getLogger("infofin.screener.eodhd")

DEFAULT_FOREX_FALLBACK = {
    "NOK": 0.086,
    "USD": 0.92,
    "GBP": 1.18,
    "DKK": 0.134,
    "SEK": 0.088,
    "CHF": 1.03,
    "EUR": 1.0,
}

def load_eodhd_token() -> str:
    path = Path(r"C:\Users\jegour\OneDrive - CEGID\Documents\EODHD_token.txt")
    if not path.exists():
        raise FileNotFoundError(f"Le fichier EODHD token est introuvable à l'emplacement: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"Le fichier EODHD token est vide: {path}")
    return token

def scrub_token(text: str, token: str) -> str:
    if not token or not text:
        return text
    return text.replace(token, "[REDACTED_TOKEN]")

class EodhdDataProvider(ABC):
    def __init__(self, settings: Settings, token: str | None = None) -> None:
        self.settings = settings
        self._token = token or load_eodhd_token()
        
        # Setup logging redactor immediately to protect all logs
        from screener.logging_utils import setup_logging_redactor
        setup_logging_redactor(self._token)

        self.cache_dir = settings.data_dir.parent / "cache" / "eodhd"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, filename: str) -> Path:
        return self.cache_dir / filename

    def _is_cache_valid(self, path: Path, ttl_days: int) -> bool:
        if not path.exists():
            return False
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return (datetime.now() - mtime) < timedelta(days=ttl_days)

    def _read_cache(self, path: Path) -> Any | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Erreur lecture cache %s: %s", path, exc)
            return None

    def _write_cache(self, path: Path, data: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("Erreur écriture cache %s: %s", path, exc)

    @abstractmethod
    def get_exchange_symbol_list(self, exchange_code: str, force: bool = False) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def get_eod_historical_data(
        self,
        symbol: str,
        exchange: str,
        as_of_date: date,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def get_fundamentals(self, symbol: str, exchange: str, force: bool = False) -> dict[str, Any]:
        pass

    @abstractmethod
    def get_forex_rate(
        self,
        currency: str,
        as_of_date: date,
        force: bool = False,
    ) -> tuple[float, bool]:
        pass

class EodhdRestProvider(EodhdDataProvider):
    def __init__(self, settings: Settings, token: str | None = None) -> None:
        super().__init__(settings, token)
        self.session = build_http_session(
            retries=settings.http_retries,
            backoff_factor=settings.http_backoff_factor,
            user_agent=settings.user_agent,
            verify=settings.http_verify_ssl,
        )

    def _request(self, url: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        params["api_token"] = self._token
        params["fmt"] = "json"
        
        logged_url = url
        if "?" in url:
            logged_url = url.split("?")[0]
        LOGGER.debug("Requesting EODHD API: %s", logged_url)

        try:
            response = self.session.get(url, params=params, timeout=self.settings.http_timeout_seconds)
            if response.status_code == 429:
                LOGGER.error("EODHD rate limit (429) hit.")
                raise requests.HTTPError("EODHD 429 Rate Limit Exceeded", response=response)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.SSLError as exc:
            err_msg = scrub_token(str(exc), self._token)
            
            is_blocked = False
            try:
                import urllib3
                with urllib3.warnings.catch_warnings():
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    test_resp = requests.get("https://eodhd.com", verify=False, timeout=2)
                    if "block.opendns.com" in test_resp.text or "opendns" in test_resp.text:
                        is_blocked = True
            except Exception:
                pass

            if is_blocked:
                friendly_msg = (
                    "ERREUR SECURITE RESEAU : La connexion à eodhd.com est bloquée et interceptée "
                    "par votre filtre de sécurité réseau (Cisco Umbrella / OpenDNS). "
                    "Veuillez vérifier vos règles de filtrage ou utiliser un VPN / autre connexion."
                )
                LOGGER.error(friendly_msg)
                raise requests.exceptions.SSLError(friendly_msg) from None
            else:
                LOGGER.error("EODHD SSL Error: %s", err_msg)
                raise requests.exceptions.SSLError(err_msg) from None
        except requests.RequestException as exc:
            err_msg = scrub_token(str(exc), self._token)
            LOGGER.error("EODHD HTTP Request Error: %s", err_msg)
            raise requests.RequestException(err_msg) from None
        except ValueError as exc:
            LOGGER.error("EODHD Invalid JSON Response")
            raise ValueError("Invalid JSON response from EODHD API") from None

    def get_exchange_symbol_list(self, exchange_code: str, force: bool = False) -> list[dict[str, Any]]:
        cache_path = self._get_cache_path(f"exchange_{exchange_code}.json")
        if not force and self._is_cache_valid(cache_path, ttl_days=7):
            data = self._read_cache(cache_path)
            if data is not None:
                return data

        url = f"https://eodhd.com/api/exchange-symbol-list/{exchange_code}"
        try:
            data = self._request(url)
            self._write_cache(cache_path, data)
            return data
        except Exception as exc:
            clean_exc_str = scrub_token(str(exc), self._token)
            LOGGER.warning(
                "Impossible de récupérer la liste des tickers pour %s, utilisation du cache: %s",
                exchange_code,
                clean_exc_str,
            )
            if cache_path.exists():
                data = self._read_cache(cache_path)
                if data is not None:
                    return data
            raise

    def get_eod_historical_data(
        self,
        symbol: str,
        exchange: str,
        as_of_date: date,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        cache_path = self._get_cache_path(f"eod_{symbol}_{exchange}.json")
        if not force and self._is_cache_valid(cache_path, ttl_days=1):
            data = self._read_cache(cache_path)
            if data is not None:
                return data

        from_date = (as_of_date - timedelta(days=500)).isoformat()
        to_date = as_of_date.isoformat()
        url = f"https://eodhd.com/api/eod/{symbol}.{exchange}"
        params = {
            "from": from_date,
            "to": to_date,
            "period": "d",
        }
        try:
            data = self._request(url, params=params)
            self._write_cache(cache_path, data)
            return data
        except Exception as exc:
            clean_exc_str = scrub_token(str(exc), self._token)
            LOGGER.warning(
                "Impossible de récupérer les cours pour %s.%s, utilisation du cache: %s",
                symbol,
                exchange,
                clean_exc_str,
            )
            if cache_path.exists():
                data = self._read_cache(cache_path)
                if data is not None:
                    return data
            raise

    def get_fundamentals(self, symbol: str, exchange: str, force: bool = False) -> dict[str, Any]:
        cache_path = self._get_cache_path(f"fundamentals_{symbol}_{exchange}.json")
        if not force and self._is_cache_valid(cache_path, ttl_days=7):
            data = self._read_cache(cache_path)
            if data is not None:
                return data

        url = f"https://eodhd.com/api/fundamentals/{symbol}.{exchange}"
        try:
            data = self._request(url)
            self._write_cache(cache_path, data)
            return data
        except Exception as exc:
            clean_exc_str = scrub_token(str(exc), self._token)
            LOGGER.warning(
                "Impossible de récupérer les fondamentaux pour %s.%s, utilisation du cache: %s",
                symbol,
                exchange,
                clean_exc_str,
            )
            if cache_path.exists():
                data = self._read_cache(cache_path)
                if data is not None:
                    return data
            raise

    def get_forex_rate(
        self,
        currency: str,
        as_of_date: date,
        force: bool = False,
    ) -> tuple[float, bool]:
        currency = currency.upper()
        if currency == "EUR":
            return 1.0, False

        cache_path = self._get_cache_path(f"forex_{currency}_EUR.json")
        
        if not force and self._is_cache_valid(cache_path, ttl_days=1):
            data = self._read_cache(cache_path)
            if data is not None:
                rate = data.get("rate")
                is_fallback = data.get("is_fallback", False)
                return rate, is_fallback

        url = f"https://eodhd.com/api/eod/{currency}EUR.FOREX"
        from_date = (as_of_date - timedelta(days=10)).isoformat()
        params = {"from": from_date, "to": as_of_date.isoformat(), "period": "d"}

        try:
            data = self._request(url, params=params)
            if data and isinstance(data, list):
                sorted_bars = sorted(data, key=lambda x: x["date"], reverse=True)
                rate = float(sorted_bars[0]["close"])
                self._write_cache(cache_path, {"rate": rate, "is_fallback": False})
                return rate, False
        except Exception:
            url_inv = f"https://eodhd.com/api/eod/EUR{currency}.FOREX"
            try:
                data = self._request(url_inv, params=params)
                if data and isinstance(data, list):
                    sorted_bars = sorted(data, key=lambda x: x["date"], reverse=True)
                    inv_rate = float(sorted_bars[0]["close"])
                    if inv_rate > 0:
                        rate = 1.0 / inv_rate
                        self._write_cache(cache_path, {"rate": rate, "is_fallback": False})
                        return rate, False
            except Exception:
                pass

        fallback_rate = DEFAULT_FOREX_FALLBACK.get(currency)
        if fallback_rate is not None:
            LOGGER.warning("Forex rate not found for %s/EUR, using fallback rate: %s", currency, fallback_rate)
            self._write_cache(cache_path, {"rate": fallback_rate, "is_fallback": True})
            return fallback_rate, True

        raise ValueError(f"Taux de change inconnu pour la devise: {currency}")

class EodHdClient:
    def __init__(self, settings: Settings, token: str | None = None, backend: str = "auto") -> None:
        self.settings = settings
        self._token = token or load_eodhd_token()
        
        # Setup logging redactor immediately to protect all logs
        from screener.logging_utils import setup_logging_redactor
        setup_logging_redactor(self._token)

        self.backend = backend.lower()
        if self.backend == "auto":
            is_blocked = False
            try:
                import urllib3
                with urllib3.warnings.catch_warnings():
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    test_resp = requests.get("https://eodhd.com", verify=False, timeout=2.0)
                    if "block.opendns.com" in test_resp.text or "opendns" in test_resp.text:
                        is_blocked = True
            except Exception:
                pass

            if is_blocked:
                LOGGER.warning("REST EODHD bloqué par OpenDNS/Cisco Umbrella, bascule vers MCP")
                from screener.eodhd_mcp import EodhdMcpProvider
                self.provider = EodhdMcpProvider(settings, self._token)
                self.backend = "mcp"
            else:
                self.provider = EodhdRestProvider(settings, self._token)
                self.backend = "rest"
        elif self.backend == "mcp":
            from screener.eodhd_mcp import EodhdMcpProvider
            self.provider = EodhdMcpProvider(settings, self._token)
        elif self.backend == "rest":
            self.provider = EodhdRestProvider(settings, self._token)
        else:
            raise ValueError(f"Backend EODHD inconnu: {backend}")

    def get_exchange_symbol_list(self, exchange_code: str, force: bool = False) -> list[dict[str, Any]]:
        return self.provider.get_exchange_symbol_list(exchange_code, force=force)

    def get_eod_historical_data(
        self,
        symbol: str,
        exchange: str,
        as_of_date: date,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        return self.provider.get_eod_historical_data(symbol, exchange, as_of_date, force=force)

    def get_fundamentals(self, symbol: str, exchange: str, force: bool = False) -> dict[str, Any]:
        return self.provider.get_fundamentals(symbol, exchange, force=force)

    def get_forex_rate(
        self,
        currency: str,
        as_of_date: date,
        force: bool = False,
    ) -> tuple[float, bool]:
        return self.provider.get_forex_rate(currency, as_of_date, force=force)

    @property
    def session(self) -> Any:
        if hasattr(self.provider, "session"):
            return self.provider.session
        return None
