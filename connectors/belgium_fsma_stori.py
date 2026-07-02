from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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

DEFAULT_PUBLIC_URL = "https://www.fsma.be/en/stori"
DEFAULT_API_ORIGIN = "https://webapi.fsma.be"
ANNUAL_TYPE_ID = "9813c451-9fd4-41ba-ba7d-4e0dda0d3051"
HALF_YEAR_TYPE_ID = "69dda7b3-bde3-4c1e-9c06-154c411b2c12"
SUPPORTED_FILE_TYPES = {"pdf", "xhtml", "zip", "xbri"}
FINANCIAL_TERMS = (
    "annual financial report",
    "half-yearly financial report",
    "half year financial report",
    "semi-annual financial report",
    "annual report",
    "rapport financier annuel",
    "rapport financier semestriel",
    "jaarverslag",
    "halfjaarlijks financieel verslag",
    "esef",
)


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        char for char in decomposed
        if not unicodedata.combining(char)
    )
    words = re.findall(r"[a-z0-9]+", ascii_value.casefold())
    ignored = {
        "sa",
        "nv",
        "n.v",
        "s.a",
        "se",
        "plc",
        "group",
        "groupe",
    }
    return " ".join(word for word in words if word not in ignored)


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


def _file_type(value: str, filename: str = "") -> str | None:
    normalized = (value or "").casefold().strip().lstrip(".")
    if normalized in SUPPORTED_FILE_TYPES:
        return normalized
    suffix = PurePosixPath(filename).suffix.casefold().lstrip(".")
    if suffix in SUPPORTED_FILE_TYPES:
        return suffix
    return None


def _financial_type(topic: str, title: str, filename: str = "") -> str | None:
    combined = f"{topic} {title} {filename}"
    normalized = _normalize(combined)
    title_normalized = _normalize(f"{title} {filename}")
    if any(
        marker in title_normalized
        for marker in (
            "press release",
            "communique de presse",
            "persbericht",
        )
    ) and not any(
        marker in title_normalized
        for marker in (
            "annual financial report",
            "rapport financier annuel",
            "jaarverslag",
            "annual report",
        )
    ):
        return "other_regulatory_announcement"
    if any(
        marker in normalized
        for marker in (
            "half yearly financial report",
            "half year financial report",
            "semi annual financial report",
            "rapport financier semestriel",
            "halfjaarlijks financieel verslag",
        )
    ):
        return "half_year_financial_report"
    if any(
        marker in normalized
        for marker in (
            "annual financial report",
            "annual report",
            "rapport financier annuel",
            "jaarverslag",
        )
    ):
        return "annual_financial_report"
    if any(marker in normalized for marker in ("esef", "xhtml", "xbri")):
        return "esef"
    classified = classify_document(combined, filename)
    if classified:
        return classified
    if any(_normalize(term) in normalized for term in FINANCIAL_TERMS):
        return "financial_report"
    return None


def _download_url(api_root: str, file_data_id: str) -> str:
    return f"{api_root}/download?fileDataId={quote(file_data_id)}"


@dataclass(frozen=True, slots=True)
class BelgiumFile:
    file_data_id: str
    language: str
    title: str
    original_filename: str
    size_kb: int | None
    file_type: str
    download_url: str


@dataclass(frozen=True, slots=True)
class BelgiumNotice:
    record_id: str
    company_name: str
    company_number: str | None
    nationality: str | None
    reporting_topic: str
    published_date: date | None
    received_date: date | None
    lei: str | None
    isin_codes: tuple[str, ...]
    markets: tuple[str, ...]
    document_title: str
    detail_url: str
    files: tuple[BelgiumFile, ...]


@dataclass(frozen=True, slots=True)
class ParsedBelgiumPage:
    notices: tuple[BelgiumNotice, ...]
    total_count: int | None
    next_url: str | None
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BelgiumEndpointCandidate:
    url: str
    role: str
    format: str
    pagination: str
    fields: tuple[str, ...]
    verified: bool
    state: ConnectorState
    http_status: int | None
    records_count: int | None = None


@dataclass(frozen=True, slots=True)
class BelgiumSourceDiscovery:
    source: str
    query: str
    candidates: tuple[BelgiumEndpointCandidate, ...]
    notices: tuple[BelgiumNotice, ...]
    attempts: tuple[EndpointAttempt, ...]


@dataclass(frozen=True, slots=True)
class BelgiumSourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    api_url: str
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
class BelgiumIssuerResolution:
    found: bool
    matched_name: str | None
    company_id: str | None
    isin: str | None
    stori_url: str | None
    detail_url: str | None
    home_member_state: str | None
    fsma_record_id: str | None
    match_score: float
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


def _tag_text(container: Tag, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        found = container.select_one(selector)
        if found:
            text = " ".join(found.get_text(" ", strip=True).split())
            if text:
                return text
    return ""


def _extract_html_files(
    container: Tag,
    *,
    base_url: str,
    api_root: str,
) -> tuple[BelgiumFile, ...]:
    files: list[BelgiumFile] = []
    for link in container.select("a[href], [data-file-data-id]"):
        href = str(link.get("href") or "").strip()
        file_data_id = str(
            link.get("data-file-data-id")
            or parse_qs(urlparse(href).query).get("fileDataId", [""])[0]
        ).strip()
        filename = str(
            link.get("download")
            or link.get("data-filename")
            or link.get("title")
            or link.get_text(" ", strip=True)
        ).strip()
        declared_type = str(link.get("data-file-type") or "")
        detected_type = _file_type(declared_type, filename or href)
        if not detected_type:
            continue
        download_url = (
            _download_url(api_root, file_data_id)
            if file_data_id
            else urljoin(base_url, href)
        )
        if not file_data_id:
            file_data_id = download_url
        files.append(
            BelgiumFile(
                file_data_id=file_data_id,
                language=str(link.get("data-language") or ""),
                title=str(link.get("data-title") or filename),
                original_filename=filename,
                size_kb=None,
                file_type=detected_type,
                download_url=download_url,
            )
        )
    unique = {item.download_url: item for item in files}
    return tuple(unique.values())


def parse_stori_detail_html(
    html: str,
    *,
    detail_url: str,
    api_root: str = f"{DEFAULT_API_ORIGIN}/api/v1/en/stori",
) -> tuple[BelgiumFile, ...]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    return _extract_html_files(
        root,
        base_url=detail_url,
        api_root=api_root,
    )


def parse_stori_html(
    html: str,
    *,
    base_url: str,
    api_root: str = f"{DEFAULT_API_ORIGIN}/api/v1/en/stori",
) -> ParsedBelgiumPage:
    soup = BeautifulSoup(html, "html.parser")
    containers: list[Tag] = list(
        soup.select(
            "[data-record-id], .stori-notice, .stori-result, "
            "table.stori-results tbody tr"
        )
    )
    if not containers:
        containers = [
            row for row in soup.select("main table tbody tr")
            if row.select_one("a[href]")
        ]

    notices: list[BelgiumNotice] = []
    observed_fields: set[str] = set()
    for index, container in enumerate(containers):
        record_id = str(
            container.get("data-record-id")
            or container.get("data-id")
            or ""
        ).strip()
        company = str(container.get("data-company") or "").strip()
        if not company:
            company = _tag_text(
                container,
                (
                    ".company",
                    ".issuer",
                    "[data-field='companyName']",
                    "td:nth-of-type(1)",
                ),
            )
        topic = str(container.get("data-topic") or "").strip()
        if not topic:
            topic = _tag_text(
                container,
                (
                    ".document-type",
                    ".topic",
                    "[data-field='reportingTopicName']",
                    "td:nth-of-type(2)",
                ),
            )
        published_text = str(
            container.get("data-publication-date") or ""
        ).strip() or _tag_text(
            container,
            (
                ".publication-date",
                "[data-field='datePublication']",
                "time",
                "td:nth-of-type(3)",
            ),
        )
        title = str(container.get("data-title") or "").strip()
        if not title:
            title = _tag_text(
                container,
                (".title", ".document-title", "h2", "h3"),
            )
        detail_link = container.select_one(
            "a.detail[href], a[data-detail-url][href]"
        )
        detail_url = str(
            container.get("data-detail-url")
            or (detail_link.get("href") if detail_link else "")
            or base_url
        )
        detail_url = urljoin(base_url, detail_url)

        isins: list[str] = []
        markets: list[str] = []
        for node in container.select("[data-isin], .isin"):
            value = str(node.get("data-isin") or node.get_text(" ", strip=True))
            isins.extend(re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", value))
        raw_isins = str(container.get("data-isin") or "")
        isins.extend(re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", raw_isins))
        for node in container.select("[data-market], .market"):
            market = str(
                node.get("data-market") or node.get_text(" ", strip=True)
            ).strip()
            if market:
                markets.append(market)
        raw_market = str(container.get("data-market") or "").strip()
        if raw_market:
            markets.append(raw_market)

        files = _extract_html_files(
            container,
            base_url=detail_url,
            api_root=api_root,
        )
        if not record_id:
            record_id = (
                files[0].file_data_id
                if files
                else f"html-{index}-{company}-{published_text}"
            )
        if company:
            observed_fields.add("companyName")
        if topic:
            observed_fields.add("reportingTopicName")
        if published_text:
            observed_fields.add("datePublication")
        if files:
            observed_fields.add("mainDocuments")
        if isins:
            observed_fields.add("isinCodes")
        if not company and not files:
            continue
        notices.append(
            BelgiumNotice(
                record_id=record_id,
                company_name=company,
                company_number=str(
                    container.get("data-company-number") or ""
                ).strip() or None,
                nationality=str(
                    container.get("data-nationality") or ""
                ).strip() or None,
                reporting_topic=topic,
                published_date=_parse_date(published_text),
                received_date=_parse_date(
                    container.get("data-received-date")
                ),
                lei=str(container.get("data-lei") or "").strip() or None,
                isin_codes=tuple(dict.fromkeys(isins)),
                markets=tuple(dict.fromkeys(markets)),
                document_title=title,
                detail_url=detail_url,
                files=files,
            )
        )

    total_count = None
    total_node = soup.select_one("[data-result-count], .result-count")
    if total_node:
        raw_count = str(
            total_node.get("data-result-count")
            or total_node.get_text(" ", strip=True)
        )
        count_match = re.search(r"\d+", raw_count.replace(" ", ""))
        if count_match:
            total_count = int(count_match.group())
    next_link = soup.select_one(
        "a[rel='next'][href], .pager__item--next a[href], a.next[href]"
    )
    return ParsedBelgiumPage(
        notices=tuple(notices),
        total_count=total_count,
        next_url=(
            urljoin(base_url, str(next_link.get("href")))
            if next_link
            else None
        ),
        fields=tuple(sorted(observed_fields)),
    )


def parse_api_notice(
    item: dict[str, Any],
    *,
    public_url: str,
    api_root: str,
) -> BelgiumNotice:
    files: list[BelgiumFile] = []
    for raw in (*item.get("mainDocuments", []), *item.get("attachments", [])):
        file_data_id = str(raw.get("fileDataId") or "").strip()
        filename = str(raw.get("originalFileName") or raw.get("title") or "")
        detected_type = _file_type(str(raw.get("fileType") or ""), filename)
        if not file_data_id or not detected_type:
            continue
        size = raw.get("size")
        try:
            size_kb = int(size) if size is not None else None
        except (TypeError, ValueError):
            size_kb = None
        files.append(
            BelgiumFile(
                file_data_id=file_data_id,
                language=str(raw.get("language") or ""),
                title=str(raw.get("title") or ""),
                original_filename=filename,
                size_kb=size_kb,
                file_type=detected_type,
                download_url=_download_url(api_root, file_data_id),
            )
        )
    isin_codes: list[str] = []
    markets: list[str] = []
    for raw_isin in item.get("isinCodes", []):
        code = str(raw_isin.get("code") or "").strip().upper()
        market = str(raw_isin.get("market") or "").strip()
        if code:
            isin_codes.append(code)
        if market:
            markets.append(market)
    return BelgiumNotice(
        record_id=str(item.get("requiredReportingTopicId") or "").strip(),
        company_name=str(item.get("companyName") or "").strip(),
        company_number=str(item.get("companyNumber") or "").strip() or None,
        nationality=str(item.get("nationality") or "").strip() or None,
        reporting_topic=str(item.get("reportingTopicName") or "").strip(),
        published_date=_parse_date(item.get("datePublication")),
        received_date=_parse_date(item.get("dateReceived")),
        lei=str(item.get("lei") or "").strip() or None,
        isin_codes=tuple(dict.fromkeys(isin_codes)),
        markets=tuple(dict.fromkeys(markets)),
        document_title=str(item.get("documentTitle") or "").strip(),
        # STORI currently expands notices in-place and exposes no detail route.
        detail_url=public_url,
        files=tuple(files),
    )


def issuer_notice_match_score(issuer: Issuer, notice: BelgiumNotice) -> float:
    if issuer.isin and issuer.isin.upper() in notice.isin_codes:
        return 100.0
    issuer_name = _normalize(issuer.name)
    company_name = _normalize(notice.company_name)
    if issuer_name and company_name:
        if issuer_name == company_name:
            return 85.0
        if issuer_name in company_name or company_name in issuer_name:
            return 72.0
        issuer_words = set(issuer_name.split())
        company_words = set(company_name.split())
        if issuer_words and company_words:
            overlap = len(issuer_words & company_words) / len(
                issuer_words | company_words
            )
            if overlap >= 0.6:
                return 60.0 + overlap * 10.0
    symbol = _normalize(issuer.symbol)
    if symbol and (
        symbol == company_name
        or symbol in company_name.split()
        or company_name.replace(" ", "").startswith(symbol)
    ):
        return 55.0
    return 0.0


def match_issuer_notice(issuer: Issuer, notice: BelgiumNotice) -> bool:
    return issuer_notice_match_score(issuer, notice) >= 55.0


class BelgiumFsmaStoriConnector(Connector):
    source_name = "fsma_stori"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str = DEFAULT_PUBLIC_URL,
        market: str = "Euronext Brussels",
        rate_limit_seconds: float = 0.2,
        lookback_days: int = 900,
        timeout: int = 30,
        api_base_url: str | None = None,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.market = market
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.api_origin = (
            api_base_url.rstrip("/")
            if api_base_url
            else DEFAULT_API_ORIGIN
        )
        self.api_root = f"{self.api_origin}/api/v1/en/stori"
        self.state = ConnectorState.READY
        self.last_error = None
        self._last_request_at = 0.0
        self._document_types: list[dict[str, Any]] | None = None
        self._financial_type_ids: dict[str, str] = {}
        self._companies: list[dict[str, Any]] | None = None
        self._market_notices: tuple[BelgiumNotice, ...] | None = None

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        delay = self.rate_limit_seconds - elapsed
        if delay > 0:
            time.sleep(delay)

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        self._wait()
        response = self.session.get(url, timeout=self.timeout, **kwargs)
        self._last_request_at = time.monotonic()
        return response

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        self._wait()
        response = self.session.post(url, timeout=self.timeout, **kwargs)
        self._last_request_at = time.monotonic()
        return response

    @staticmethod
    def _json(response: requests.Response) -> Any:
        try:
            return response.json()
        except (ValueError, AttributeError):
            return json.loads(response.text)

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
            excerpt = " ".join(response.text[:300].split())
        return EndpointAttempt(
            name=name,
            base_url=urlparse(url).scheme + "://" + urlparse(url).netloc,
            dataset="stori",
            endpoint=url,
            method=method,
            http_status=response.status_code if response is not None else None,
            success=success,
            total_count=total_count,
            response_excerpt=excerpt,
            error=error,
        )

    def _update_api_origin_from_html(self, html: str) -> None:
        match = re.search(
            r'"vueToolsApi"\s*:\s*"([^"]+)"',
            html,
            flags=re.IGNORECASE,
        )
        if not match:
            return
        discovered = match.group(1).replace("\\/", "/").rstrip("/")
        if discovered.startswith("http"):
            self.api_origin = discovered
            self.api_root = f"{discovered}/api/v1/en/stori"

    def _get_document_types(
        self,
        attempts: list[EndpointAttempt] | None = None,
    ) -> list[dict[str, Any]]:
        if self._document_types is not None:
            return self._document_types
        url = f"{self.api_root}/document-type"
        try:
            response = self._get(url)
            response.raise_for_status()
            data = self._json(response)
            valid = isinstance(data, list) and bool(data)
            if attempts is not None:
                attempts.append(
                    self._attempt(
                        name="stori_document_types",
                        url=url,
                        method="GET",
                        response=response,
                        success=valid,
                        total_count=len(data) if isinstance(data, list) else None,
                    )
                )
            if not valid:
                raise ConnectorError("Liste des types STORI vide ou invalide")
            self._document_types = data
            for item in data:
                type_id = str(item.get("documentTypeId") or "").strip()
                name = _normalize(str(item.get("localisedName") or ""))
                if not type_id:
                    continue
                if any(
                    marker in name
                    for marker in (
                        "half yearly financial report",
                        "half year financial report",
                        "rapport financier semestriel",
                        "halfjaarlijks financieel verslag",
                    )
                ):
                    self._financial_type_ids["half_year"] = type_id
                elif any(
                    marker in name
                    for marker in (
                        "annual financial report",
                        "rapport financier annuel",
                        "jaarlijks financieel verslag",
                    )
                ):
                    self._financial_type_ids["annual"] = type_id
            return data
        except Exception as exc:
            if attempts is not None:
                attempts.append(
                    self._attempt(
                        name="stori_document_types",
                        url=url,
                        method="GET",
                        success=False,
                        error=str(exc),
                    )
                )
            raise

    def _get_companies(
        self,
        attempts: list[EndpointAttempt] | None = None,
    ) -> list[dict[str, Any]]:
        if self._companies is not None:
            return self._companies
        url = f"{self.api_root}/companies/abbreviated-name"
        try:
            response = self._get(url)
            response.raise_for_status()
            data = self._json(response)
            valid = isinstance(data, list) and bool(data)
            if attempts is not None:
                attempts.append(
                    self._attempt(
                        name="stori_companies",
                        url=url,
                        method="GET",
                        response=response,
                        success=valid,
                        total_count=len(data) if isinstance(data, list) else None,
                    )
                )
            if not valid:
                raise ConnectorError("Liste des émetteurs STORI vide")
            self._companies = data
            return data
        except Exception as exc:
            if attempts is not None:
                attempts.append(
                    self._attempt(
                        name="stori_companies",
                        url=url,
                        method="GET",
                        success=False,
                        error=str(exc),
                    )
                )
            raise

    def _post_result(
        self,
        payload: dict[str, Any],
        *,
        attempts: list[EndpointAttempt] | None = None,
        name: str = "stori_result",
    ) -> tuple[list[BelgiumNotice], int]:
        url = f"{self.api_root}/result"
        try:
            response = self._post(url, json=payload)
            response.raise_for_status()
            data = self._json(response)
            raw_items = data.get("storiResultItems", [])
            total_count = int(data.get("resultCount") or len(raw_items))
            notices = [
                parse_api_notice(
                    item,
                    public_url=self.base_url,
                    api_root=self.api_root,
                )
                for item in raw_items
                if isinstance(item, dict)
            ]
            if attempts is not None:
                attempts.append(
                    self._attempt(
                        name=name,
                        url=url,
                        method="POST",
                        response=response,
                        success=bool(notices),
                        total_count=total_count,
                    )
                )
            return notices, total_count
        except Exception as exc:
            if attempts is not None:
                attempts.append(
                    self._attempt(
                        name=name,
                        url=url,
                        method="POST",
                        success=False,
                        error=str(exc),
                    )
                )
            raise

    def _type_id_for_query(self, query: str) -> str | None:
        normalized = _normalize(query)
        if any(
            marker in normalized
            for marker in (
                "half yearly",
                "half year",
                "semi annual",
                "semestriel",
                "halfjaarlijks",
            )
        ):
            return self._financial_type_ids.get(
                "half_year",
                HALF_YEAR_TYPE_ID,
            )
        if any(
            marker in normalized
            for marker in (
                "annual",
                "annuel",
                "jaarverslag",
                "esef",
                "xhtml",
                "zip",
                "pdf",
            )
        ):
            return self._financial_type_ids.get("annual", ANNUAL_TYPE_ID)
        return None

    def _fetch_pages(
        self,
        *,
        document_type_id: str | None = None,
        query: str | None = None,
        company_id: str | None = None,
        isin: str | None = None,
        page_size: int = 100,
        max_pages: int = 50,
        attempts: list[EndpointAttempt] | None = None,
    ) -> tuple[list[BelgiumNotice], int]:
        collected: list[BelgiumNotice] = []
        total_count = 0
        since = date.today() - timedelta(days=self.lookback_days)
        for page in range(max_pages):
            payload: dict[str, Any] = {
                "startRowIndex": page * page_size,
                "pageSize": page_size,
                "sortDirection": "Descending",
                "publicationStart": since.isoformat(),
            }
            if document_type_id:
                payload["documentTypeId"] = document_type_id
                payload["isDocumentTypeGroup"] = False
            if query and not document_type_id:
                payload["title"] = query
            if company_id:
                payload["companyId"] = company_id
            if isin:
                payload["isinCode"] = isin
            page_notices, total_count = self._post_result(
                payload,
                attempts=attempts,
                name=f"stori_result_page_{page + 1}",
            )
            collected.extend(page_notices)
            if not page_notices or len(collected) >= total_count:
                break
        unique = {notice.record_id: notice for notice in collected}
        return list(unique.values()), total_count

    def _load_html_fallback(
        self,
        attempts: list[EndpointAttempt] | None = None,
    ) -> list[BelgiumNotice]:
        notices: list[BelgiumNotice] = []
        url: str | None = self.base_url
        seen: set[str] = set()
        for page in range(5):
            if not url or url in seen:
                break
            seen.add(url)
            try:
                response = self._get(url)
                response.raise_for_status()
                parsed = parse_stori_html(
                    response.text,
                    base_url=url,
                    api_root=self.api_root,
                )
                if attempts is not None:
                    attempts.append(
                        self._attempt(
                            name=f"stori_html_page_{page + 1}",
                            url=url,
                            method="GET",
                            response=response,
                            success=bool(parsed.notices),
                            total_count=parsed.total_count,
                        )
                    )
                for notice in parsed.notices:
                    files = notice.files
                    if not files and notice.detail_url != url:
                        detail_response = self._get(notice.detail_url)
                        detail_response.raise_for_status()
                        files = parse_stori_detail_html(
                            detail_response.text,
                            detail_url=notice.detail_url,
                            api_root=self.api_root,
                        )
                        notice = BelgiumNotice(
                            record_id=notice.record_id,
                            company_name=notice.company_name,
                            company_number=notice.company_number,
                            nationality=notice.nationality,
                            reporting_topic=notice.reporting_topic,
                            published_date=notice.published_date,
                            received_date=notice.received_date,
                            lei=notice.lei,
                            isin_codes=notice.isin_codes,
                            markets=notice.markets,
                            document_title=notice.document_title,
                            detail_url=notice.detail_url,
                            files=files,
                        )
                    notices.append(notice)
                url = parsed.next_url
            except Exception as exc:
                if attempts is not None:
                    attempts.append(
                        self._attempt(
                            name=f"stori_html_page_{page + 1}",
                            url=url,
                            method="GET",
                            success=False,
                            error=str(exc),
                        )
                    )
                break
        return notices

    def _load_market_notices(self) -> tuple[BelgiumNotice, ...]:
        if self._market_notices is not None:
            return self._market_notices
        try:
            self._get_document_types()
            notices: list[BelgiumNotice] = []
            for type_id in (
                self._financial_type_ids.get("annual", ANNUAL_TYPE_ID),
                self._financial_type_ids.get("half_year", HALF_YEAR_TYPE_ID),
            ):
                found, _ = self._fetch_pages(document_type_id=type_id)
                notices.extend(found)
            if not notices:
                raise ConnectorError(
                    "L'API STORI n'a retourné aucune notice financière"
                )
            unique = {notice.record_id: notice for notice in notices}
            self._market_notices = tuple(unique.values())
            return self._market_notices
        except Exception as api_error:
            fallback = self._load_html_fallback()
            fallback = [
                notice for notice in fallback
                if any(
                    _financial_type(
                        notice.reporting_topic,
                        notice.document_title,
                        item.original_filename,
                    )
                    for item in notice.files
                )
            ]
            if fallback:
                self.mark_degraded(
                    f"API STORI indisponible, fallback HTML partiel: {api_error}"
                )
                self._market_notices = tuple(fallback)
                return self._market_notices
            self.mark_degraded(f"STORI inexploitable: {api_error}")
            raise ConnectorError(self.last_error or str(api_error)) from api_error

    def _market_matches(self, notice: BelgiumNotice) -> bool:
        if not notice.markets:
            return True
        normalized = {_normalize(value) for value in notice.markets}
        if self.market.casefold() == "euronext growth brussels":
            return bool(
                normalized
                & {
                    "alternext",
                    "euronext growth",
                    "euronext growth brussels",
                }
            )
        return "euronext brussels" in normalized

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        notices = self._load_market_notices()
        candidates: list[DocumentCandidate] = []
        for notice in notices:
            if not self._market_matches(notice):
                continue
            if not match_issuer_notice(issuer, notice):
                continue
            for file in notice.files:
                if file.file_type not in SUPPORTED_FILE_TYPES:
                    continue
                document_type = _financial_type(
                    notice.reporting_topic,
                    notice.document_title or file.title,
                    file.original_filename,
                )
                if not document_type:
                    continue
                title = (
                    file.title
                    or notice.document_title
                    or file.original_filename
                    or notice.reporting_topic
                )
                candidates.append(
                    DocumentCandidate(
                        title=title,
                        url=file.download_url,
                        published_date=notice.published_date,
                        document_type=document_type,
                        source=self.source_name,
                        source_document_id=file.file_data_id,
                        metadata={
                            "fsma_record_id": notice.record_id,
                            "stori_url": self.base_url,
                            "detail_url": notice.detail_url,
                            "home_member_state": "Belgium",
                            "company_number": notice.company_number,
                            "lei": notice.lei,
                            "isins": list(notice.isin_codes),
                            "issuer_isins": list(notice.isin_codes),
                            "issuer_name": notice.company_name,
                            "issuer_symbol": None,
                            "markets": list(notice.markets),
                            "file_format": file.file_type,
                            "filename": file.original_filename,
                            "language": file.language,
                        },
                    )
                )
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
        cutoff = since or (date.today() - timedelta(days=7))
        candidate_limit = max(1, limit or 1000)
        notices = [
            notice
            for notice in self._load_market_notices()
            if (
                notice.published_date is None
                or notice.published_date >= cutoff
            )
        ]
        self._scanned_notices = len(notices)
        candidates: list[DocumentCandidate] = []
        for notice in notices:
            for file in notice.files:
                if file.file_type not in SUPPORTED_FILE_TYPES:
                    continue
                document_type = _financial_type(
                    notice.reporting_topic,
                    notice.document_title or file.title,
                    file.original_filename,
                )
                if not document_type:
                    continue
                candidates.append(
                    DocumentCandidate(
                        title=(
                            file.title
                            or notice.document_title
                            or file.original_filename
                            or notice.reporting_topic
                        ),
                        url=file.download_url,
                        published_date=notice.published_date,
                        document_type=document_type,
                        source=self.source_name,
                        source_document_id=file.file_data_id,
                        metadata={
                            "fsma_record_id": notice.record_id,
                            "stori_url": self.base_url,
                            "detail_url": notice.detail_url,
                            "home_member_state": "Belgium",
                            "company_number": notice.company_number,
                            "lei": notice.lei,
                            "isins": list(notice.isin_codes),
                            "issuer_isins": list(notice.isin_codes),
                            "issuer_name": notice.company_name,
                            "issuer_symbol": None,
                            "markets": list(notice.markets),
                            "file_format": file.file_type,
                            "filename": file.original_filename,
                            "language": file.language,
                        },
                    )
                )
                if len(candidates) >= candidate_limit:
                    return candidates
        return candidates

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        candidate_limit = max(1, limit or 1000)
        pages = min(50, (candidate_limit + 99) // 100)
        return 1 + pages * 2

    @staticmethod
    def _notice_output(notice: BelgiumNotice) -> dict[str, Any]:
        return {
            "record_id": notice.record_id,
            "company_name": notice.company_name,
            "company_number": notice.company_number,
            "reporting_topic": notice.reporting_topic,
            "published_date": (
                notice.published_date.isoformat()
                if notice.published_date
                else None
            ),
            "received_date": (
                notice.received_date.isoformat()
                if notice.received_date
                else None
            ),
            "lei": notice.lei,
            "isin_codes": list(notice.isin_codes),
            "markets": list(notice.markets),
            "document_title": notice.document_title,
            "detail_url": notice.detail_url,
            "files": [
                {
                    "file_data_id": item.file_data_id,
                    "filename": item.original_filename,
                    "format": item.file_type,
                    "language": item.language,
                    "download_url": item.download_url,
                }
                for item in notice.files
            ],
        }

    def _probe_download(
        self,
        file: BelgiumFile,
        attempts: list[EndpointAttempt],
    ) -> bool:
        try:
            response = self._get(file.download_url, stream=True)
            response.raise_for_status()
            content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .casefold()
            )
            disposition = response.headers.get("Content-Disposition", "")
            valid = (
                content_type not in {"", "text/html"}
                or bool(disposition)
            )
            attempts.append(
                self._attempt(
                    name=f"stori_download_{file.file_type}",
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
                    name=f"stori_download_{file.file_type}",
                    url=file.download_url,
                    method="GET",
                    success=False,
                    error=str(exc),
                )
            )
            return False

    def diagnose(self) -> BelgiumSourceDiagnostic:
        attempts: list[EndpointAttempt] = []
        checks = {
            "stori_accessible": False,
            "public_search": False,
            "pagination": False,
            "real_notices": False,
            "download_links": False,
            "automatic_download": False,
        }
        page_status: int | None = None
        page_error: str | None = None
        try:
            page_response = self._get(self.base_url)
            page_status = page_response.status_code
            page_response.raise_for_status()
            self._update_api_origin_from_html(page_response.text)
            checks["stori_accessible"] = True
            attempts.append(
                self._attempt(
                    name="stori_public_page",
                    url=self.base_url,
                    method="GET",
                    response=page_response,
                    success=True,
                )
            )
        except Exception as exc:
            page_error = str(exc)
            attempts.append(
                self._attempt(
                    name="stori_public_page",
                    url=self.base_url,
                    method="GET",
                    success=False,
                    error=page_error,
                )
            )

        notices: list[BelgiumNotice] = []
        total_count: int | None = None
        api_error: str | None = None
        try:
            self._get_document_types(attempts)
            self._get_companies(attempts)
            payload = {
                "startRowIndex": 0,
                "pageSize": 10,
                "sortDirection": "Descending",
                "documentTypeId": self._financial_type_ids.get(
                    "annual",
                    ANNUAL_TYPE_ID,
                ),
                "isDocumentTypeGroup": False,
                "publicationStart": (
                    date.today() - timedelta(days=self.lookback_days)
                ).isoformat(),
            }
            notices, total_count = self._post_result(
                payload,
                attempts=attempts,
                name="stori_annual_listing",
            )
            checks["public_search"] = True
            checks["real_notices"] = bool(notices)
            if total_count and total_count > len(notices):
                next_payload = dict(payload)
                next_payload["startRowIndex"] = len(notices)
                page_two, _ = self._post_result(
                    next_payload,
                    attempts=attempts,
                    name="stori_pagination",
                )
                checks["pagination"] = bool(page_two)
            else:
                checks["pagination"] = bool(notices)
        except Exception as exc:
            api_error = str(exc)
            html_notices = self._load_html_fallback(attempts)
            notices = html_notices[:10]
            checks["public_search"] = bool(notices)
            checks["real_notices"] = bool(notices)
            checks["pagination"] = bool(
                any(
                    attempt.name.startswith("stori_html_page_2")
                    and attempt.success
                    for attempt in attempts
                )
            )

        files = [
            item
            for notice in notices
            for item in notice.files
            if item.file_type in SUPPORTED_FILE_TYPES
        ]
        checks["download_links"] = bool(files)
        probed_formats: set[str] = set()
        probe_results: list[bool] = []
        for file in files:
            if file.file_type in probed_formats:
                continue
            probed_formats.add(file.file_type)
            probe_results.append(self._probe_download(file, attempts))
        checks["automatic_download"] = any(probe_results)

        if (
            checks["stori_accessible"]
            and checks["public_search"]
            and checks["real_notices"]
            and checks["download_links"]
            and checks["automatic_download"]
        ):
            state = ConnectorState.READY
            error = None
        elif checks["stori_accessible"] or checks["public_search"]:
            state = ConnectorState.DEGRADED
            error = api_error or page_error or "Diagnostic STORI partiel"
        else:
            state = ConnectorState.UNAVAILABLE
            error = api_error or page_error or "STORI inaccessible"

        example = self._notice_output(notices[0]) if notices else None
        fields = (
            "requiredReportingTopicId",
            "companyName",
            "companyNumber",
            "nationality",
            "reportingTopicName",
            "datePublication",
            "dateReceived",
            "lei",
            "mainDocuments",
            "attachments",
            "isinCodes",
            "documentTitle",
        )
        return BelgiumSourceDiagnostic(
            source=self.source_name,
            state=state,
            called_url=self.base_url,
            api_url=self.api_root,
            http_status=page_status,
            total_count=total_count,
            detected_count=len(notices),
            fields=fields,
            formats=tuple(sorted({item.file_type for item in files})),
            example_notice=example,
            checks=checks,
            attempts=tuple(attempts),
            error=error,
        )

    def discover(self, query: str) -> BelgiumSourceDiscovery:
        attempts: list[EndpointAttempt] = []
        candidates: list[BelgiumEndpointCandidate] = []
        notices: list[BelgiumNotice] = []
        api_error: str | None = None
        total_count: int | None = None
        try:
            self._get_document_types(attempts)
            type_id = self._type_id_for_query(query)
            notices, total_count = self._fetch_pages(
                document_type_id=type_id,
                query=query,
                page_size=20,
                max_pages=1,
                attempts=attempts,
            )
            candidates.append(
                BelgiumEndpointCandidate(
                    url=f"{self.api_root}/result",
                    role="primary STORI regulated information search",
                    format="JSON",
                    pagination="startRowIndex/pageSize",
                    fields=(
                        "companyName",
                        "reportingTopicName",
                        "datePublication",
                        "mainDocuments",
                        "isinCodes",
                    ),
                    verified=bool(notices),
                    state=(
                        ConnectorState.READY
                        if notices
                        else ConnectorState.DEGRADED
                    ),
                    http_status=200,
                    records_count=total_count,
                )
            )
        except Exception as exc:
            api_error = str(exc)

        html_notices: list[BelgiumNotice] = []
        try:
            html_notices = self._load_html_fallback(attempts)
        except Exception:
            html_notices = []
        candidates.append(
            BelgiumEndpointCandidate(
                url=self.base_url,
                role="public STORI HTML fallback",
                format="HTML",
                pagination="next link when server-rendered",
                fields=(
                    "issuer",
                    "type",
                    "publication date",
                    "documents",
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
                        if attempt.name == "stori_html_page_1"
                    ),
                    None,
                ),
                records_count=len(html_notices) or None,
            )
        )
        if not notices and html_notices:
            query_text = _normalize(query)
            notices = [
                notice for notice in html_notices
                if query_text
                in _normalize(
                    f"{notice.reporting_topic} {notice.document_title}"
                )
            ]
        if not notices and api_error:
            LOGGER.warning("Découverte STORI API échouée: %s", api_error)
        return BelgiumSourceDiscovery(
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
    ) -> BelgiumIssuerResolution:
        attempts: list[EndpointAttempt] = []
        try:
            companies = self._get_companies(attempts)
        except Exception as exc:
            return BelgiumIssuerResolution(
                found=False,
                matched_name=None,
                company_id=None,
                isin=isin,
                stori_url=None,
                detail_url=None,
                home_member_state=None,
                fsma_record_id=None,
                match_score=0.0,
                attempts=tuple(attempts),
                error=str(exc),
            )

        normalized_name = _normalize(name)
        normalized_symbol = _normalize(symbol)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for company in companies:
            abbreviation = str(
                company.get("abbreviation")
                or company.get("localisedName")
                or ""
            )
            normalized_company = _normalize(abbreviation)
            score = 0.0
            if normalized_company == normalized_name:
                score = 90.0
            elif normalized_symbol and normalized_company == normalized_symbol:
                score = 80.0
            elif (
                normalized_symbol
                and normalized_company.replace(" ", "").startswith(
                    normalized_symbol
                )
            ):
                score = 75.0
            elif (
                normalized_name
                and (
                    normalized_name in normalized_company
                    or normalized_company in normalized_name
                )
            ):
                score = 70.0
            if score:
                ranked.append((score, company))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            return BelgiumIssuerResolution(
                found=False,
                matched_name=None,
                company_id=None,
                isin=isin,
                stori_url=None,
                detail_url=None,
                home_member_state=None,
                fsma_record_id=None,
                match_score=0.0,
                attempts=tuple(attempts),
                error="Émetteur introuvable dans la liste STORI",
            )

        score, company = ranked[0]
        company_id = str(company.get("companyId") or "")
        matched_name = str(
            company.get("abbreviation")
            or company.get("localisedName")
            or name
        )
        try:
            notices, _ = self._fetch_pages(
                company_id=company_id,
                page_size=100,
                max_pages=1,
                attempts=attempts,
            )
        except Exception as exc:
            return BelgiumIssuerResolution(
                found=False,
                matched_name=matched_name,
                company_id=company_id,
                isin=isin,
                stori_url=self.base_url,
                detail_url=None,
                home_member_state="Belgium",
                fsma_record_id=None,
                match_score=score,
                attempts=tuple(attempts),
                error=str(exc),
            )
        if isin:
            exact = [
                notice for notice in notices
                if isin.upper() in notice.isin_codes
            ]
            if exact:
                notices = exact
                score = 100.0
        notices.sort(
            key=lambda notice: notice.published_date or date.min,
            reverse=True,
        )
        selected = notices[0] if notices else None
        selected_isin = (
            isin
            or (
                selected.isin_codes[0]
                if selected and selected.isin_codes
                else None
            )
        )
        return BelgiumIssuerResolution(
            found=selected is not None,
            matched_name=matched_name,
            company_id=company_id,
            isin=selected_isin,
            stori_url=self.base_url,
            detail_url=selected.detail_url if selected else None,
            home_member_state="Belgium",
            fsma_record_id=selected.record_id if selected else None,
            match_score=score,
            attempts=tuple(attempts),
            error=None if selected else "Aucune notice STORI pour cet émetteur",
        )
