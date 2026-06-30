from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests

from classification import classify_document
from connectors.base import (
    Connector,
    ConnectorState,
    DatasetCandidate,
    DocumentCandidate,
    EndpointAttempt,
    SourceDiagnostic,
    SourceDiscovery,
)
from load_watchlist import ISIN_RE
from models import Issuer

LOGGER = logging.getLogger(__name__)

DOCUMENT_SEARCH_TERMS = (
    "rapport financier annuel",
    "annual financial report",
    "rapport financier semestriel",
    "half-year financial report",
    "document d'enregistrement universel",
    "universal registration document",
    "ESEF",
)


def normalize_field_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", "_", ascii_value.casefold()).strip("_")


def detect_field_role(field_name: str) -> str | None:
    name = normalize_field_name(field_name)
    tokens = set(name.split("_"))

    if "isin" in tokens or name.endswith("isin") or "cd_isi" in name:
        return "isin"
    if (
        tokens & {"societe", "company", "issuer", "emetteur"}
        or {"nom", "soc"} <= tokens
    ):
        return "company"
    if tokens & {"ticker", "symbol", "symbole"} or "cd_tkr" in name:
        return "symbol"
    if (
        "url" in tokens
        or "lien" in tokens
        or "href" in tokens
        or "download" in tokens
    ) and (
        len(tokens) == 1
        or tokens
        & {
            "recuperation",
            "document",
            "fichier",
            "file",
            "download",
            "piece",
            "jointe",
        }
    ):
        return "url"
    if (
        ("sous" in tokens and "type" in tokens)
        or "subtype" in tokens
        or name.startswith("sous_type")
    ) and tokens & {"information", "info", "document"}:
        return "information_subtype"
    if "type" in tokens and tokens & {"information", "info", "document"}:
        return "information_type"
    if (
        tokens & {"titre", "title", "filename"}
        or {"tit", "inf"} <= tokens
        or (tokens & {"nom", "libelle"} and tokens & {"fichier", "document"})
    ):
        return "title"
    if (
        "date" in tokens
        or "dat" in tokens
        or name.startswith("date")
        or name.endswith("_date")
    ):
        return "date"
    if (
        name in {"recordid", "record_id", "id", "numero"}
        or {"uin", "idt"} <= tokens
    ):
        return "source_id"
    return None


class FranceInfoFinanciereConnector(Connector):
    market = "Euronext Paris"
    source_name = "amf_info_financiere_opendatasoft"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str,
        dataset: str,
        fallback_base_urls: tuple[str, ...] = (),
        rows: int = 100,
        timeout: int = 30,
    ) -> None:
        self.session = session
        self.configured_base_url = base_url.rstrip("/")
        self.dataset = dataset.strip()
        self.rows = min(max(rows, 1), 100)
        self.timeout = timeout
        self.state = ConnectorState.READY
        self.last_error = None
        self.base_urls = self._base_urls(base_url, fallback_base_urls)

    @staticmethod
    def _portal_root(value: str) -> str:
        parsed = urlparse(value.strip())
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"URL de base AMF invalide: {value!r}")
        return f"{parsed.scheme}://{parsed.netloc}"

    @classmethod
    def _base_urls(
        cls,
        base_url: str,
        fallback_base_urls: tuple[str, ...],
    ) -> tuple[str, ...]:
        roots: list[str] = []
        for value in (base_url, *fallback_base_urls):
            root = cls._portal_root(value)
            if root not in roots:
                roots.append(root)
        return tuple(roots)

    def _endpoints(self, base_url: str) -> dict[str, str]:
        encoded = quote(self.dataset, safe="")
        return {
            "explore_v2_records": (
                f"{base_url}/api/explore/v2.1/catalog/datasets/"
                f"{encoded}/records"
            ),
            "explore_v2_export_json": (
                f"{base_url}/api/explore/v2.1/catalog/datasets/"
                f"{encoded}/exports/json"
            ),
            "records_v1_search": (
                f"{base_url}/api/records/1.0/search/"
            ),
            "catalog_search": (
                f"{base_url}/api/explore/v2.1/catalog/datasets"
            ),
        }

    @staticmethod
    def _full_url(url: str, params: dict[str, Any] | None) -> str:
        return requests.Request("GET", url, params=params).prepare().url or url

    @staticmethod
    def _excerpt(response: Any, limit: int = 600) -> str | None:
        try:
            text = response.text
        except Exception:
            return None
        compact = re.sub(r"\s+", " ", text).strip()
        return compact[:limit] or None

    @staticmethod
    def _close(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    def _request(
        self,
        *,
        name: str,
        base_url: str,
        url: str,
        params: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> tuple[EndpointAttempt, Any | None]:
        endpoint = self._full_url(url, params)
        response: Any | None = None
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout,
                stream=stream,
            )
        except requests.RequestException as exc:
            attempt = EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=self.dataset,
                endpoint=endpoint,
                method="GET",
                http_status=None,
                success=False,
                error=f"réseau: {exc}",
            )
            LOGGER.error("%s: %s", endpoint, attempt.error)
            return attempt, None

        status = int(response.status_code)
        if status >= 400:
            attempt = EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=self.dataset,
                endpoint=endpoint,
                method="GET",
                http_status=status,
                success=False,
                response_excerpt=self._excerpt(response),
                error=f"HTTP {status}",
            )
            LOGGER.error(
                "%s: %s; réponse=%r",
                endpoint,
                attempt.error,
                attempt.response_excerpt,
            )
            self._close(response)
            return attempt, None

        if stream:
            self._close(response)
            return (
                EndpointAttempt(
                    name=name,
                    base_url=base_url,
                    dataset=self.dataset,
                    endpoint=endpoint,
                    method="GET",
                    http_status=status,
                    success=True,
                ),
                None,
            )

        try:
            payload = response.json()
        except (requests.exceptions.JSONDecodeError, json.JSONDecodeError, ValueError) as exc:
            attempt = EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=self.dataset,
                endpoint=endpoint,
                method="GET",
                http_status=status,
                success=False,
                response_excerpt=self._excerpt(response),
                error=f"parsing JSON: {exc}",
            )
            LOGGER.error(
                "%s: %s; réponse=%r",
                endpoint,
                attempt.error,
                attempt.response_excerpt,
            )
            self._close(response)
            return attempt, None
        self._close(response)
        return (
            EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=self.dataset,
                endpoint=endpoint,
                method="GET",
                http_status=status,
                success=True,
            ),
            payload,
        )

    def _records_v2(
        self,
        base_url: str,
        *,
        params: dict[str, Any],
    ) -> tuple[EndpointAttempt, int | None, list[dict[str, Any]]]:
        endpoint = self._endpoints(base_url)["explore_v2_records"]
        attempt, payload = self._request(
            name="explore_v2_records",
            base_url=base_url,
            url=endpoint,
            params=params,
        )
        if payload is None:
            return attempt, None, []
        if not isinstance(payload, dict):
            return self._parsing_failure(attempt, "la réponse JSON n'est pas un objet")
        records = payload.get("results")
        total_count = payload.get("total_count")
        if (
            not isinstance(total_count, int)
            or not isinstance(records, list)
            or any(not isinstance(record, dict) for record in records)
        ):
            return self._parsing_failure(
                attempt,
                "champs 'total_count' ou 'results' absents ou invalides",
            )
        return replace(attempt, total_count=total_count), total_count, records

    def _records_v1(
        self,
        base_url: str,
        *,
        params: dict[str, Any],
    ) -> tuple[EndpointAttempt, int | None, list[dict[str, Any]]]:
        endpoint = self._endpoints(base_url)["records_v1_search"]
        attempt, payload = self._request(
            name="records_v1_search",
            base_url=base_url,
            url=endpoint,
            params={"dataset": self.dataset, **params},
        )
        if payload is None:
            return attempt, None, []
        if not isinstance(payload, dict):
            return self._parsing_failure(attempt, "la réponse JSON n'est pas un objet")
        raw_records = payload.get("records")
        total_count = payload.get("nhits")
        if (
            not isinstance(total_count, int)
            or not isinstance(raw_records, list)
            or any(not isinstance(record, dict) for record in raw_records)
        ):
            return self._parsing_failure(
                attempt,
                "champs 'nhits' ou 'records' absents ou invalides",
            )
        records: list[dict[str, Any]] = []
        for record in raw_records:
            fields = record.get("fields")
            if not isinstance(fields, dict):
                return self._parsing_failure(
                    attempt,
                    "champ 'fields' v1 absent ou invalide",
                )
            if "recordid" in record:
                fields = {**fields, "recordid": record["recordid"]}
            records.append(fields)
        return replace(attempt, total_count=total_count), total_count, records

    @staticmethod
    def _parsing_failure(
        attempt: EndpointAttempt,
        message: str,
    ) -> tuple[EndpointAttempt, None, list[dict[str, Any]]]:
        failed = EndpointAttempt(
            name=attempt.name,
            base_url=attempt.base_url,
            dataset=attempt.dataset,
            endpoint=attempt.endpoint,
            method=attempt.method,
            http_status=attempt.http_status,
            success=False,
            response_excerpt=attempt.response_excerpt,
            error=f"parsing: {message}",
        )
        LOGGER.error("%s: %s", failed.endpoint, failed.error)
        return failed, None, []

    def diagnose(self) -> SourceDiagnostic:
        attempts: list[EndpointAttempt] = []
        selected: tuple[str, int, list[dict[str, Any]]] | None = None

        for base_url in self.base_urls:
            endpoints = self._endpoints(base_url)
            records_attempt, total_count, records = self._records_v2(
                base_url,
                params={"limit": min(self.rows, 10)},
            )
            attempts.append(records_attempt)
            if total_count is not None and records and selected is None:
                selected = (records_attempt.endpoint, total_count, records)

            export_attempt, _ = self._request(
                name="explore_v2_export_json",
                base_url=base_url,
                url=endpoints["explore_v2_export_json"],
                stream=True,
            )
            attempts.append(export_attempt)

            v1_attempt, v1_total, v1_records = self._records_v1(
                base_url,
                params={"rows": min(self.rows, 10)},
            )
            attempts.append(v1_attempt)
            if v1_total is not None and v1_records and selected is None:
                selected = (v1_attempt.endpoint, v1_total, v1_records)

            catalog_attempt, _ = self._catalog(base_url, "flux-amf")
            attempts.append(catalog_attempt)

        if selected is None:
            errors = "; ".join(
                f"{attempt.name}: {attempt.error or 'aucun record'}"
                for attempt in attempts
                if not attempt.success or attempt.total_count == 0
            )
            self._degrade(errors or "aucun enregistrement réel retourné")
            return SourceDiagnostic(
                source=self.source_name,
                state=self.state,
                base_url=self.configured_base_url,
                dataset=self.dataset,
                selected_endpoint=None,
                total_count=None,
                fields=(),
                example_record=None,
                attempts=tuple(attempts),
                error=self.last_error,
            )

        selected_endpoint, total_count, records = selected
        fields = tuple(
            sorted({str(key) for record in records for key in record})
        )
        self.state = ConnectorState.READY
        self.last_error = None
        return SourceDiagnostic(
            source=self.source_name,
            state=self.state,
            base_url=self.configured_base_url,
            dataset=self.dataset,
            selected_endpoint=selected_endpoint,
            total_count=total_count,
            fields=fields,
            example_record=records[0],
            attempts=tuple(attempts),
        )

    def _catalog(
        self,
        base_url: str,
        query: str,
    ) -> tuple[EndpointAttempt, list[DatasetCandidate]]:
        endpoint = self._endpoints(base_url)["catalog_search"]
        attempt, payload = self._request(
            name="catalog_search",
            base_url=base_url,
            url=endpoint,
            params={"limit": 100, "where": f'search("{query}")'},
        )
        if payload is None:
            return attempt, []
        if not isinstance(payload, dict) or not isinstance(
            payload.get("results"), list
        ):
            failed, _, _ = self._parsing_failure(
                attempt,
                "tableau catalogue 'results' absent ou invalide",
            )
            return failed, []

        candidates: list[DatasetCandidate] = []
        for item in payload["results"]:
            if not isinstance(item, dict):
                continue
            dataset_id = item.get("dataset_id")
            if not isinstance(dataset_id, str) or not dataset_id:
                continue
            metas = item.get("metas")
            default_meta = (
                metas.get("default")
                if isinstance(metas, dict)
                and isinstance(metas.get("default"), dict)
                else {}
            )
            title = default_meta.get("title")
            records_count = item.get("records_count")
            if not isinstance(records_count, int):
                records_count = default_meta.get("records_count")
            candidates.append(
                DatasetCandidate(
                    dataset_id=dataset_id,
                    title=title if isinstance(title, str) else dataset_id,
                    records_count=(
                        records_count if isinstance(records_count, int) else None
                    ),
                    base_url=base_url,
                )
            )
        total_count = payload.get("total_count")
        if not isinstance(total_count, int):
            total_count = len(candidates)
        return replace(attempt, total_count=total_count), candidates

    def discover(self, query: str) -> SourceDiscovery:
        attempts: list[EndpointAttempt] = []
        candidates: list[DatasetCandidate] = []
        seen: set[tuple[str, str]] = set()
        for base_url in self.base_urls:
            attempt, found = self._catalog(base_url, query)
            attempts.append(attempt)
            for candidate in found:
                key = (candidate.base_url, candidate.dataset_id)
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)
        return SourceDiscovery(
            source=self.source_name,
            query=query,
            candidates=tuple(candidates),
            attempts=tuple(attempts),
        )

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        if issuer.market.casefold() != self.market.casefold():
            return []

        attempts: list[EndpointAttempt] = []
        records: list[dict[str, Any]] = []
        any_success = False
        rows_per_term = min(self.rows, 25)
        for base_url in self.base_urls:
            base_records: list[dict[str, Any]] = []
            v2_working = True
            for term in DOCUMENT_SEARCH_TERMS:
                attempt, _, found = self._records_v2(
                    base_url,
                    params={
                        "limit": rows_per_term,
                        "where": (
                            f'search("{issuer.isin}") AND search("{term}")'
                        ),
                    },
                )
                attempts.append(attempt)
                if not attempt.success:
                    v2_working = False
                    break
                any_success = True
                base_records.extend(found)
            if base_records:
                records = self._unique_records(base_records)
                break
            if v2_working:
                break

            base_records = []
            v1_working = True
            for term in DOCUMENT_SEARCH_TERMS:
                attempt, _, found = self._records_v1(
                    base_url,
                    params={
                        "rows": rows_per_term,
                        "q": f'{issuer.isin} "{term}"',
                    },
                )
                attempts.append(attempt)
                if not attempt.success:
                    v1_working = False
                    break
                any_success = True
                base_records.extend(found)
            if base_records:
                records = self._unique_records(base_records)
                break
            if v1_working:
                break

        if not records:
            if any_success:
                self.state = ConnectorState.READY
                self.last_error = None
                return []
            errors = "; ".join(
                f"{attempt.endpoint}: {attempt.error or 'aucun record'}"
                for attempt in attempts
            )
            self._degrade(errors)
            return []

        self.state = ConnectorState.READY
        self.last_error = None
        candidates: list[DocumentCandidate] = []
        seen_urls: set[str] = set()
        for record in records:
            candidates.extend(self._parse_record(record, issuer, seen_urls))
        return candidates

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != self.market.casefold():
            return []
        cutoff = since or (date.today() - timedelta(days=7))
        candidate_limit = max(1, limit or 1000)
        page_size = min(self.rows, 100, candidate_limit)
        date_field = "informationdeposee_inf_dat_emt"
        records: list[dict[str, Any]] = []
        attempts: list[EndpointAttempt] = []

        for base_url in self.base_urls:
            offset = 0
            while len(records) < candidate_limit:
                attempt, _, page = self._records_v2(
                    base_url,
                    params={
                        "limit": page_size,
                        "offset": offset,
                        "where": (
                            f"{date_field} >= date'{cutoff.isoformat()}'"
                        ),
                        "order_by": f"{date_field} DESC",
                    },
                )
                attempts.append(attempt)
                if not attempt.success:
                    records = []
                    break
                records.extend(page)
                if len(page) < page_size:
                    break
                offset += len(page)
            if records or (attempts and attempts[-1].success):
                break

            start = 0
            while len(records) < candidate_limit:
                attempt, _, page = self._records_v1(
                    base_url,
                    params={
                        "rows": page_size,
                        "start": start,
                        "sort": f"-{date_field}",
                    },
                )
                attempts.append(attempt)
                if not attempt.success:
                    records = []
                    break
                recent_page = []
                for record in page:
                    values = self._values_by_role(record)
                    published = self._extract_date(values.get("date", []))
                    if published is None or published >= cutoff:
                        recent_page.append(record)
                records.extend(recent_page)
                if len(page) < page_size:
                    break
                if recent_page and len(recent_page) < len(page):
                    break
                start += len(page)
            if records or (attempts and attempts[-1].success):
                break

        if not any(attempt.success for attempt in attempts):
            self._degrade(
                "; ".join(
                    attempt.error or attempt.endpoint for attempt in attempts
                )
            )
            return []

        self.state = ConnectorState.READY
        self.last_error = None
        self._scanned_notices = len(records)
        seen_urls: set[str] = set()
        candidates: list[DocumentCandidate] = []
        for record in records:
            candidates.extend(self._parse_record(record, None, seen_urls))
            if len(candidates) >= candidate_limit:
                break
        return sorted(
            candidates[:candidate_limit],
            key=lambda candidate: (
                candidate.published_date or date.min,
                candidate.title.casefold(),
                candidate.url,
            ),
            reverse=True,
        )

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        candidate_limit = max(1, limit or 1000)
        page_size = min(self.rows, 100)
        pages = max(1, (candidate_limit + page_size - 1) // page_size)
        return pages * 2 * len(self.base_urls)

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return len(DOCUMENT_SEARCH_TERMS) * 2 * len(self.base_urls)

    @staticmethod
    def _unique_records(
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for record in records:
            key = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            unique.setdefault(key, record)
        return list(unique.values())

    def _degrade(self, message: str) -> None:
        error = f"Source France en état degraded: {message}"
        self.mark_degraded(error)
        LOGGER.error("%s", error)

    def _parse_record(
        self,
        fields: dict[str, Any],
        issuer: Issuer | None,
        seen_urls: set[str],
    ) -> list[DocumentCandidate]:
        role_values = self._values_by_role(fields)
        record_text = json.dumps(fields, ensure_ascii=False, default=str)
        explicit_isins = self._extract_isins(role_values.get("isin", []))
        record_isins = explicit_isins or self._extract_isins([record_text])
        if issuer is not None and record_isins and issuer.isin not in record_isins:
            return []

        title_parts: list[str] = []
        for role in ("title", "information_type", "information_subtype"):
            for value in role_values.get(role, []):
                part = self._first_text([value])
                if part and part not in title_parts:
                    title_parts.append(part)
        base_title = " - ".join(title_parts)
        published_date = self._extract_date(role_values.get("date", []))
        source_id = self._first_text(role_values.get("source_id", []))
        company = self._first_text(role_values.get("company", []))
        symbol = self._first_text(role_values.get("symbol", []))
        results: list[DocumentCandidate] = []

        for index, (url, filename) in enumerate(
            self._extract_links(fields, role_values),
            start=1,
        ):
            absolute_url = urljoin(f"{self.base_urls[0]}/", url)
            if absolute_url in seen_urls:
                continue
            title = base_title or filename or PurePosixPath(urlparse(url).path).name
            document_type = classify_document(title, absolute_url)
            if not document_type:
                continue
            seen_urls.add(absolute_url)
            results.append(
                DocumentCandidate(
                    title=title or "Document financier",
                    url=absolute_url,
                    published_date=published_date,
                    document_type=document_type,
                    source=self.source_name,
                    source_document_id=(
                        f"{source_id}:{index}" if source_id else None
                    ),
                    metadata={
                        "dataset": self.dataset,
                        "company": company,
                        "issuer_name": company,
                        "issuer_isins": sorted(record_isins),
                        "issuer_symbol": symbol,
                    },
                )
            )
        return results

    @staticmethod
    def _values_by_role(fields: dict[str, Any]) -> dict[str, list[Any]]:
        values: dict[str, list[Any]] = {}
        for field_name, value in fields.items():
            role = detect_field_role(str(field_name))
            if role:
                values.setdefault(role, []).append(value)
        return values

    def _extract_links(
        self,
        fields: dict[str, Any],
        role_values: dict[str, list[Any]],
    ) -> list[tuple[str, str | None]]:
        links: list[tuple[str, str | None]] = []
        preferred_values = role_values.get("url", [])

        def visit(node: Any, *, preferred: bool = False) -> None:
            if isinstance(node, dict):
                filename = self._filename_from_mapping(node)
                attachment_id = self._attachment_id(node)
                if attachment_id and filename:
                    links.append(
                        (
                            f"/explore/dataset/{self.dataset}/files/"
                            f"{attachment_id}/download/",
                            filename,
                        )
                    )
                for child in node.values():
                    visit(child, preferred=preferred)
                return
            if isinstance(node, list):
                for child in node:
                    visit(child, preferred=preferred)
                return
            if not isinstance(node, str):
                return
            text = node.strip()
            if not text.startswith(("http://", "https://", "/")):
                return
            path = urlparse(text).path.casefold()
            if (
                preferred
                or path.endswith((".pdf", ".xhtml", ".xht", ".zip"))
                or "/download" in path
            ):
                links.append(
                    (text, PurePosixPath(urlparse(text).path).name or None)
                )

        for value in preferred_values:
            visit(value, preferred=True)
        visit(fields)

        unique: dict[str, str | None] = {}
        for url, filename in links:
            unique.setdefault(url, filename)
        return list(unique.items())

    @staticmethod
    def _filename_from_mapping(mapping: dict[str, Any]) -> str | None:
        for key, value in mapping.items():
            if detect_field_role(str(key)) == "title" and isinstance(value, str):
                return value.strip() or None
        return None

    @staticmethod
    def _attachment_id(mapping: dict[str, Any]) -> str | None:
        for key, value in mapping.items():
            normalized = normalize_field_name(str(key))
            if normalized in {"id", "file_id", "attachment_id"} and isinstance(
                value, (str, int)
            ):
                return str(value)
        return None

    @staticmethod
    def _first_text(values: list[Any]) -> str | None:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (int, float)):
                return str(value)
        return None

    @staticmethod
    def _extract_isins(values: list[Any]) -> set[str]:
        isins: set[str] = set()
        for value in values:
            text = json.dumps(value, ensure_ascii=False, default=str)
            for token in re.findall(
                r"\b[A-Za-z]{2}[A-Za-z0-9]{9}[0-9]\b",
                text,
            ):
                normalized = token.upper()
                if ISIN_RE.fullmatch(normalized):
                    isins.add(normalized)
        return isins

    @staticmethod
    def _extract_date(values: list[Any]) -> date | None:
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            normalized = value.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized).date()
            except ValueError:
                parsed = None
                for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                    try:
                        parsed = datetime.strptime(
                            normalized[:10],
                            fmt,
                        ).date()
                        break
                    except ValueError:
                        continue
            if parsed is None:
                LOGGER.debug("Date OpenDataSoft non reconnue: %s", value)
                continue
            if 1900 <= parsed.year <= date.today().year + 1:
                return parsed
            LOGGER.debug("Date OpenDataSoft sentinelle ignorée: %s", value)
        return None
