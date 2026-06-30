from __future__ import annotations

import json
import logging
import requests
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config import Settings
from screener.eodhd import EodhdDataProvider, scrub_token

LOGGER = logging.getLogger("infofin.screener.eodhd")

class EodhdMcpProvider(EodhdDataProvider):
    def __init__(self, settings: Settings, token: str | None = None) -> None:
        super().__init__(settings, token)
        self.session_id = None
        self.request_id = 0
        self.mcp_url = f"https://mcpv2.eodhd.dev/v1/mcp?apikey={self._token}"
        self.verify = settings.http_verify_ssl

    def _mcp_request(self, method: str, params: dict[str, Any] | None, notify: bool = False) -> Any:
        self.request_id += 1
        body = {
            "jsonrpc": "2.0",
            "method": method
        }
        if not notify:
            body["id"] = self.request_id
        if params is not None:
            body["params"] = params

        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "france-report-collector/1.0"
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        try:
            LOGGER.debug("Sending MCP request: %s", method)
            response = requests.post(
                self.mcp_url,
                json=body,
                headers=headers,
                timeout=30,
                verify=self.verify
            )
            response.raise_for_status()
        except Exception as exc:
            clean_exc = scrub_token(str(exc), self._token)
            LOGGER.error("MCP request failed: %s", clean_exc)
            raise RuntimeError(f"MCP HTTP request error: {clean_exc}") from exc

        if "Mcp-Session-Id" in response.headers:
            self.session_id = response.headers["Mcp-Session-Id"]
        elif "mcp-session-id" in response.headers:
            self.session_id = response.headers["mcp-session-id"]

        if notify:
            return None

        payload = response.text
        messages = []
        for line in payload.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                if raw:
                    try:
                        message = json.loads(raw)
                        if message.get("id") == self.request_id:
                            messages.append(message)
                    except Exception:
                        pass

        if not messages:
            try:
                res_json = response.json()
                if "error" in res_json:
                    raise RuntimeError(f"MCP JSON response error: {res_json['error']}")
                return res_json.get("result")
            except Exception as exc:
                raise RuntimeError(f"No valid MCP response found. Status: {response.status_code}") from exc

        message = messages[-1]
        if "error" in message:
            raise RuntimeError(f"MCP protocol error: {message['error']}")
        return message.get("result")

    def _ensure_session(self) -> None:
        if self.session_id is not None:
            return
        LOGGER.info("Initializing EODHD MCP session...")
        try:
            self._mcp_request("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "infofin-screener", "version": "1.0"}
            })
            self._mcp_request("notifications/initialized", None, notify=True)
            LOGGER.debug("EODHD MCP session initialized. Session ID: %s", self.session_id)
        except Exception as exc:
            LOGGER.error("Failed to initialize EODHD MCP session: %s", exc)
            raise

    def get_exchange_symbol_list(self, exchange_code: str, force: bool = False) -> list[dict[str, Any]]:
        cache_path = self._get_cache_path(f"exchange_{exchange_code}.json")
        if not force and self._is_cache_valid(cache_path, ttl_days=7):
            data = self._read_cache(cache_path)
            if data is not None:
                return data

        mcp_exchange = exchange_code
        mic_map = {
            "XPAR": "PA",
            "XBRU": "BR",
            "XLIS": "LS",
            "XAMS": "AS",
            "XDUB": "IR",
            "XOSL": "OL",
            "XMIL": "MI",
        }
        if exchange_code.upper() in mic_map:
            mcp_exchange = mic_map[exchange_code.upper()]

        try:
            self._ensure_session()
            LOGGER.info("Fetching exchange symbols for %s via MCP...", mcp_exchange)
            res = self._mcp_request("tools/call", {
                "name": "get_exchange_tickers",
                "arguments": {"exchange_code": mcp_exchange}
            })
            text = res["content"][0]["text"]
            data = json.loads(text)
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(data.get("text") or data.get("error"))
            
            self._write_cache(cache_path, data)
            return data
        except Exception as exc:
            clean_exc = scrub_token(str(exc), self._token)
            LOGGER.warning(
                "Impossible de récupérer la liste des tickers pour %s via MCP, utilisation du cache: %s",
                exchange_code,
                clean_exc,
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
        ticker = f"{symbol}.{exchange}"
        
        try:
            self._ensure_session()
            LOGGER.info("Fetching EOD historical data for %s via MCP...", ticker)
            res = self._mcp_request("tools/call", {
                "name": "get_historical_stock_prices",
                "arguments": {
                    "ticker": ticker,
                    "start_date": from_date,
                    "end_date": to_date
                }
            })
            text = res["content"][0]["text"]
            data = json.loads(text)
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(data.get("text") or data.get("error"))

            self._write_cache(cache_path, data)
            return data
        except Exception as exc:
            clean_exc = scrub_token(str(exc), self._token)
            LOGGER.warning(
                "Impossible de récupérer les cours pour %s via MCP, utilisation du cache: %s",
                ticker,
                clean_exc,
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

        ticker = f"{symbol}.{exchange}"
        try:
            self._ensure_session()
            LOGGER.info("Fetching fundamentals for %s via MCP...", ticker)
            res = self._mcp_request("tools/call", {
                "name": "get_fundamentals_data",
                "arguments": {"ticker": ticker}
            })
            text = res["content"][0]["text"]
            data = json.loads(text)
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(data.get("text") or data.get("error"))

            self._write_cache(cache_path, data)
            return data
        except Exception as exc:
            clean_exc = scrub_token(str(exc), self._token)
            LOGGER.warning(
                "Impossible de récupérer les fondamentaux pour %s via MCP, utilisation du cache: %s",
                ticker,
                clean_exc,
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

        from_date = (as_of_date - timedelta(days=10)).isoformat()
        to_date = as_of_date.isoformat()
        
        ticker = f"{currency}EUR.FOREX"
        try:
            self._ensure_session()
            res = self._mcp_request("tools/call", {
                "name": "get_historical_stock_prices",
                "arguments": {
                    "ticker": ticker,
                    "start_date": from_date,
                    "end_date": to_date
                }
            })
            text = res["content"][0]["text"]
            data = json.loads(text)
            if isinstance(data, list) and data:
                sorted_bars = sorted(data, key=lambda x: x["date"], reverse=True)
                rate = float(sorted_bars[0]["close"])
                self._write_cache(cache_path, {"rate": rate, "is_fallback": False})
                return rate, False
        except Exception:
            ticker_inv = f"EUR{currency}.FOREX"
            try:
                self._ensure_session()
                res = self._mcp_request("tools/call", {
                    "name": "get_historical_stock_prices",
                    "arguments": {
                        "ticker": ticker_inv,
                        "start_date": from_date,
                        "end_date": to_date
                    }
                })
                text = res["content"][0]["text"]
                data = json.loads(text)
                if isinstance(data, list) and data:
                    sorted_bars = sorted(data, key=lambda x: x["date"], reverse=True)
                    inv_rate = float(sorted_bars[0]["close"])
                    if inv_rate > 0:
                        rate = 1.0 / inv_rate
                        self._write_cache(cache_path, {"rate": rate, "is_fallback": False})
                        return rate, False
            except Exception:
                pass

        from screener.eodhd import DEFAULT_FOREX_FALLBACK
        fallback_rate = DEFAULT_FOREX_FALLBACK.get(currency)
        if fallback_rate is not None:
            LOGGER.warning("Forex rate not found for %s/EUR via MCP, using fallback rate: %s", currency, fallback_rate)
            self._write_cache(cache_path, {"rate": fallback_rate, "is_fallback": True})
            return fallback_rate, True

        raise ValueError(f"Taux de change inconnu pour la devise: {currency}")
