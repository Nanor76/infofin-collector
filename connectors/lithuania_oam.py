from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from connectors.base import Connector, ConnectorState, DocumentCandidate, EndpointAttempt
from models import Issuer


DEFAULT_BASE_URL = "https://www.oam.lt"
SUPPORTED_FORMATS = {"pdf", "zip", "xhtml", "xht", "xml", "xbrl", "xbri"}

PERIODIC_CATEGORIES = {
    "171": "Metinė informacija",
    "452": "Pusmečio informacija",
    "172": "Tarpinė informacija",
}

NEGATIVE_TERMS = (
    "prospectus",
    "final terms",
    "prospektas",
    "emisiju prospektai",
    "bond",
    "bonds",
    "notes",
    "debt",
    "obligacija",
    "obligacijos",
    "obligaciju",
    "share buyback",
    "share buy-back",
    "nuosavu akciju",
    "tender offer",
    "capital increase",
    "rights issue",
    "major holding",
    "major shareholding",
    "insider transaction",
    "manager transaction",
    "managers transaction",
    "vadovu sandoriai",
    "vadovu sandoriu",
    "voting rights",
    "general meeting",
    "akcininku susirinkimas",
    "visuotinis akcininku",
    "dividend announcement",
    "dividend",
    "corporate action",
    "investor presentation",
    "pristatymas",
    "press release",
    "financial calendar",
    "finansu kalendorius",
    "investuotoju kalendorius",
    "webcast",
    "factsheet",
    "fund",
    "ucits",
    "kid",
    "priips",
    "pranešimas apie esminį įvykį",
    "esminio iykio",
    "kita informacija",
)


def _normalize(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()


def _normalize_issuer(value: object) -> str:
    normalized = _normalize(value)
    return re.sub(
        r"\b(?:uab|ab|as|grupe|grupė)\b",
        " ",
        normalized,
    ).strip()


def _parse_date(value: object) -> date | None:
    raw = str(value or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass
    for pattern in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def _file_format(filename: str, url: str = "") -> str | None:
    for value in (filename, urlparse(url).path):
        suffix = PurePosixPath(value).suffix.casefold().lstrip(".")
        if suffix in SUPPORTED_FORMATS:
            return "xhtml" if suffix == "xht" else suffix
    normalized = _normalize(f"{filename} {url}")
    for marker in ("xbri", "xbrl", "xhtml", "esef", "zip", "pdf"):
        if marker in normalized:
            return "zip" if marker == "esef" else marker
    if "globenewswire" in normalized and "download" in normalized:
        return "pdf"
    return None


def classify_lithuania_document(
    title: str,
    category: str = "",
    filename: str = "",
) -> tuple[str, str, list[str], list[str]]:
    haystack = _normalize(" ".join((title, category, filename)))
    negative = sorted(
        {term for term in NEGATIVE_TERMS if _normalize(term) in haystack}
    )
    if negative:
        return (
            "other_regulatory_announcement",
            f"Explicit exclusion term: {negative[0]}",
            [],
            negative,
        )

    filename_haystack = _normalize(filename)
    attachment_negative = sorted(
        term
        for term in (
            "corporate governance",
            "governance report",
            "valdymo ataskaita",
            "korporacinio valdymo",
            "valdymo ir kontroles",
        )
        if _normalize(term) in filename_haystack
    )
    if attachment_negative:
        return (
            "other_regulatory_announcement",
            f"Explicit attachment exclusion term: {attachment_negative[0]}",
            [],
            attachment_negative,
        )

    quarterly = (
        "quarterly report",
        "quarter report",
        "first quarter",
        "second quarter",
        "third quarter",
        "fourth quarter",
        "q1",
        "q2",
        "q3",
        "q4",
        "ketvirtinis",
        "ketvircio",
        "trijų mėnesių",
        "triju menesiu",
        "3 menesiu",
        "3 menesio",
    )
    half_year = (
        "half year",
        "half-year",
        "semi annual",
        "semi-annual",
        "pusmetinio",
        "pusmetines",
        "pusmecio",
        "pusmecio informacija",
        "6 menesiu",
        "6 menesio",
    )
    interim = (
        "interim report",
        "interim financial",
        "tarpine informacija",
        "tarpines finansines",
        "starpposma",
    )
    annual = (
        "annual financial report",
        "annual report",
        "annual financial statements",
        "audited annual report",
        "consolidated annual report",
        "standalone annual report",
        "year end report",
        "year-end report",
        "metine informacija",
        "metines finansines",
        "finansiniu ataskaitu",
        "audituotas metinis",
        "audituotos finansines",
        "metu finansiniu rezultatu",
        "metinis pranesimas",
    )

    rules = (
        ("quarterly_financial_report", quarterly),
        ("half_year_financial_report", half_year),
        ("interim_report", interim),
        ("annual_financial_report", annual),
    )
    for document_type, terms in rules:
        matched = sorted({term for term in terms if _normalize(term) in haystack})
        if matched:
            return (
                document_type,
                f"Periodic report term: {matched[0]}",
                matched,
                [],
            )

    category_normalized = _normalize(category)
    if category_normalized in {
        "171",
        _normalize(PERIODIC_CATEGORIES["171"]),
        _normalize("Metinė informacija"),
    }:
        return (
            "annual_financial_report",
            "OAM Lithuania exact annual-information category 171",
            ["171"],
            [],
        )
    if category_normalized in {
        "452",
        _normalize(PERIODIC_CATEGORIES["452"]),
        _normalize("Pusmečio informacija"),
    }:
        return (
            "half_year_financial_report",
            "OAM Lithuania exact half-year category 452",
            ["452"],
            [],
        )
    if category_normalized in {
        "172",
        _normalize(PERIODIC_CATEGORIES["172"]),
        _normalize("Tarpinė informacija"),
    }:
        if re.search(r"\bq[1-4]\b|ketvirt|trijų mėnesių|triju menesiu", haystack):
            return (
                "quarterly_financial_report",
                "OAM Lithuania interim category 172 with quarterly title",
                ["172"],
                [],
            )
        return (
            "interim_report",
            "OAM Lithuania interim information category 172",
            ["172"],
            [],
        )

    return (
        "other_regulatory_announcement",
        "No accepted periodic report category or title term",
        [],
        [],
    )


def extract_lithuania_date_info(
    title: str,
    published_raw: str | None,
    category: str = "",
    filename: str = "",
) -> dict[str, Any]:
    published_at = _parse_date(published_raw)
    text = " ".join((title, filename))
    period_end: date | None = None
    reporting_year: int | None = None
    source_period_raw: str | None = None
    reason = "No unambiguous reporting period detected"

    patterns = (
        (r"\b(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})\b", (1, 2, 3)),
        (r"\b(\d{1,2})[.](\d{1,2})[.](20\d{2})\b", (3, 2, 1)),
        (r"\bFA\s+(20\d{2})\s+(\d{1,2})\s+(\d{1,2})\b", (1, 2, 3)),
    )
    explicit_dates: list[tuple[date, str]] = []
    for pattern, order in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            year, month, day = (int(match.group(index)) for index in order)
            try:
                parsed = date(year, month, day)
            except ValueError:
                continue
            if published_at and (
                parsed > published_at
                or (published_at - parsed) < timedelta(days=14)
            ):
                continue
            explicit_dates.append((parsed, match.group(0)))
    if explicit_dates:
        period_end, source_period_raw = max(explicit_dates, key=lambda item: item[0])
        reporting_year = period_end.year
        reason = "Explicit reporting-period date in title or attachment filename"

    classification = classify_lithuania_document(title, category, filename)[0]
    if reporting_year is None and classification != "other_regulatory_announcement":
        years = [
            int(value)
            for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)
            if published_at is None or int(value) <= published_at.year
        ]
        if years:
            reporting_year = years[-1]
            source_period_raw = str(reporting_year)
            reason = "Reporting year extracted from periodic report title or filename"
            normalized = _normalize(text)
            if classification == "annual_financial_report":
                period_end = date(reporting_year, 12, 31)
                reason += "; annual period end inferred"
            elif classification == "half_year_financial_report":
                period_end = date(reporting_year, 6, 30)
                reason += "; half-year period end inferred"
            elif classification == "quarterly_financial_report":
                quarter_month = None
                for marker, month in (
                    ("q1", 3),
                    ("first quarter", 3),
                    ("q2", 6),
                    ("second quarter", 6),
                    ("q3", 9),
                    ("third quarter", 9),
                    ("q4", 12),
                    ("fourth quarter", 12),
                    ("trijų mėnesių", 3),
                    ("triju menesiu", 3),
                    ("ketvirt", 3),
                ):
                    if marker in normalized:
                        quarter_month = month
                        break
                if quarter_month:
                    period_end = date(
                        reporting_year,
                        quarter_month,
                        31 if quarter_month in {3, 12} else 30,
                    )
                    reason += "; quarter end inferred from explicit quarter"

    return {
        "published_at": published_at,
        "period_end_date": period_end,
        "reporting_year": reporting_year,
        "source_publication_date_raw": published_raw,
        "source_period_date_raw": source_period_raw,
        "date_confidence": "high" if published_at else "low",
        "date_extraction_reason": reason,
    }


@dataclass(frozen=True, slots=True)
class LithuaniaFile:
    attachment_id: str
    filename: str
    download_url: str
    file_format: str | None
    is_official_oam: bool = True


@dataclass(frozen=True, slots=True)
class LithuaniaNotice:
    record_id: str
    issuer_name: str
    title: str
    category: str
    published_raw: str
    published_at: date | None
    detail_url: str
    files: tuple[LithuaniaFile, ...] = ()


@dataclass(frozen=True, slots=True)
class LithuaniaListingPage:
    notices: tuple[LithuaniaNotice, ...]
    page_count: int


@dataclass(frozen=True, slots=True)
class LithuaniaSourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    http_status: int | None
    method_used: str
    total_count: int
    detected_count: int
    attachment_count: int
    fields: tuple[str, ...]
    categories: dict[str, int]
    formats: tuple[str, ...]
    example_notice: dict[str, Any] | None
    http_calls: int
    request_efficiency: str
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LithuaniaSourceDiscovery:
    source: str
    query: str
    notices: tuple[LithuaniaNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LithuaniaIssuerResolution:
    found: bool
    matched_name: str | None = None
    source_record_id: str | None = None
    source_url: str | None = None
    detail_url: str | None = None
    home_member_state: str | None = "Lithuania"
    pea_country_check: str | None = "eu_candidate"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


def parse_lithuania_listing(
    html_text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> LithuaniaListingPage:
    soup = BeautifulSoup(html_text, "html.parser")
    notices: list[LithuaniaNotice] = []

    for row in soup.find_all("nef-table-row", class_="message-row"):
        cells = row.find_all("nef-table-cell")
        if len(cells) < 3:
            continue
        published_raw = cells[0].get_text(" ", strip=True)
        issuer_name = cells[1].get_text(" ", strip=True)
        link_el = cells[2].find("nef-link") or cells[2].find("a")
        if link_el is None:
            continue
        title = link_el.get_text(" ", strip=True)
        href = str(link_el.get("href") or "")
        match = re.search(r"/view/(\d+)", href)
        if not match:
            continue
        category = cells[3].get_text(" ", strip=True) if len(cells) > 3 else ""
        detail_url = urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))
        notices.append(
            LithuaniaNotice(
                record_id=match.group(1),
                issuer_name=issuer_name,
                title=title,
                category=category,
                published_raw=published_raw,
                published_at=_parse_date(published_raw),
                detail_url=detail_url,
            )
        )

    page_numbers = [1]
    pagination = soup.find("nef-pagination")
    if pagination is not None:
        total_pages = pagination.get("totalpages") or pagination.get("total-pages")
        if total_pages:
            try:
                page_numbers.append(int(total_pages))
            except ValueError:
                pass
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        page_match = re.search(r"[?&]page=(\d+)", href)
        if page_match:
            page_numbers.append(int(page_match.group(1)))

    return LithuaniaListingPage(
        notices=tuple(notices),
        page_count=max(page_numbers),
    )


def _attachment_id_from_url(url: str) -> str:
    query = urlparse(url).query
    match = re.search(r"messageAttachmentId=(\d+)", query)
    if match:
        return match.group(1)
    path_match = re.search(
        r"/Resource/Download/([0-9a-f-]{36})",
        url,
        flags=re.IGNORECASE,
    )
    if path_match:
        return path_match.group(1)
    return PurePosixPath(urlparse(url).path).name or "attachment"


def parse_lithuania_detail(
    html_text: str,
    notice: LithuaniaNotice,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> LithuaniaNotice:
    soup = BeautifulSoup(html_text, "html.parser")
    files: list[LithuaniaFile] = []
    seen_urls: set[str] = set()

    for link in soup.find_all("nef-link", class_="attachment-link"):
        href = str(link.get("href") or "")
        if "viewAttachment.action" not in href:
            continue
        download_url = urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))
        if download_url in seen_urls:
            continue
        filename = (
            link.get("title")
            or link.get("aria-label")
            or link.get_text(" ", strip=True)
            or ""
        )
        filename = re.sub(r"^Parsisiųsti\s+", "", filename, flags=re.IGNORECASE)
        attachment_id = _attachment_id_from_url(download_url)
        file_format = _file_format(filename, download_url)
        seen_urls.add(download_url)
        files.append(
            LithuaniaFile(
                attachment_id=attachment_id,
                filename=filename or f"attachment-{attachment_id}",
                download_url=download_url,
                file_format=file_format,
                is_official_oam=True,
            )
        )

    for link in soup.select("#gnw_attachments_section-items a[href]"):
        href = str(link.get("href") or "")
        if "globenewswire.com/Resource/Download" not in href:
            continue
        if href in seen_urls:
            continue
        filename = link.get_text(" ", strip=True) or _attachment_id_from_url(href)
        attachment_id = _attachment_id_from_url(href)
        file_format = _file_format(filename, href)
        seen_urls.add(href)
        files.append(
            LithuaniaFile(
                attachment_id=attachment_id,
                filename=filename,
                download_url=href,
                file_format=file_format,
                is_official_oam=False,
            )
        )

    return LithuaniaNotice(
        record_id=notice.record_id,
        issuer_name=notice.issuer_name,
        title=notice.title,
        category=notice.category,
        published_raw=notice.published_raw,
        published_at=notice.published_at,
        detail_url=notice.detail_url,
        files=tuple(files),
    )


class LithuaniaOamConnector(Connector):
    market = "Vilnius Stock Exchange"
    source_name = "lithuania_oam"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str = DEFAULT_BASE_URL,
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 90,
        timeout: int = 30,
        verify_ssl: bool = True,
        max_pages: int = 10,
        page_size: int = 50,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.search_url = f"{self.base_url}/"
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.max_pages = max(1, max_pages)
        self.page_size = max(1, page_size)
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        self._csrf_token: str | None = None
        self._listing_cache: dict[
            tuple[date, date, str, int], tuple[LithuaniaNotice, ...]
        ] = {}
        self._detail_cache: dict[str, LithuaniaNotice] = {}
        self._scanned_notices = 0
        self._details_visited = 0
        self._cache_hits = 0

    @property
    def scanned_notices(self) -> int:
        return self._scanned_notices

    @property
    def details_visited(self) -> int:
        return self._details_visited

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _fetch_csrf(self, *, force: bool = False) -> str:
        if self._csrf_token and not force:
            return self._csrf_token
        self._wait()
        response: Any | None = None
        try:
            response = self.session.get(
                self.search_url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            csrf_tag = soup.find("meta", {"name": "_csrf"})
            token = csrf_tag.get("content") if csrf_tag else ""
            if not token:
                csrf_input = soup.find("input", {"name": "_csrf"})
                token = csrf_input.get("value") if csrf_input else ""
            self._csrf_token = token
            self.attempts.append(
                EndpointAttempt(
                    name="OAM Lithuania CSRF bootstrap",
                    base_url=self.base_url,
                    dataset="CSF",
                    endpoint="/",
                    method="GET",
                    http_status=response.status_code,
                    success=bool(token),
                )
            )
            return token
        except Exception as exc:
            self.state = ConnectorState.UNAVAILABLE
            self.last_error = str(exc)
            self.attempts.append(
                EndpointAttempt(
                    name="OAM Lithuania CSRF bootstrap",
                    base_url=self.base_url,
                    dataset="CSF",
                    endpoint="/",
                    method="GET",
                    http_status=getattr(response, "status_code", None),
                    success=False,
                    error=str(exc),
                )
            )
            raise
        finally:
            self._last_request_at = time.monotonic()

    def _fetch_listing(
        self,
        *,
        from_date: date,
        to_date: date,
        free_text: str = "",
        limit: int | None = None,
    ) -> tuple[LithuaniaNotice, ...]:
        cache_key = (from_date, to_date, free_text, limit or 0)
        if cache_key in self._listing_cache:
            self._cache_hits += 1
            cached = self._listing_cache[cache_key]
            return cached[:limit] if limit is not None else cached

        notices: list[LithuaniaNotice] = []
        page_count = 1
        for page in range(1, self.max_pages + 1):
            if page > page_count:
                break
            csrf = self._fetch_csrf(force=page == 1)
            if not csrf:
                break
            self._wait()
            response: Any | None = None
            payload = {
                "_csrf": csrf,
                "oam": "lt",
                "language": "lt",
                "pageSize": str(self.page_size),
                "page": str(page),
                "market": "",
                "company": "",
                "category": "",
                "startDate": from_date.isoformat(),
                "endDate": to_date.isoformat(),
                "freeText": free_text,
                "includeObsoleteCompanies": "true",
            }
            try:
                response = self.session.post(
                    self.search_url,
                    data=payload,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
                response.raise_for_status()
                parsed = parse_lithuania_listing(
                    response.text,
                    base_url=self.base_url,
                )
                page_count = min(parsed.page_count, self.max_pages)
                notices.extend(parsed.notices)
                self.attempts.append(
                    EndpointAttempt(
                        name=f"OAM Lithuania periodic listing page {page}",
                        base_url=self.base_url,
                        dataset="CSF",
                        endpoint="/",
                        method="POST",
                        http_status=response.status_code,
                        success=True,
                        total_count=len(parsed.notices),
                    )
                )
                if not parsed.notices or len(parsed.notices) < self.page_size:
                    break
                if limit is not None and len(notices) >= limit:
                    break
            except Exception as exc:
                self.state = ConnectorState.UNAVAILABLE
                self.last_error = str(exc)
                self.attempts.append(
                    EndpointAttempt(
                        name=f"OAM Lithuania periodic listing page {page}",
                        base_url=self.base_url,
                        dataset="CSF",
                        endpoint="/",
                        method="POST",
                        http_status=getattr(response, "status_code", None),
                        success=False,
                        error=str(exc),
                    )
                )
                raise
            finally:
                self._last_request_at = time.monotonic()

        result = tuple(notices)
        self._listing_cache[cache_key] = result
        self._scanned_notices += len(result)
        self.state = ConnectorState.READY
        self.last_error = None
        return result[:limit] if limit is not None else result

    def _notice_candidate(self, notice: LithuaniaNotice) -> DocumentCandidate:
        document_type, reason, positive, negative = classify_lithuania_document(
            notice.title,
            notice.category,
        )
        dates = extract_lithuania_date_info(
            notice.title,
            notice.published_raw,
            notice.category,
        )
        return DocumentCandidate(
            title=notice.title,
            url=notice.detail_url,
            published_date=dates["published_at"],
            document_type=document_type,
            source=self.source_name,
            source_document_id=notice.record_id,
            metadata={
                "official_source": 1,
                "issuer_name": notice.issuer_name,
                "issuer_country": "Lithuania",
                "home_member_state": "Lithuania",
                "pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
                "record_id": notice.record_id,
                "detail_url": notice.detail_url,
                "category": notice.category,
                "received_at": notice.published_raw,
                "parent_page_url": notice.detail_url,
                "lithuania_oam_url": self.search_url,
            },
            classification=document_type,
            classification_reason=reason,
            matched_positive_terms=positive,
            matched_negative_terms=negative,
            **dates,
        )

    def _load_detail(self, candidate: DocumentCandidate) -> LithuaniaNotice:
        record_id = str(candidate.metadata.get("record_id") or "")
        cached = self._detail_cache.get(record_id)
        if cached is not None:
            self._cache_hits += 1
            return cached

        notice = LithuaniaNotice(
            record_id=record_id,
            issuer_name=str(candidate.metadata.get("issuer_name") or ""),
            title=candidate.title,
            category=str(candidate.metadata.get("category") or ""),
            published_raw=candidate.source_publication_date_raw or "",
            published_at=candidate.published_at,
            detail_url=str(candidate.metadata.get("detail_url") or candidate.url),
        )
        self._wait()
        response: Any | None = None
        try:
            response = self.session.get(
                notice.detail_url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            detailed = parse_lithuania_detail(
                response.text,
                notice,
                base_url=self.base_url,
            )
            self._detail_cache[record_id] = detailed
            self._details_visited += 1
            self.attempts.append(
                EndpointAttempt(
                    name=f"OAM Lithuania detail {record_id}",
                    base_url=self.base_url,
                    dataset="CSF",
                    endpoint=notice.detail_url,
                    method="GET",
                    http_status=response.status_code,
                    success=True,
                    total_count=len(detailed.files),
                )
            )
            return detailed
        except Exception as exc:
            self.state = ConnectorState.UNAVAILABLE
            self.last_error = str(exc)
            self.attempts.append(
                EndpointAttempt(
                    name=f"OAM Lithuania detail {record_id}",
                    base_url=self.base_url,
                    dataset="CSF",
                    endpoint=notice.detail_url,
                    method="GET",
                    http_status=getattr(response, "status_code", None),
                    success=False,
                    error=str(exc),
                )
            )
            raise
        finally:
            self._last_request_at = time.monotonic()

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        if candidate.document_type == "other_regulatory_announcement":
            return [candidate]

        notice = self._load_detail(candidate)
        materialized: list[DocumentCandidate] = []
        for item in notice.files:
            document_type, reason, positive, negative = classify_lithuania_document(
                notice.title,
                notice.category,
                item.filename,
            )
            if document_type == "other_regulatory_announcement":
                continue
            dates = extract_lithuania_date_info(
                notice.title,
                notice.published_raw,
                notice.category,
                item.filename,
            )
            metadata = dict(candidate.metadata)
            metadata.update(
                {
                    "attachment_id": item.attachment_id,
                    "file_id": item.attachment_id,
                    "filename": item.filename,
                    "file_format": item.file_format,
                    "parent_page_url": notice.detail_url,
                    "official_oam_attachment": int(item.is_official_oam),
                }
            )
            materialized.append(
                DocumentCandidate(
                    title=f"{notice.title} - {item.filename}",
                    url=item.download_url,
                    published_date=dates["published_at"],
                    document_type=document_type,
                    source=self.source_name,
                    source_document_id=f"{notice.record_id}:{item.attachment_id}",
                    metadata=metadata,
                    classification=document_type,
                    classification_reason=reason,
                    matched_positive_terms=positive,
                    matched_negative_terms=negative,
                    **dates,
                )
            )
        return materialized

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != self.market.casefold():
            return []
        end = date.today()
        start = since or (end - timedelta(days=self.lookback_days))
        notices = self._fetch_listing(from_date=start, to_date=end, limit=limit)
        candidates = [self._notice_candidate(notice) for notice in notices]
        return candidates[:limit] if limit is not None else candidates

    def search_documents_for_issuer(
        self,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        candidates = self.search_recent_documents(self.market)
        expected = _normalize_issuer(issuer.name)
        return [
            candidate
            for candidate in candidates
            if _normalize_issuer(candidate.metadata.get("issuer_name")) == expected
            or expected in _normalize_issuer(candidate.metadata.get("issuer_name"))
            or _normalize_issuer(candidate.metadata.get("issuer_name")) in expected
        ]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def resolve_issuer(self, issuer: Issuer) -> LithuaniaIssuerResolution:
        end = date.today()
        start = end - timedelta(days=365 * 3)
        expected = _normalize_issuer(issuer.name)
        try:
            csrf = self._fetch_csrf()
            if not csrf:
                return LithuaniaIssuerResolution(
                    found=False,
                    attempts=tuple(self.attempts),
                    error="Could not obtain OAM Lithuania CSRF token",
                )
            response = self.session.get(self.search_url, timeout=self.timeout, verify=self.verify_ssl)
            soup = BeautifulSoup(response.text, "html.parser")
            company_select = soup.find(id="company-select")
            companies: list[dict[str, str]] = []
            if company_select and company_select.get("options"):
                companies = json.loads(company_select.get("options"))

            best: tuple[float, LithuaniaNotice] | None = None
            for notice in self._fetch_listing(
                from_date=start,
                to_date=end,
                limit=100,
            ):
                observed = _normalize_issuer(notice.issuer_name)
                score = (
                    100.0
                    if expected == observed
                    else 85.0
                    if expected in observed or observed in expected
                    else 0.0
                )
                if score and (best is None or score > best[0]):
                    best = (score, notice)

            if best is None and companies:
                for item in companies:
                    label_norm = _normalize_issuer(item.get("label", ""))
                    score = (
                        100.0
                        if expected == label_norm
                        else 85.0
                        if expected in label_norm or label_norm in expected
                        else 0.0
                    )
                    if score and (best is None or score > best[0]):
                        best = (
                            score,
                            LithuaniaNotice(
                                record_id=item.get("value", ""),
                                issuer_name=item.get("label", ""),
                                title="",
                                category="",
                                published_raw="",
                                published_at=None,
                                detail_url=self.search_url,
                            ),
                        )

            if best is None:
                return LithuaniaIssuerResolution(
                    found=False,
                    attempts=tuple(self.attempts),
                    error="No matching issuer in OAM Lithuania periodic filings",
                )
            score, notice = best
            return LithuaniaIssuerResolution(
                found=True,
                matched_name=notice.issuer_name,
                source_record_id=notice.record_id,
                source_url=self.search_url,
                detail_url=notice.detail_url,
                match_score=score,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return LithuaniaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def discover(self, query: str, limit: int = 25) -> LithuaniaSourceDiscovery:
        normalized = _normalize(query)
        end = date.today()
        start = end - timedelta(days=365 * 3)
        try:
            notices = list(
                self._fetch_listing(
                    from_date=start,
                    to_date=end,
                    free_text=query if normalized not in {"annual", "half year"} else "",
                    limit=200,
                )
            )
            if "annual" in normalized:
                notices = [
                    notice
                    for notice in notices
                    if classify_lithuania_document(
                        notice.title,
                        notice.category,
                    )[0]
                    == "annual_financial_report"
                ]
            elif "half year" in normalized or "semi annual" in normalized:
                notices = [
                    notice
                    for notice in notices
                    if classify_lithuania_document(
                        notice.title,
                        notice.category,
                    )[0]
                    == "half_year_financial_report"
                ]
            elif "quarter" in normalized or "interim" in normalized:
                notices = [
                    notice
                    for notice in notices
                    if classify_lithuania_document(
                        notice.title,
                        notice.category,
                    )[0]
                    in {"quarterly_financial_report", "interim_report"}
                ]
            notices = notices[:limit]
            candidates = tuple(self._notice_candidate(notice) for notice in notices)
            return LithuaniaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=tuple(notices),
                candidates=candidates,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return LithuaniaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> LithuaniaSourceDiagnostic:
        end = date.today()
        start = end - timedelta(days=120)
        try:
            notices = list(
                self._fetch_listing(from_date=start, to_date=end, limit=100)
            )
            categories: dict[str, int] = {}
            for notice in notices:
                categories[notice.category] = categories.get(notice.category, 0) + 1

            periodic = next(
                (
                    notice
                    for notice in notices
                    if classify_lithuania_document(
                        notice.title,
                        notice.category,
                    )[0]
                    != "other_regulatory_announcement"
                ),
                None,
            )
            formats: set[str] = set()
            attachment_count = 0
            example = None
            if periodic is not None:
                candidate = self._notice_candidate(periodic)
                detailed = self._load_detail(candidate)
                attachment_count = len(detailed.files)
                formats.update(
                    item.file_format for item in detailed.files if item.file_format
                )
                example = {
                    "record_id": periodic.record_id,
                    "issuer": periodic.issuer_name,
                    "title": periodic.title,
                    "category": periodic.category,
                    "published_at": (
                        periodic.published_at.isoformat()
                        if periodic.published_at
                        else None
                    ),
                    "detail_url": periodic.detail_url,
                    "files": [
                        {
                            "attachment_id": item.attachment_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": item.download_url,
                            "official_oam_attachment": item.is_official_oam,
                        }
                        for item in detailed.files
                    ],
                }
            status = next(
                (
                    attempt.http_status
                    for attempt in reversed(self.attempts)
                    if attempt.success
                ),
                None,
            )
            return LithuaniaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.READY if example else ConnectorState.DEGRADED,
                called_url=self.search_url,
                http_status=status,
                method_used=(
                    "GET CSRF bootstrap then POST global CSF listing with date filters; "
                    "detail GET only for selected notices; local classification because "
                    "server-side category filter is ignored"
                ),
                total_count=len(notices),
                detected_count=len(notices),
                attachment_count=attachment_count,
                fields=(
                    "record_id",
                    "issuer_name",
                    "title",
                    "category",
                    "published_at",
                    "detail_url",
                    "attachment_url",
                ),
                categories=categories,
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=len(self.attempts),
                request_efficiency=(
                    "One CSRF GET plus bounded global POST per page; no issuer loop; "
                    f"detail pages visited only after local matching; cache hits: "
                    f"{self._cache_hits}"
                ),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return LithuaniaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.search_url,
                http_status=None,
                method_used="GET/POST OAM Lithuania CSF listing",
                total_count=0,
                detected_count=0,
                attachment_count=0,
                fields=(),
                categories={},
                formats=(),
                example_notice=None,
                http_calls=len(self.attempts),
                request_efficiency="Diagnostic failed before completion",
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        return 2

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 3