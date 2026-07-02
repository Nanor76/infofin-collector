from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from connectors.base import Connector, ConnectorState, DocumentCandidate
from models import Issuer


DEFAULT_DFSA_PAGE_URL = (
    "https://www.dfsa.dk/financial-themes/capital-market/company-announcements"
)
DEFAULT_EXTENSION_ORIGIN = "https://appft.gold.extension.gopublic.dk"
DEFAULT_MODULE_ID = "9217fa13-5d9a-46c6-9921-69ee7e6cfaf6"

PERIODIC_CATEGORIES = {
    "annual_financial_report",
    "half_year_financial_report",
    "interim_report",
    "quarterly_report",
    "year_end_report",
}

NEGATIVE_TERMS = (
    "major shareholder",
    "major holding",
    "manager transaction",
    "managers transaction",
    "manager's transaction",
    "managerial responsibilities",
    "insider",
    "voting rights",
    "share buyback",
    "share buy-back",
    "aktietilbagekøb",
    "capital increase",
    "rights issue",
    "prospectus",
    "tender offer",
    "bond",
    "notes",
    "debt",
    "change in board",
    "general meeting",
    "financial calendar",
    "remuneration report",
    "corporate governance",
    "corporate announcement",
    "press release",
)

POSITIVE_RULES = (
    ("year_end_report", ("year-end report", "year end report", "årsregnskabsmeddelelse")),
    (
        "half_year_financial_report",
        (
            "half-year report",
            "half year report",
            "half-yearly financial report",
            "half yearly financial report",
            "halvårsrapport",
            "halvår",
            "halvaar",
        ),
    ),
    ("quarterly_report", ("quarterly report", "kvartalsrapport")),
    ("interim_report", ("interim report", "delårsrapport")),
    (
        "annual_financial_report",
        ("annual financial report", "annual report", "årsrapport", "årsregnskab"),
    ),
    ("interim_report", ("financial report", "finansiel rapport")),
)

FORMAT_TERMS = ("esef", "xhtml", "xbrl", "zip", "pdf")


def _normalize(value: str | None) -> str:
    text = (value or "").casefold()
    text = re.sub(r"[^\wæøå]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    for pattern in (
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw, pattern)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def classify_denmark_document(
    title: str,
    category: str = "",
    url: str = "",
) -> tuple[str, str, list[str], list[str]]:
    haystack = _normalize(" ".join((title, category, url)))
    negative = [term for term in NEGATIVE_TERMS if _normalize(term) in haystack]
    matched_positive: list[str] = []
    for _, terms in POSITIVE_RULES:
        matched_positive.extend(term for term in terms if _normalize(term) in haystack)
    matched_positive.extend(term for term in FORMAT_TERMS if term in haystack)

    if negative:
        return (
            "other_regulatory_announcement",
            f"Explicit exclusion term: {negative[0]}",
            sorted(set(matched_positive)),
            sorted(set(negative)),
        )

    for classification, terms in POSITIVE_RULES:
        detected = [term for term in terms if _normalize(term) in haystack]
        if detected:
            return (
                classification,
                f"Periodic report term: {detected[0]}",
                sorted(set(matched_positive)),
                [],
            )

    return (
        "other_regulatory_announcement",
        "No periodic financial-report term detected",
        sorted(set(matched_positive)),
        [],
    )


def extract_denmark_date_info(
    title: str,
    published_raw: str | None,
    period_raw: str | None = None,
    filenames: tuple[str, ...] = (),
) -> dict[str, Any]:
    published_datetime = _parse_datetime(published_raw)
    published_at = published_datetime.date() if published_datetime else None
    period_end: date | None = None
    reporting_year: int | None = None
    detected_period_raw = period_raw
    reason = "No reporting-period date detected"
    confidence = "low"
    search_text = " ".join((title, period_raw or "", *filenames))

    iso_matches = re.findall(r"\b(20\d{2})[-_](0[1-9]|1[0-2])[-_](0[1-9]|[12]\d|3[01])\b", search_text)
    for year, month, day in iso_matches:
        try:
            period_end = date(int(year), int(month), int(day))
            reporting_year = period_end.year
            detected_period_raw = detected_period_raw or period_end.isoformat()
            reason = "Reporting period extracted from an explicit ISO date"
            confidence = "high"
            break
        except ValueError:
            continue

    if period_end is None:
        year_matches = re.findall(r"\b(20\d{2})(?:\s*/\s*(\d{2,4}))?\b", search_text)
        if year_matches:
            first_year, second_year = year_matches[-1]
            if second_year:
                reporting_year = int(
                    second_year if len(second_year) == 4 else first_year[:2] + second_year
                )
            else:
                reporting_year = int(first_year)
            reason = "Reporting year extracted from report title or filename"
            confidence = "medium"
            detected_period_raw = detected_period_raw or str(reporting_year)

    return {
        "published_at": published_at,
        "period_end_date": period_end,
        "reporting_year": reporting_year,
        "source_publication_date_raw": published_raw,
        "source_period_date_raw": detected_period_raw,
        "date_confidence": confidence,
        "date_extraction_reason": reason,
    }


@dataclass(frozen=True)
class DenmarkFile:
    filename: str
    download_url: str
    file_type: str


@dataclass(frozen=True)
class DenmarkNotice:
    record_id: str
    title: str
    issuer_name: str
    published_raw: str
    detail_url: str
    category: str = ""
    registration_raw: str | None = None
    issuer_isins: tuple[str, ...] = ()
    issuer_symbol: str | None = None
    national_business_id: str | None = None
    lei: str | None = None
    files: tuple[DenmarkFile, ...] = ()


@dataclass(frozen=True)
class EndpointAttempt:
    method: str
    url: str
    status_code: int | None
    note: str = ""


@dataclass
class DenmarkSourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    http_status: int | None
    method_used: str
    total_count: int
    fields: list[str] = field(default_factory=list)
    example_notice: dict[str, Any] | None = None
    formats: list[str] = field(default_factory=list)
    attempts: list[EndpointAttempt] = field(default_factory=list)
    error: str | None = None


@dataclass
class DenmarkSourceDiscovery:
    source: str
    state: str
    query: str
    notices: list[DenmarkNotice] = field(default_factory=list)
    candidates: list[DocumentCandidate] = field(default_factory=list)
    attempts: list[EndpointAttempt] = field(default_factory=list)
    error: str | None = None


@dataclass
class DenmarkIssuerResolution:
    found: bool
    matched_name: str | None = None
    denmark_dfsa_issuer_url: str | None = None
    denmark_dfsa_record_id: str | None = None
    denmark_dfsa_detail_url: str | None = None
    denmark_home_member_state: str | None = None
    denmark_nasdaq_company_url: str | None = None
    denmark_pea_country_check: str | None = None
    match_score: int = 0
    attempts: list[EndpointAttempt] = field(default_factory=list)
    error: str | None = None


def _supported_file_type(filename: str, url: str) -> str | None:
    path = urlparse(url).path.casefold()
    combined = f"{filename.casefold()} {path}"
    for extension, file_type in (
        (".pdf", "PDF"),
        (".xhtml", "XHTML"),
        (".html", "XHTML"),
        (".xml", "XML/XBRL"),
        (".xbrl", "XML/XBRL"),
        (".xbri", "XML/XBRL"),
        (".zip", "ZIP/XBRL"),
    ):
        if extension in combined:
            return file_type
    return None


def _is_confirmed_danish_issuer(notice: DenmarkNotice) -> bool:
    return bool(
        notice.issuer_name
        and notice.national_business_id
        and re.fullmatch(r"\d{8}", notice.national_business_id.strip())
    )


def _flatten_detail_values(payload: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    values: dict[str, str] = {}
    links: list[dict[str, Any]] = []
    for section in payload.get("sections", []):
        if not isinstance(section, dict):
            continue
        section_name = _normalize(str(section.get("heading") or ""))
        for element in section.get("elements", []):
            if not isinstance(element, dict):
                continue
            key_node = element.get("key")
            value_node = element.get("value")
            key = (
                str(key_node.get("name") or "")
                if isinstance(key_node, dict)
                else str(key_node or "")
            )
            if not isinstance(value_node, dict):
                continue
            if value_node.get("type") == "link" and value_node.get("url"):
                links.append(value_node)
                continue
            raw_value = value_node.get("value")
            if not isinstance(raw_value, (str, int, float)):
                continue
            value = str(raw_value)
            normalized_key = _normalize(key)
            if not normalized_key and section_name == "notification":
                values["regulatory category"] = value
            elif normalized_key:
                values[f"{section_name} {normalized_key}".strip()] = value
                values.setdefault(normalized_key, value)
    return values, links


def parse_denmark_search_json(payload: dict[str, Any], detail_base_url: str) -> list[DenmarkNotice]:
    rows = payload.get("data", {}).get("rows", [])
    notices: list[DenmarkNotice] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        record_id = str(row.get("id") or row.get("AnnouncementId") or "").strip()
        title = str(row.get("HeadlineColumn") or row.get("headline") or "").strip()
        issuer_name = str(row.get("IssuerColumn") or row.get("issuer") or "").strip()
        published_raw = str(row.get("PublicationDateColumn") or row.get("published") or "").strip()
        if not record_id or not title:
            continue
        raw_isins = str(
            row.get("IsinColumn")
            or row.get("ISINColumn")
            or row.get("isin")
            or ""
        )
        notices.append(
            DenmarkNotice(
                record_id=record_id,
                title=title,
                issuer_name=issuer_name,
                published_raw=published_raw,
                registration_raw=str(row.get("RegistrationDateColumn") or "").strip() or None,
                category=str(row.get("CategoryColumn") or "").strip(),
                issuer_isins=tuple(
                    sorted(
                        set(
                            re.findall(
                                r"\b[A-Z]{2}[A-Z0-9]{9}\d\b",
                                raw_isins.upper(),
                            )
                        )
                    )
                ),
                issuer_symbol=str(
                    row.get("SymbolColumn") or row.get("symbol") or ""
                ).strip()
                or None,
                detail_url=f"{detail_base_url.rstrip('/')}/{record_id}",
            )
        )
    return notices


def parse_denmark_dfsa_html(html: str, detail_base_url: str) -> list[DenmarkNotice]:
    soup = BeautifulSoup(html, "html.parser")
    notices: list[DenmarkNotice] = []
    for row in soup.select("[data-record-id], tr"):
        record_id = row.get("data-record-id") or ""
        link = row.select_one("a[href]")
        title_node = row.select_one("[data-field='headline'], .headline") or link
        issuer_node = row.select_one("[data-field='issuer'], .issuer")
        published_node = row.select_one("[data-field='published'], time, .published")
        if not record_id and link:
            match = re.search(r"/details/(\d+)", link.get("href", ""))
            record_id = match.group(1) if match else ""
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not record_id or not title:
            continue
        notices.append(
            DenmarkNotice(
                record_id=str(record_id),
                title=title,
                issuer_name=issuer_node.get_text(" ", strip=True) if issuer_node else "",
                published_raw=published_node.get_text(" ", strip=True) if published_node else "",
                detail_url=urljoin(detail_base_url + "/", link.get("href", "")) if link else (
                    f"{detail_base_url.rstrip('/')}/{record_id}"
                ),
            )
        )
    return notices


def parse_denmark_detail_json(
    payload: dict[str, Any],
    fallback: DenmarkNotice,
    origin: str,
) -> DenmarkNotice:
    values, raw_links = _flatten_detail_values(payload)
    heading = str(payload.get("heading") or payload.get("title") or fallback.title).strip()
    category = (
        values.get("regulatory category")
        or values.get("type")
        or values.get("announcement type")
        or values.get("category")
        or fallback.category
    )
    issuer_name = (
        values.get("issuer company")
        or values.get("company")
        or values.get("issuer")
        or fallback.issuer_name
    )
    published_raw = (
        values.get("time published")
        or values.get("published")
        or values.get("publication date")
        or fallback.published_raw
    )
    registration_raw = (
        values.get("time registration time")
        or values.get("registration time")
        or fallback.registration_raw
    )
    isins: list[str] = []
    for key, value in values.items():
        if "isin" in key:
            isins.extend(re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", value.upper()))

    files: list[DenmarkFile] = []
    seen_urls: set[str] = set()
    for link in raw_links:
        raw_url = str(link.get("url") or "").strip()
        if not raw_url:
            continue
        download_url = urljoin(origin + "/", raw_url)
        if download_url in seen_urls:
            continue
        filename = str(
            link.get("filename")
            or link.get("name")
            or link.get("label")
            or link.get("title")
            or urlparse(download_url).path.rsplit("/", 1)[-1]
        ).strip()
        filename = re.sub(r"\s+\([^)]+\)\s*$", "", unquote(filename))
        file_type = _supported_file_type(filename, download_url)
        if not file_type:
            continue
        seen_urls.add(download_url)
        files.append(DenmarkFile(filename=filename, download_url=download_url, file_type=file_type))

    return DenmarkNotice(
        record_id=fallback.record_id,
        title=heading,
        issuer_name=issuer_name,
        published_raw=published_raw,
        detail_url=fallback.detail_url,
        category=category,
        registration_raw=registration_raw,
        issuer_isins=tuple(sorted(set(isins))),
        issuer_symbol=fallback.issuer_symbol,
        national_business_id=(
            values.get("issuer national business id")
            or values.get("national business id")
        ),
        lei=values.get("issuer lei code") or values.get("lei code") or values.get("lei"),
        files=tuple(files),
    )


class DenmarkDfsaOamConnector(Connector):
    supports_source_first = True
    source_name = "dfsa_oam"

    def __init__(
        self,
        session: requests.Session,
        base_url: str = DEFAULT_DFSA_PAGE_URL,
        nasdaq_listed_companies_url: str | None = None,
        market: str = "Nasdaq Copenhagen",
        lookback_days: int = 30,
        rate_limit_seconds: float = 0.5,
        timeout: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        self.session = session
        self.market = market
        self.base_url = base_url.rstrip("/")
        self.nasdaq_listed_companies_url = nasdaq_listed_companies_url
        self.lookback_days = lookback_days
        self.rate_limit_seconds = rate_limit_seconds
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.state = ConnectorState.UNAVAILABLE
        self.attempts: list[EndpointAttempt] = []
        self.extension_origin: str | None = None
        self.module_id: str | None = None
        self._last_request_at = 0.0
        self._scanned_notices = 0
        self._details_visited = 0

    @property
    def scanned_notices(self) -> int:
        return self._scanned_notices

    @property
    def details_visited(self) -> int:
        return self._details_visited

    @property
    def search_url(self) -> str:
        self._ensure_endpoints()
        return f"{self.extension_origin}/api/{self.module_id}/search"

    @property
    def detail_base_url(self) -> str:
        self._ensure_endpoints()
        return f"{self.extension_origin}/api/{self.module_id}/details"

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _get(self, url: str) -> requests.Response:
        self._wait()
        response = self.session.get(url, timeout=self.timeout, verify=self.verify_ssl)
        self._last_request_at = time.monotonic()
        self.attempts.append(EndpointAttempt("GET", url, response.status_code))
        return response

    def _post(self, url: str, payload: dict[str, Any]) -> requests.Response:
        self._wait()
        response = self.session.post(
            url,
            json=payload,
            timeout=self.timeout,
            verify=self.verify_ssl,
            headers={"Accept-Language": "en", "Accept": "application/json"},
        )
        self._last_request_at = time.monotonic()
        self.attempts.append(EndpointAttempt("POST", url, response.status_code))
        return response

    def _ensure_endpoints(self) -> None:
        if self.extension_origin and self.module_id:
            return

        parsed = urlparse(self.base_url)
        if parsed.netloc == urlparse(DEFAULT_EXTENSION_ORIGIN).netloc:
            self.extension_origin = f"{parsed.scheme}://{parsed.netloc}"
            module_match = re.search(r"/api/([^/]+)/", parsed.path)
            self.module_id = module_match.group(1) if module_match else DEFAULT_MODULE_ID
            return

        try:
            response = self._get(self.base_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            iframe = soup.select_one("iframe[src*='config=']")
            if iframe:
                src = urljoin(self.base_url + "/", iframe.get("src", ""))
                config_match = re.search(r"[?&]config=([^&]+)", src)
                if config_match:
                    encoded = config_match.group(1)
                    padded = encoded + "=" * (-len(encoded) % 4)
                    config = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
                    self.module_id = str(config.get("moduleId") or "").strip()
                    iframe_url = urlparse(src)
                    self.extension_origin = f"{iframe_url.scheme}://{iframe_url.netloc}"
        except (requests.RequestException, ValueError, json.JSONDecodeError):
            pass

        self.extension_origin = self.extension_origin or DEFAULT_EXTENSION_ORIGIN
        self.module_id = self.module_id or DEFAULT_MODULE_ID

    def _search(
        self,
        query: str,
        since: date | None,
        until: date | None,
        limit: int,
    ) -> tuple[list[DenmarkNotice], int]:
        page = 1
        page_size = max(1, min(100, limit))
        notices: list[DenmarkNotice] = []
        total_count = 0
        while len(notices) < limit:
            filters: list[dict[str, Any]] = []
            if since or until:
                filters.append(
                    {
                        "type": "daterange",
                        "key": "PublicationDateFilter",
                        "min": (since or date(1990, 1, 1)).isoformat(),
                        "max": (until or date.today()).isoformat(),
                    }
                )
            payload = {
                "query": query,
                "filters": filters,
                "page": page,
                "pageSize": page_size,
                "sorting": {"key": "PublicationDateColumn", "direction": "descending"},
            }
            response = self._post(self.search_url, payload)
            response.raise_for_status()
            data = response.json()
            page_notices = parse_denmark_search_json(data, self.detail_base_url)
            notices.extend(page_notices)
            paging = data.get("paging", {})
            total_count = int(paging.get("totalCount") or len(notices))
            total_pages = int(paging.get("totalPages") or 1)
            if not page_notices or page >= total_pages:
                break
            page += 1
        self._scanned_notices += min(len(notices), limit)
        self.state = ConnectorState.READY
        return notices[:limit], total_count

    def _notice_candidate(self, notice: DenmarkNotice) -> DocumentCandidate:
        classification, reason, positive, negative = classify_denmark_document(
            notice.title, notice.category
        )
        dates = extract_denmark_date_info(notice.title, notice.published_raw)
        return DocumentCandidate(
            title=notice.title,
            url=notice.detail_url,
            published_date=dates["published_at"],
            document_type=classification,
            source="dfsa_oam",
            source_document_id=notice.record_id,
            metadata={
                "issuer_name": notice.issuer_name,
                "issuer_isins": list(notice.issuer_isins),
                "issuer_symbol": notice.issuer_symbol,
                "record_id": notice.record_id,
                "detail_url": notice.detail_url,
                "category": notice.category,
                "denmark_pea_country_check": "eu_candidate",
            },
            classification=classification,
            classification_reason=reason,
            matched_positive_terms=positive,
            matched_negative_terms=negative,
            **dates,
        )

    def _load_detail(self, notice: DenmarkNotice) -> DenmarkNotice:
        response = self._get(notice.detail_url)
        self._details_visited += 1
        response.raise_for_status()
        return parse_denmark_detail_json(response.json(), notice, self.extension_origin or "")

    def _detailed_candidates(self, notice: DenmarkNotice) -> list[DocumentCandidate]:
        detailed = self._load_detail(notice)
        confirmed_denmark = _is_confirmed_danish_issuer(detailed)
        filenames = tuple(item.filename for item in detailed.files)
        dates = extract_denmark_date_info(
            detailed.title,
            detailed.published_raw,
            filenames=filenames,
        )
        candidates: list[DocumentCandidate] = []
        for item in detailed.files:
            classification, reason, positive, negative = classify_denmark_document(
                detailed.title, detailed.category, item.download_url
            )
            if re.match(r"(?i)^release(?:[_\s.-]|$)", item.filename):
                classification = "other_regulatory_announcement"
                reason = "Auxiliary release attachment, not the periodic report"
                negative = sorted(set([*negative, "release attachment"]))
            elif re.match(
                r"(?i)^(?:company announcement|selskabsmeddelelse)(?:[_\s.-]|$)",
                item.filename,
            ):
                classification = "other_regulatory_announcement"
                reason = "Auxiliary company-announcement attachment, not the periodic report"
                negative = sorted(
                    set([*negative, "company announcement attachment"])
                )
            metadata: dict[str, Any] = {
                "issuer_name": detailed.issuer_name,
                "issuer_isins": list(detailed.issuer_isins),
                "issuer_symbol": detailed.issuer_symbol,
                "record_id": detailed.record_id,
                "detail_url": detailed.detail_url,
                "denmark_dfsa_issuer_url": self.base_url,
                "category": detailed.category,
                "filename": item.filename,
                "file_type": item.file_type,
                "national_business_id": detailed.national_business_id,
                "lei": detailed.lei,
                "denmark_pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
            }
            if confirmed_denmark:
                metadata["issuer_country"] = "Denmark"
                metadata["denmark_home_member_state"] = "Denmark"
            candidates.append(
                DocumentCandidate(
                    title=f"{detailed.title} - {item.filename}",
                    url=item.download_url,
                    published_date=dates["published_at"],
                    document_type=classification,
                    source="dfsa_oam",
                    source_document_id=f"{detailed.record_id}:{item.filename}",
                    metadata=metadata,
                    classification=classification,
                    classification_reason=reason,
                    matched_positive_terms=positive,
                    matched_negative_terms=negative,
                    **dates,
                )
            )
        return candidates

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        until = date.today()
        effective_since = since or (until - timedelta(days=self.lookback_days))
        notices, _ = self._search("", effective_since, until, limit or 100)
        return [self._notice_candidate(notice) for notice in notices]

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        notice = DenmarkNotice(
            record_id=str(candidate.metadata.get("record_id") or candidate.source_document_id or ""),
            title=candidate.title,
            issuer_name=str(candidate.metadata.get("issuer_name") or ""),
            published_raw=candidate.source_publication_date_raw or "",
            detail_url=str(candidate.metadata.get("detail_url") or candidate.url),
            category=str(candidate.metadata.get("category") or ""),
            issuer_isins=tuple(candidate.metadata.get("issuer_isins") or ()),
            issuer_symbol=candidate.metadata.get("issuer_symbol"),
        )
        return self._detailed_candidates(notice)

    def search_documents_for_issuer(
        self,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        notices: list[DenmarkNotice] = []
        seen_records: set[str] = set()
        for query in dict.fromkeys(
            value for value in (issuer.isin, issuer.name, issuer.symbol) if value
        ):
            found, _ = self._search(query, None, date.today(), 25)
            for notice in found:
                if notice.record_id not in seen_records:
                    notices.append(notice)
                    seen_records.add(notice.record_id)
        issuer_name = _normalize(issuer.name)
        symbol = _normalize(issuer.symbol)
        matching = [
            notice
            for notice in notices
            if (
                issuer.isin
                and issuer.isin.upper() in {item.upper() for item in notice.issuer_isins}
            )
            or (issuer_name and issuer_name in _normalize(notice.issuer_name))
            or (symbol and symbol in _normalize(f"{notice.issuer_name} {notice.title}"))
        ]
        candidates: list[DocumentCandidate] = []
        for notice in matching[:25]:
            candidates.extend(self._detailed_candidates(notice))
        return candidates

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def _nasdaq_company_url(self, issuer: Issuer) -> str | None:
        if not self.nasdaq_listed_companies_url:
            return None
        try:
            response = self._get(self.nasdaq_listed_companies_url)
            if response.status_code >= 400:
                return None
            soup = BeautifulSoup(response.text, "html.parser")
            needles = tuple(
                value for value in (_normalize(issuer.name), _normalize(issuer.symbol), _normalize(issuer.isin)) if value
            )
            for link in soup.select("a[href]"):
                text = _normalize(link.get_text(" ", strip=True))
                href = link.get("href", "")
                if any(needle in text or needle in _normalize(href) for needle in needles):
                    return urljoin(self.nasdaq_listed_companies_url, href)
        except requests.RequestException:
            return None
        return None

    def resolve_issuer(self, issuer: Issuer) -> DenmarkIssuerResolution:
        try:
            notices: list[DenmarkNotice] = []
            seen_records: set[str] = set()
            for query in dict.fromkeys(
                value
                for value in (issuer.isin, issuer.name, issuer.symbol)
                if value
            ):
                found, _ = self._search(query, None, date.today(), 25)
                for notice in found:
                    if notice.record_id not in seen_records:
                        notices.append(notice)
                        seen_records.add(notice.record_id)
            best: DenmarkNotice | None = None
            best_score = 0
            normalized_name = _normalize(issuer.name)
            normalized_symbol = _normalize(issuer.symbol)
            for notice in notices:
                score = 0
                if issuer.isin and issuer.isin.upper() in {
                    value.upper() for value in notice.issuer_isins
                }:
                    score = 100
                elif normalized_name and normalized_name in _normalize(notice.issuer_name):
                    score = 80
                elif normalized_symbol and normalized_symbol in _normalize(
                    f"{notice.issuer_name} {notice.title}"
                ):
                    score = 60
                if score > best_score:
                    best, best_score = notice, score
            if not best:
                return DenmarkIssuerResolution(
                    found=False,
                    denmark_pea_country_check="eu_candidate",
                    attempts=list(self.attempts),
                    error="No matching DFSA issuer notice found",
                )
            detailed = self._load_detail(best)
            home_member_state = (
                "Denmark" if _is_confirmed_danish_issuer(detailed) else None
            )
            return DenmarkIssuerResolution(
                found=True,
                matched_name=detailed.issuer_name,
                denmark_dfsa_issuer_url=self.base_url,
                denmark_dfsa_record_id=detailed.record_id,
                denmark_dfsa_detail_url=detailed.detail_url,
                denmark_home_member_state=home_member_state,
                denmark_nasdaq_company_url=self._nasdaq_company_url(issuer),
                denmark_pea_country_check="eu_candidate",
                match_score=best_score,
                attempts=list(self.attempts),
            )
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            return DenmarkIssuerResolution(
                found=False,
                denmark_pea_country_check="eu_candidate",
                attempts=list(self.attempts),
                error=str(exc),
            )

    def diagnose(self, limit: int = 10) -> DenmarkSourceDiagnostic:
        until = date.today()
        since = until - timedelta(days=self.lookback_days)
        try:
            notices, total_count = self._search("", since, until, limit)
            formats: set[str] = set()
            example: dict[str, Any] | None = None
            for notice in notices:
                classification = classify_denmark_document(notice.title, notice.category)[0]
                if example is None:
                    example = {
                        "record_id": notice.record_id,
                        "title": notice.title,
                        "issuer": notice.issuer_name,
                        "published_at": notice.published_raw,
                        "category": notice.category,
                        "detail_url": notice.detail_url,
                    }
                if classification in PERIODIC_CATEGORIES:
                    detailed = self._load_detail(notice)
                    formats.update(item.file_type for item in detailed.files)
                    if formats:
                        break
            if not formats:
                periodic_notices, _ = self._search(
                    "annual report",
                    None,
                    until,
                    10,
                )
                for notice in periodic_notices:
                    detailed = self._load_detail(notice)
                    formats.update(item.file_type for item in detailed.files)
                    if {"PDF", "XHTML", "ZIP/XBRL"}.issubset(formats):
                        break
            state = "ready" if notices else "degraded"
            self.state = ConnectorState.READY if notices else ConnectorState.DEGRADED
            status = next(
                (attempt.status_code for attempt in reversed(self.attempts) if attempt.method == "POST"),
                None,
            )
            return DenmarkSourceDiagnostic(
                source="dfsa_oam",
                state=ConnectorState.READY if notices else ConnectorState.DEGRADED,
                called_url=self.search_url,
                http_status=status,
                method_used="date interval / global listing / search",
                total_count=total_count,
                fields=[
                    "id",
                    "HeadlineColumn",
                    "IssuerColumn",
                    "CategoryColumn",
                    "PublicationDateColumn",
                    "RegistrationDateColumn",
                ],
                example_notice=example,
                formats=sorted(formats),
                attempts=list(self.attempts),
            )
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            self.state = ConnectorState.UNAVAILABLE
            status = self.attempts[-1].status_code if self.attempts else None
            return DenmarkSourceDiagnostic(
                source="dfsa_oam",
                state=ConnectorState.UNAVAILABLE,
                called_url=self.search_url,
                http_status=status,
                method_used="date interval / global listing / search",
                total_count=0,
                attempts=list(self.attempts),
                error=str(exc),
            )

    def discover(self, query: str, limit: int = 25) -> DenmarkSourceDiscovery:
        try:
            notices, _ = self._search(query, None, date.today(), limit)
            candidates: list[DocumentCandidate] = []
            for notice in notices:
                if classify_denmark_document(notice.title, notice.category)[0] in PERIODIC_CATEGORIES:
                    candidates.extend(self._detailed_candidates(notice))
                    if len(candidates) >= limit:
                        break
            return DenmarkSourceDiscovery(
                source="dfsa_oam",
                state="ready" if notices else "degraded",
                query=query,
                notices=notices,
                candidates=candidates[:limit],
                attempts=list(self.attempts),
            )
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            return DenmarkSourceDiscovery(
                source="dfsa_oam",
                state="unavailable",
                query=query,
                attempts=list(self.attempts),
                error=str(exc),
            )

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None = None,
    ) -> int:
        return 2

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 5
