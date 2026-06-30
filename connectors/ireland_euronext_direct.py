from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from classification import classify_document
from connectors.base import (
    Connector,
    ConnectorError,
    ConnectorState,
    DocumentCandidate,
    EndpointAttempt,
)
from models import Issuer

LOGGER = logging.getLogger(__name__)

DEFAULT_DIRECT_URL = "https://direct.euronext.com"
DEFAULT_DUBLIN_URL = (
    "https://www.euronext.com/en/about-euronext/markets/dublin"
)
SUPPORTED_FILE_TYPES = {"pdf", "xhtml", "xml", "zip"}
FINANCIAL_TERMS = (
    "annual financial report",
    "annual report",
    "annual results",
    "publication of annual report",
    "half-year financial report",
    "half year financial report",
    "half-yearly report",
    "half yearly report",
    "interim report",
    "annual financial and audit reports",
    "half yearly financial reports and audit reports",
    "esef",
    "xhtml",
    "xml",
    "zip",
)
API_FIELDS = (
    "filingDate",
    "releaseDate",
    "companyName",
    "headline",
    "headLine",
    "regulatoryCategory",
    "regulatoryCategoryDescription",
    "publishTime",
    "documents",
    "previousXHTMLVersions",
    "totalItems",
    "currentPage",
    "numberOfPages",
)


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def _company_key(value: str) -> str:
    words = _normalize(value).split()
    expansions = {
        "cont": "continental",
        "gp": "group",
        "hold": "holdings",
        "perm": "permanent",
        "prop": "properties",
        "res": "residential",
    }
    words = [expansions.get(word, word) for word in words]
    suffixes = {
        "company",
        "dac",
        "designated",
        "limited",
        "ltd",
        "nv",
        "plc",
        "public",
        "sa",
    }
    return " ".join(word for word in words if word not in suffixes)


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None


def _file_type(filename: str, content_type: str = "") -> str | None:
    path = urlparse(filename).path.casefold()
    mime = content_type.casefold().split(";", 1)[0].strip()
    if path.endswith((".xhtml", ".xht")) or mime == "application/xhtml+xml":
        return "xhtml"
    if path.endswith(".xml") or ".xml." in path or mime in {
        "application/xml",
        "text/xml",
    }:
        return "xml"
    if path.endswith((".zip", ".xbri")) or mime in {
        "application/zip",
        "application/x-zip-compressed",
    }:
        return "zip"
    if path.endswith(".pdf") or mime == "application/pdf":
        return "pdf"
    return None


def _financial_type(category: str, title: str, filename: str) -> str | None:
    text = _normalize(f"{category} {title} {filename}")
    if any(
        marker in text
        for marker in (
            "half year financial report",
            "half yearly financial report",
            "half yearly financial reports and audit reports",
            "half yearly report",
            "interim report",
        )
    ):
        return "half_year_financial_report"
    if any(
        marker in text
        for marker in (
            "annual financial and audit reports",
            "annual financial report",
            "annual report",
            "annual results",
            "publication of annual report",
            "full year",
            "audited financial statement",
            "audited financial statements",
        )
    ):
        return "annual_financial_report"
    classified = classify_document(f"{category} {title}", filename)
    if classified:
        return classified
    if any(marker in text for marker in ("esef", "xhtml", "xml", "zip")):
        return "esef"
    return None


def _record_id_from_url(url: str) -> str | None:
    return parse_qs(urlparse(url).query).get("id", [None])[0]


def _download_url(
    direct_url: str,
    *,
    source_kind: str,
    file_id: str,
    filename: str,
) -> str:
    route = "OAMDocument" if source_kind == "oam" else "RISDocument"
    safe_name = quote(filename, safe="._-()")
    return (
        f"{direct_url.rstrip('/')}/api/PublicAnnouncements/"
        f"{route}/{safe_name}?id={quote(file_id)}"
    )


@dataclass(frozen=True, slots=True)
class IrelandFile:
    file_id: str
    filename: str
    file_type: str
    download_url: str


@dataclass(frozen=True, slots=True)
class IrelandNotice:
    record_id: str
    published_date: date | None
    company_name: str
    headline: str
    regulatory_category: str
    regulatory_category_description: str
    detail_url: str
    source_kind: str
    files: tuple[IrelandFile, ...]
    isin_codes: tuple[str, ...] = ()
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedIrelandPage:
    notices: tuple[IrelandNotice, ...]
    total_count: int
    current_page: int
    number_of_pages: int
    next_url: str | None
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IrelandEndpointCandidate:
    url: str
    role: str
    format: str
    pagination: str
    fields: tuple[str, ...]
    verified: bool
    state: ConnectorState
    http_status: int | None
    records_count: int | None


@dataclass(frozen=True, slots=True)
class IrelandSourceDiscovery:
    source: str
    query: str
    candidates: tuple[IrelandEndpointCandidate, ...]
    notices: tuple[IrelandNotice, ...]
    attempts: tuple[EndpointAttempt, ...]


@dataclass(frozen=True, slots=True)
class IrelandSourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    oam_url: str
    ris_url: str
    dublin_url: str
    http_status: int | None
    total_count: int | None
    detected_count: int
    fields: tuple[str, ...]
    formats: tuple[str, ...]
    example_notice: dict[str, Any] | None
    checks: dict[str, bool]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IrelandIssuerResolution:
    found: bool
    matched_name: str | None
    direct_url: str | None
    oam_url: str | None
    detail_url: str | None
    record_id: str | None
    home_member_state: str | None
    match_score: float
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


def _raw_isins(item: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    raw_values = item.get("isinCodes") or item.get("isins") or []
    if isinstance(raw_values, str):
        raw_values = re.split(r"[\s,;]+", raw_values)
    if not isinstance(raw_values, list):
        raw_values = [raw_values]
    for raw in raw_values:
        if isinstance(raw, dict):
            value = str(raw.get("code") or raw.get("isin") or "")
        else:
            value = str(raw or "")
        values.extend(re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", value.upper()))
    single = str(item.get("isin") or "")
    values.extend(re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", single.upper()))
    return tuple(dict.fromkeys(values))


def parse_direct_json(
    payload: dict[str, Any],
    *,
    direct_url: str,
    source_kind: str,
) -> ParsedIrelandPage:
    raw_records = payload.get("records") or payload.get("items") or []
    if not isinstance(raw_records, list):
        raw_records = []
    notices: list[IrelandNotice] = []
    observed_fields: set[str] = set()
    detail_url = (
        f"{direct_url.rstrip('/')}/#/oamfiling"
        if source_kind == "oam"
        else f"{direct_url.rstrip('/')}/#/rispublication"
    )
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        observed_fields.update(str(key) for key in item)
        company_name = str(item.get("companyName") or "").strip()
        headline = str(item.get("headline") or item.get("headLine") or "").strip()
        category = str(item.get("regulatoryCategory") or "").strip()
        category_description = str(
            item.get("regulatoryCategoryDescription") or ""
        ).strip()
        files: list[IrelandFile] = []
        raw_documents = item.get("documents") or item.get("mainDocuments") or []
        if not isinstance(raw_documents, list):
            raw_documents = []
        for raw_file in raw_documents:
            if not isinstance(raw_file, dict):
                continue
            file_id = str(
                raw_file.get("id")
                or raw_file.get("fileDataId")
                or raw_file.get("documentId")
                or ""
            ).strip()
            filename = str(
                raw_file.get("name")
                or raw_file.get("originalFileName")
                or raw_file.get("filename")
                or ""
            ).strip()
            file_type = _file_type(filename)
            if not file_id or not filename or not file_type:
                continue
            folder_url = str(raw_file.get("folderUrl") or "").strip()
            download_url = (
                folder_url
                if folder_url.startswith(("http://", "https://"))
                else _download_url(
                    direct_url,
                    source_kind=source_kind,
                    file_id=file_id,
                    filename=filename,
                )
            )
            files.append(
                IrelandFile(
                    file_id=file_id,
                    filename=filename,
                    file_type=file_type,
                    download_url=download_url,
                )
            )
        record_id = str(item.get("id") or "").strip()
        if record_id == "00000000-0000-0000-0000-000000000000":
            record_id = ""
        if not record_id and files:
            record_id = files[0].file_id
        missing = [
            name
            for name, value in (
                ("companyName", company_name),
                ("headline", headline),
                ("date", item.get("filingDate") or item.get("releaseDate")),
                ("documents", files),
            )
            if not value
        ]
        if missing:
            LOGGER.debug(
                "Euronext Direct %s champs manquants: %s",
                source_kind.upper(),
                ", ".join(missing),
            )
        if not company_name or not headline or not record_id:
            continue
        notices.append(
            IrelandNotice(
                record_id=record_id,
                published_date=_parse_date(
                    item.get("filingDate")
                    or item.get("releaseDate")
                    or item.get("publishTime")
                ),
                company_name=company_name,
                headline=headline,
                regulatory_category=category,
                regulatory_category_description=category_description,
                detail_url=detail_url,
                source_kind=source_kind,
                files=tuple(files),
                isin_codes=_raw_isins(item),
                symbol=str(item.get("symbol") or item.get("ticker") or "").strip()
                or None,
            )
        )
    total_count = int(payload.get("totalItems") or len(notices))
    current_page = int(payload.get("currentPage") or 0)
    number_of_pages = int(
        payload.get("numberOfPages") or (1 if total_count else 0)
    )
    return ParsedIrelandPage(
        notices=tuple(notices),
        total_count=total_count,
        current_page=current_page,
        number_of_pages=number_of_pages,
        next_url=None,
        fields=tuple(sorted(observed_fields)),
    )


def _cell_text(cells: list[Tag], index: int) -> str:
    if index >= len(cells):
        return ""
    return " ".join(cells[index].get_text(" ", strip=True).split())


def parse_direct_html(
    html: str,
    *,
    direct_url: str,
    source_kind: str,
) -> ParsedIrelandPage:
    soup = BeautifulSoup(html, "html.parser")
    containers = list(
        soup.select(
            "[data-record-id], [data-announcement-id], .announcement, "
            ".publication-row, table tbody tr"
        )
    )
    notices: list[IrelandNotice] = []
    observed_fields: set[str] = set()
    page_url = (
        f"{direct_url.rstrip('/')}/#/oamfiling"
        if source_kind == "oam"
        else f"{direct_url.rstrip('/')}/#/rispublication"
    )
    for index, container in enumerate(containers):
        cells = list(container.select("td"))
        company = str(
            container.get("data-company")
            or container.get("data-company-name")
            or _cell_text(cells, 1)
        ).strip()
        headline = str(
            container.get("data-headline")
            or container.get("data-title")
            or _cell_text(cells, 2)
        ).strip()
        published_text = str(
            container.get("data-filing-date")
            or container.get("data-release-date")
            or _cell_text(cells, 0)
        ).strip()
        category = str(
            container.get("data-category")
            or container.get("data-regulatory-category")
            or (_cell_text(cells, 3) if source_kind == "oam" else "")
        ).strip()
        category_description = str(
            container.get("data-category-description") or category
        ).strip()
        links = container.select(
            'a[href*="/api/PublicAnnouncements/"], '
            'a[href$=".pdf"], a[href$=".xhtml"], a[href$=".xml"], '
            'a[href$=".zip"]'
        )
        files: list[IrelandFile] = []
        for link in links:
            href = urljoin(direct_url.rstrip("/") + "/", str(link.get("href")))
            filename = (
                str(link.get("download") or "").strip()
                or PurePosixPath(urlparse(href).path).name
            )
            file_type = _file_type(filename)
            if not file_type:
                continue
            file_id = (
                str(link.get("data-document-id") or "").strip()
                or _record_id_from_url(href)
                or href
            )
            files.append(IrelandFile(file_id, filename, file_type, href))
        unique_files = {item.download_url: item for item in files}
        record_id = str(
            container.get("data-record-id")
            or container.get("data-announcement-id")
            or (next(iter(unique_files.values())).file_id if unique_files else "")
            or f"html-{index}"
        ).strip()
        if not company or not headline or not unique_files:
            continue
        observed_fields.update(
            ("companyName", "headline", "date", "documents")
        )
        if category_description:
            observed_fields.add("regulatoryCategoryDescription")
        raw_isin = str(container.get("data-isin") or "")
        notices.append(
            IrelandNotice(
                record_id=record_id,
                published_date=_parse_date(published_text),
                company_name=company,
                headline=headline,
                regulatory_category=category,
                regulatory_category_description=category_description,
                detail_url=str(
                    container.get("data-detail-url") or page_url
                ),
                source_kind=source_kind,
                files=tuple(unique_files.values()),
                isin_codes=tuple(
                    re.findall(
                        r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b",
                        raw_isin.upper(),
                    )
                ),
                symbol=str(container.get("data-symbol") or "").strip() or None,
            )
        )
    total_count = len(notices)
    count_node = soup.select_one("[data-total-items], .result-count")
    if count_node:
        match = re.search(
            r"\d+",
            str(
                count_node.get("data-total-items")
                or count_node.get_text(" ", strip=True)
            ).replace(",", ""),
        )
        if match:
            total_count = int(match.group())
    next_link = soup.select_one(
        'a[rel="next"][href], a.next[href], '
        'a[aria-label*="next" i][href]'
    )
    return ParsedIrelandPage(
        notices=tuple(notices),
        total_count=total_count,
        current_page=0,
        number_of_pages=max(1, (total_count + 49) // 50) if total_count else 0,
        next_url=(
            urljoin(direct_url.rstrip("/") + "/", str(next_link.get("href")))
            if next_link
            else None
        ),
        fields=tuple(sorted(observed_fields)),
    )


def issuer_notice_match_score(issuer: Issuer, notice: IrelandNotice) -> float:
    if issuer.isin and issuer.isin.upper() in notice.isin_codes:
        return 100.0
    expected = _company_key(issuer.name)
    observed = _company_key(notice.company_name)
    if expected and observed:
        if expected == observed:
            return 90.0
        if expected in observed or observed in expected:
            return 82.0
        expected_words = set(expected.split())
        observed_words = set(observed.split())
        if expected_words and observed_words:
            overlap = len(expected_words & observed_words) / len(
                expected_words | observed_words
            )
            if overlap >= 0.6:
                return 65.0 + overlap * 15.0
        similarity = SequenceMatcher(None, expected, observed).ratio()
        if similarity >= 0.78:
            return 60.0 + similarity * 20.0
    symbol = _normalize(issuer.symbol)
    notice_symbol = _normalize(notice.symbol or "")
    if symbol and notice_symbol and symbol == notice_symbol:
        return 70.0
    combined = _normalize(f"{notice.company_name} {notice.headline}")
    if len(symbol) >= 3 and re.search(rf"\b{re.escape(symbol)}\b", combined):
        return 55.0
    return 0.0


def match_issuer_notice(issuer: Issuer, notice: IrelandNotice) -> bool:
    return issuer_notice_match_score(issuer, notice) >= 55.0


class IrelandEuronextDirectConnector(Connector):
    market = "Euronext Dublin"
    source_name = "euronext_direct"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str = DEFAULT_DIRECT_URL,
        dublin_url: str = DEFAULT_DUBLIN_URL,
        market: str = "Euronext Dublin",
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 900,
        timeout: int = 30,
        max_pages: int = 20,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.dublin_url = dublin_url
        self.market = market
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.max_pages = max(1, max_pages)
        self.oam_page_url = f"{self.base_url}/#/oamfiling"
        self.ris_page_url = f"{self.base_url}/#/rispublication"
        self.oam_api_url = f"{self.base_url}/api/PublicAnnouncements/OAMs"
        self.ris_api_url = f"{self.base_url}/api/PublicAnnouncements/RIS"
        self.state = ConnectorState.READY
        self.last_error = None
        self._last_request_at = 0.0
        self._issuer_cache: dict[str, tuple[IrelandNotice, ...]] = {}

    def _wait(self) -> None:
        delay = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if delay > 0:
            time.sleep(delay)

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(2):
            self._wait()
            try:
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                    **kwargs,
                )
                self._last_request_at = time.monotonic()
                if response.status_code >= 500 and attempt == 0:
                    time.sleep(0.8)
                    continue
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.8)
        raise ConnectorError(str(last_error or f"GET impossible: {url}"))

    def _post(
        self,
        url: str,
        *,
        payload: dict[str, Any],
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(2):
            self._wait()
            try:
                response = self.session.post(
                    url,
                    json=payload,
                    timeout=self.timeout,
                )
                self._last_request_at = time.monotonic()
                if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                    time.sleep(0.8)
                    continue
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.8)
        raise ConnectorError(str(last_error or f"POST impossible: {url}"))

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except (ValueError, AttributeError):
            data = json.loads(response.text)
        if not isinstance(data, dict):
            raise ConnectorError("Réponse Euronext Direct JSON invalide")
        return data

    @staticmethod
    def _attempt(
        *,
        name: str,
        url: str,
        method: str,
        response: requests.Response | None = None,
        success: bool,
        total_count: int | None = None,
        error: str | None = None,
    ) -> EndpointAttempt:
        excerpt = None
        if response is not None and not success:
            excerpt = " ".join(str(getattr(response, "text", ""))[:300].split())
        parsed = urlparse(url)
        return EndpointAttempt(
            name=name,
            base_url=f"{parsed.scheme}://{parsed.netloc}",
            dataset="euronext_direct",
            endpoint=url,
            method=method,
            http_status=response.status_code if response is not None else None,
            success=success,
            total_count=total_count,
            response_excerpt=excerpt,
            error=error,
        )

    def _payload(
        self,
        *,
        page: int,
        company_name: str = "",
        first_letter: str = "",
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, Any]:
        end = end_date or date.today()
        start = start_date or (end - timedelta(days=self.lookback_days))
        return {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "page": page,
            "firstLetter": first_letter[:1].casefold(),
            "companyName": company_name,
        }

    def _fetch_pages(
        self,
        source_kind: str,
        *,
        company_name: str = "",
        first_letter: str = "",
        start_date: date | None = None,
        end_date: date | None = None,
        max_pages: int | None = None,
        attempts: list[EndpointAttempt] | None = None,
    ) -> tuple[list[IrelandNotice], int]:
        url = self.oam_api_url if source_kind == "oam" else self.ris_api_url
        collected: list[IrelandNotice] = []
        total_count = 0
        page_limit = max_pages or self.max_pages
        for page in range(page_limit):
            payload = self._payload(
                page=page,
                company_name=company_name,
                first_letter=first_letter,
                start_date=start_date,
                end_date=end_date,
            )
            try:
                response = self._post(url, payload=payload)
                response.raise_for_status()
                parsed = parse_direct_json(
                    self._json(response),
                    direct_url=self.base_url,
                    source_kind=source_kind,
                )
                total_count = parsed.total_count
                success = bool(parsed.notices)
                if attempts is not None:
                    attempts.append(
                        self._attempt(
                            name=f"euronext_direct_{source_kind}_page_{page + 1}",
                            url=url,
                            method="POST",
                            response=response,
                            success=success,
                            total_count=total_count,
                        )
                    )
                collected.extend(parsed.notices)
                if (
                    not parsed.notices
                    or parsed.number_of_pages <= page + 1
                    or len(collected) >= total_count
                ):
                    break
            except Exception as exc:
                if attempts is not None:
                    attempts.append(
                        self._attempt(
                            name=f"euronext_direct_{source_kind}_page_{page + 1}",
                            url=url,
                            method="POST",
                            success=False,
                            error=str(exc),
                        )
                    )
                raise
        unique = {
            (notice.source_kind, notice.record_id, notice.company_name): notice
            for notice in collected
        }
        return list(unique.values()), total_count

    def _load_html_fallback(
        self,
        source_kind: str,
        attempts: list[EndpointAttempt] | None = None,
    ) -> list[IrelandNotice]:
        page_url = (
            self.oam_page_url if source_kind == "oam" else self.ris_page_url
        )
        try:
            response = self._get(page_url)
            response.raise_for_status()
            parsed = parse_direct_html(
                response.text,
                direct_url=self.base_url,
                source_kind=source_kind,
            )
            if attempts is not None:
                attempts.append(
                    self._attempt(
                        name=f"euronext_direct_{source_kind}_html",
                        url=page_url,
                        method="GET",
                        response=response,
                        success=bool(parsed.notices),
                        total_count=parsed.total_count,
                    )
                )
            return list(parsed.notices)
        except Exception as exc:
            if attempts is not None:
                attempts.append(
                    self._attempt(
                        name=f"euronext_direct_{source_kind}_html",
                        url=page_url,
                        method="GET",
                        success=False,
                        error=str(exc),
                    )
                )
            return []

    @staticmethod
    def _issuer_queries(issuer: Issuer) -> tuple[str, ...]:
        expanded = _company_key(issuer.name)
        words = expanded.split()
        queries = [issuer.name, expanded]
        if words:
            queries.append(words[0])
        if issuer.symbol:
            queries.append(issuer.symbol)
        return tuple(
            dict.fromkeys(
                value.strip()
                for value in queries
                if value and len(value.strip()) >= 3
            )
        )

    def _issuer_notices(
        self,
        issuer: Issuer,
        attempts: list[EndpointAttempt] | None = None,
    ) -> tuple[IrelandNotice, ...]:
        cache_key = issuer.isin or f"{issuer.name}|{issuer.symbol}"
        if cache_key in self._issuer_cache:
            return self._issuer_cache[cache_key]
        api_error: Exception | None = None
        for query in self._issuer_queries(issuer):
            first_letter = _normalize(query)[:1]
            try:
                notices, _ = self._fetch_pages(
                    "oam",
                    company_name=query,
                    first_letter=first_letter,
                    attempts=attempts,
                )
            except Exception as exc:
                api_error = exc
                break
            matched = tuple(
                notice for notice in notices if match_issuer_notice(issuer, notice)
            )
            if matched:
                self._issuer_cache[cache_key] = matched
                return matched
        if api_error is not None:
            fallback = [
                notice
                for notice in self._load_html_fallback("oam", attempts)
                if match_issuer_notice(issuer, notice)
            ]
            if fallback:
                self.mark_degraded(
                    f"API Euronext Direct indisponible, fallback HTML: {api_error}"
                )
                self._issuer_cache[cache_key] = tuple(fallback)
                return tuple(fallback)
            self.mark_degraded(f"Euronext Direct inexploitable: {api_error}")
            raise ConnectorError(self.last_error or str(api_error)) from api_error

        # RIS is an official fallback when no OAM financial filing matches.
        for query in self._issuer_queries(issuer):
            try:
                notices, _ = self._fetch_pages(
                    "ris",
                    company_name=query,
                    first_letter=_normalize(query)[:1],
                    max_pages=min(self.max_pages, 10),
                    attempts=attempts,
                )
            except Exception as exc:
                LOGGER.warning("Fallback RIS impossible pour %s: %s", issuer.name, exc)
                break
            matched = tuple(
                notice for notice in notices if match_issuer_notice(issuer, notice)
            )
            if matched:
                self._issuer_cache[cache_key] = matched
                return matched
        self._issuer_cache[cache_key] = ()
        return ()

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        candidates: list[DocumentCandidate] = []
        for notice in self._issuer_notices(issuer):
            candidates.extend(self._notice_candidates(notice))
        unique = {candidate.url: candidate for candidate in candidates}
        return sorted(
            unique.values(),
            key=lambda candidate: (
                candidate.published_date or date.min,
                candidate.title.casefold(),
                candidate.url,
            ),
            reverse=True,
        )

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
        page_limit = min(
            self.max_pages,
            max(1, (candidate_limit + 49) // 50),
        )
        attempts: list[EndpointAttempt] = []
        notices: list[IrelandNotice] = []
        errors: list[str] = []
        for source_kind in ("oam", "ris"):
            try:
                found, _ = self._fetch_pages(
                    source_kind,
                    start_date=cutoff,
                    end_date=date.today(),
                    max_pages=page_limit,
                    attempts=attempts,
                )
                notices.extend(found)
            except Exception as exc:
                errors.append(f"{source_kind.upper()}: {exc}")
                fallback = self._load_html_fallback(
                    source_kind,
                    attempts,
                )
                notices.extend(
                    notice
                    for notice in fallback
                    if (
                        notice.published_date is None
                        or notice.published_date >= cutoff
                    )
                )

        unique_notices = {
            (notice.source_kind, notice.record_id): notice
            for notice in notices
            if (
                notice.published_date is None
                or notice.published_date >= cutoff
            )
        }
        self._scanned_notices = len(unique_notices)
        if not unique_notices and errors:
            self.mark_degraded("; ".join(errors))
            return []
        if errors:
            self.mark_degraded("; ".join(errors))
        else:
            self.state = ConnectorState.READY
            self.last_error = None

        candidates: list[DocumentCandidate] = []
        for notice in unique_notices.values():
            candidates.extend(self._notice_candidates(notice))
            if len(candidates) >= candidate_limit:
                break
        unique = {candidate.url: candidate for candidate in candidates}
        return sorted(
            list(unique.values())[:candidate_limit],
            key=lambda candidate: (
                candidate.published_date or date.min,
                candidate.title.casefold(),
                candidate.url,
            ),
            reverse=True,
        )

    def _notice_candidates(
        self,
        notice: IrelandNotice,
    ) -> list[DocumentCandidate]:
        candidates: list[DocumentCandidate] = []
        for file in notice.files:
            if file.file_type not in SUPPORTED_FILE_TYPES:
                continue
            document_type = _financial_type(
                notice.regulatory_category_description
                or notice.regulatory_category,
                notice.headline,
                file.filename,
            )
            if not document_type:
                continue
            candidates.append(
                DocumentCandidate(
                    title=notice.headline,
                    url=file.download_url,
                    published_date=notice.published_date,
                    document_type=document_type,
                    source=self.source_name,
                    source_document_id=file.file_id,
                    metadata={
                        "ireland_record_id": notice.record_id,
                        "ireland_euronext_direct_url": self.base_url,
                        "ireland_euronext_oam_url": self.oam_page_url,
                        "detail_url": notice.detail_url,
                        "home_member_state": "Ireland",
                        "regulatory_category": (
                            notice.regulatory_category_description
                            or notice.regulatory_category
                        ),
                        "file_format": file.file_type,
                        "filename": file.filename,
                        "source_kind": notice.source_kind,
                        "isins": list(notice.isin_codes),
                        "issuer_isins": list(notice.isin_codes),
                        "issuer_name": notice.company_name,
                        "issuer_symbol": notice.symbol,
                    },
                )
            )
        return candidates

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        page_limit = min(
            self.max_pages,
            max(1, ((limit or 1000) + 49) // 50),
        )
        return page_limit * 2

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return max(1, len(self._issuer_queries(issuer)) * 2)

    @staticmethod
    def _notice_output(notice: IrelandNotice) -> dict[str, Any]:
        return {
            "record_id": notice.record_id,
            "publication_date": (
                notice.published_date.isoformat()
                if notice.published_date
                else None
            ),
            "issuer": notice.company_name,
            "title": notice.headline,
            "regulatory_category": (
                notice.regulatory_category_description
                or notice.regulatory_category
            ),
            "detail_url": notice.detail_url,
            "source_kind": notice.source_kind,
            "isins": list(notice.isin_codes),
            "symbol": notice.symbol,
            "files": [
                {
                    "file_id": item.file_id,
                    "filename": item.filename,
                    "format": item.file_type,
                    "download_url": item.download_url,
                }
                for item in notice.files
            ],
        }

    def _probe_download(
        self,
        file: IrelandFile,
        attempts: list[EndpointAttempt],
    ) -> bool:
        try:
            response = self._get(file.download_url, stream=True)
            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type") or "")
            detected = _file_type(file.filename, content_type)
            valid = detected in SUPPORTED_FILE_TYPES
            attempts.append(
                self._attempt(
                    name=f"euronext_direct_download_{file.file_type}",
                    url=file.download_url,
                    method="GET",
                    response=response,
                    success=valid,
                )
            )
            response.close()
            return valid
        except Exception as exc:
            attempts.append(
                self._attempt(
                    name=f"euronext_direct_download_{file.file_type}",
                    url=file.download_url,
                    method="GET",
                    success=False,
                    error=str(exc),
                )
            )
            return False

    def diagnose(self) -> IrelandSourceDiagnostic:
        attempts: list[EndpointAttempt] = []
        checks = {
            "euronext_direct_accessible": False,
            "euronext_dublin_accessible": False,
            "oam_api": False,
            "ris_api": False,
            "public_search": False,
            "pagination": False,
            "real_notices": False,
            "download_links": False,
            "automatic_download": False,
            "html_fallback_detected": False,
        }
        direct_status: int | None = None
        errors: list[str] = []
        for name, url, check in (
            ("euronext_direct_home", self.base_url, "euronext_direct_accessible"),
            ("euronext_dublin_home", self.dublin_url, "euronext_dublin_accessible"),
        ):
            try:
                response = self._get(url)
                response.raise_for_status()
                if name == "euronext_direct_home":
                    direct_status = response.status_code
                checks[check] = True
                attempts.append(
                    self._attempt(
                        name=name,
                        url=url,
                        method="GET",
                        response=response,
                        success=True,
                    )
                )
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                attempts.append(
                    self._attempt(
                        name=name,
                        url=url,
                        method="GET",
                        success=False,
                        error=str(exc),
                    )
                )

        oam_notices: list[IrelandNotice] = []
        ris_notices: list[IrelandNotice] = []
        total_count: int | None = None
        try:
            oam_notices, total_count = self._fetch_pages(
                "oam",
                max_pages=6,
                attempts=attempts,
            )
            checks["oam_api"] = True
        except Exception as exc:
            errors.append(f"OAM API: {exc}")
            oam_notices = self._load_html_fallback("oam", attempts)
            checks["html_fallback_detected"] = bool(oam_notices)
        try:
            ris_notices, _ = self._fetch_pages(
                "ris",
                max_pages=2,
                attempts=attempts,
            )
            checks["ris_api"] = True
        except Exception as exc:
            errors.append(f"RIS API: {exc}")
            ris_notices = self._load_html_fallback("ris", attempts)
            checks["html_fallback_detected"] = (
                checks["html_fallback_detected"] or bool(ris_notices)
            )

        notices = oam_notices or ris_notices
        checks["public_search"] = checks["oam_api"] or checks["ris_api"] or bool(
            notices
        )
        checks["real_notices"] = bool(notices)
        checks["pagination"] = any(
            attempt.name.endswith("_page_2")
            for attempt in attempts
        ) or bool(notices and (total_count or 0) <= len(notices))
        files = [
            file
            for notice in notices
            for file in notice.files
            if file.file_type in SUPPORTED_FILE_TYPES
        ]
        checks["download_links"] = bool(files)
        probed: set[str] = set()
        results: list[bool] = []
        for file in files:
            if file.file_type in probed:
                continue
            probed.add(file.file_type)
            results.append(self._probe_download(file, attempts))
            if any(results):
                break
        checks["automatic_download"] = any(results)

        required = (
            checks["euronext_direct_accessible"],
            checks["euronext_dublin_accessible"],
            checks["oam_api"],
            checks["ris_api"],
            checks["real_notices"],
            checks["download_links"],
            checks["automatic_download"],
        )
        if all(required):
            state = ConnectorState.READY
            error = None
        elif checks["euronext_direct_accessible"] or checks["public_search"]:
            state = ConnectorState.DEGRADED
            error = "; ".join(errors) or "Diagnostic Euronext Direct partiel"
        else:
            state = ConnectorState.UNAVAILABLE
            error = "; ".join(errors) or "Euronext Direct inaccessible"
        return IrelandSourceDiagnostic(
            source=self.source_name,
            state=state,
            called_url=self.base_url,
            oam_url=self.oam_api_url,
            ris_url=self.ris_api_url,
            dublin_url=self.dublin_url,
            http_status=direct_status,
            total_count=total_count,
            detected_count=len(notices),
            fields=API_FIELDS,
            formats=tuple(sorted({file.file_type for file in files})),
            example_notice=self._notice_output(notices[0]) if notices else None,
            checks=checks,
            attempts=tuple(attempts),
            error=error,
        )

    @staticmethod
    def _query_matches(query: str, notice: IrelandNotice) -> bool:
        query_words = set(_normalize(query).split())
        text = _normalize(
            f"{notice.headline} {notice.regulatory_category} "
            f"{notice.regulatory_category_description} "
            f"{' '.join(file.filename for file in notice.files)}"
        )
        return bool(query_words) and query_words.issubset(set(text.split()))

    def discover(self, query: str) -> IrelandSourceDiscovery:
        attempts: list[EndpointAttempt] = []
        candidates: list[IrelandEndpointCandidate] = []
        collected: list[IrelandNotice] = []
        for source_kind, url, pages, role in (
            (
                "oam",
                self.oam_api_url,
                8,
                "primary Euronext Dublin OAM regulated-information listing",
            ),
            (
                "ris",
                self.ris_api_url,
                3,
                "public Euronext Direct RIS announcement listing",
            ),
        ):
            notices: list[IrelandNotice] = []
            total_count: int | None = None
            error: str | None = None
            try:
                notices, total_count = self._fetch_pages(
                    source_kind,
                    max_pages=pages,
                    attempts=attempts,
                )
                collected.extend(
                    notice
                    for notice in notices
                    if self._query_matches(query, notice)
                )
            except Exception as exc:
                error = str(exc)
            candidates.append(
                IrelandEndpointCandidate(
                    url=url,
                    role=role,
                    format="JSON",
                    pagination="page, currentPage, numberOfPages",
                    fields=API_FIELDS,
                    verified=bool(notices),
                    state=(
                        ConnectorState.READY
                        if notices
                        else ConnectorState.DEGRADED
                    ),
                    http_status=200 if notices else None,
                    records_count=total_count,
                )
            )
            if error:
                LOGGER.warning("Découverte %s échouée: %s", source_kind, error)

        html_notices = self._load_html_fallback("oam", attempts)
        collected.extend(
            notice
            for notice in html_notices
            if self._query_matches(query, notice)
        )
        candidates.append(
            IrelandEndpointCandidate(
                url=self.oam_page_url,
                role="public Euronext Direct HTML fallback",
                format="HTML",
                pagination="next link or public SPA pagination",
                fields=(
                    "date",
                    "company",
                    "headline",
                    "regulatory category",
                    "document link",
                ),
                verified=bool(html_notices),
                state=(
                    ConnectorState.READY
                    if html_notices
                    else ConnectorState.DEGRADED
                ),
                http_status=next(
                    (
                        attempt.http_status
                        for attempt in attempts
                        if attempt.name == "euronext_direct_oam_html"
                    ),
                    None,
                ),
                records_count=len(html_notices) or None,
            )
        )
        unique = {
            (notice.source_kind, notice.record_id): notice
            for notice in collected
        }
        notices = sorted(
            unique.values(),
            key=lambda item: item.published_date or date.min,
            reverse=True,
        )
        return IrelandSourceDiscovery(
            source=self.source_name,
            query=query,
            candidates=tuple(candidates),
            notices=tuple(notices),
            attempts=tuple(attempts),
        )

    def resolve_issuer(
        self,
        *,
        symbol: str,
        name: str,
        isin: str | None = None,
    ) -> IrelandIssuerResolution:
        attempts: list[EndpointAttempt] = []
        issuer = Issuer(
            name=name,
            isin=isin or "",
            symbol=symbol,
            market=self.market,
        )
        try:
            notices = list(self._issuer_notices(issuer, attempts))
        except Exception as exc:
            return IrelandIssuerResolution(
                found=False,
                matched_name=None,
                direct_url=self.base_url,
                oam_url=self.oam_page_url,
                detail_url=None,
                record_id=None,
                home_member_state="Ireland",
                match_score=0.0,
                attempts=tuple(attempts),
                error=str(exc),
            )
        ranked = sorted(
            (
                (issuer_notice_match_score(issuer, notice), notice)
                for notice in notices
            ),
            key=lambda item: (
                item[0],
                item[1].published_date or date.min,
            ),
            reverse=True,
        )
        selected = ranked[0][1] if ranked else None
        score = ranked[0][0] if ranked else 0.0
        return IrelandIssuerResolution(
            found=selected is not None,
            matched_name=selected.company_name if selected else None,
            direct_url=self.base_url,
            oam_url=self.oam_page_url,
            detail_url=selected.detail_url if selected else None,
            record_id=selected.record_id if selected else None,
            home_member_state="Ireland",
            match_score=score,
            attempts=tuple(attempts),
            error=None if selected else "Aucune notice Euronext Direct correspondante",
        )
