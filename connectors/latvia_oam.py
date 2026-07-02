from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from connectors.base import Connector, ConnectorState, DocumentCandidate, EndpointAttempt
from models import Issuer


DEFAULT_BASE_URL = "https://csri.investinfo.lv"
SEARCH_PATH = "/lv/"
PERIODIC_TYPES = {
    "111": "Annual financial reports and audit reports",
    "112": "Half-year financial reports",
    "101": "Periodic regulated information",
}
SUPPORTED_FORMATS = {"pdf", "zip", "xhtml", "xht", "xml", "xbrl", "xbri"}

NEGATIVE_TERMS = (
    "prospectus",
    "final terms",
    "bond",
    "bonds",
    "notes",
    "debt",
    "share buyback",
    "share buy-back",
    "tender offer",
    "capital increase",
    "rights issue",
    "major holding",
    "major shareholding",
    "insider transaction",
    "manager transaction",
    "managers transaction",
    "voting rights",
    "general meeting",
    "dividend announcement",
    "corporate action",
    "presentation",
    "investor presentation",
    "press release",
    "financial calendar",
    "webcast",
    "factsheet",
    "fund",
    "ucits",
    "kid",
    "priips",
    "prospekts",
    "obligacija",
    "obligacijas",
    "parads",
    "sapulce",
    "sapulces",
    "dividende",
    "dividendes",
    "finansu kalendars",
    "prezentacija",
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
        r"\b(?:as|akciju sabiedriba|akciju sabiedrības|sia)\b",
        " ",
        normalized,
    ).strip()


def _parse_date(value: object) -> date | None:
    raw = str(value or "").strip()
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
    if "openxhtml" in normalized:
        return "xhtml"
    return None


def classify_latvia_document(
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
            "korporativas parvaldibas",
            "parvaldibas zinojums",
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
        "ceturksna",
        "ceturksna parskats",
    )
    half_year = (
        "half year",
        "half-year",
        "semi annual",
        "semi-annual",
        "6 months",
        "six months",
        "pusgada",
        "pusgada parskats",
    )
    interim = (
        "interim report",
        "interim financial",
        "starpposma",
        "starpperioda",
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
        "gada parskats",
        "gada finanšu pārskats",
        "gada finansu parskats",
        "revidetie gada rezultati",
        "revidetais gada parskats",
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
    if category_normalized in {"111", _normalize(PERIODIC_TYPES["111"])}:
        return (
            "annual_financial_report",
            "OAM Latvia exact annual-report type 111",
            ["111"],
            [],
        )
    if category_normalized in {"112", _normalize(PERIODIC_TYPES["112"])}:
        return (
            "half_year_financial_report",
            "OAM Latvia exact half-year-report type 112",
            ["112"],
            [],
        )
    if category_normalized in {"101", _normalize(PERIODIC_TYPES["101"])}:
        if re.search(r"\bq[1-4]\b|ceturksna", haystack):
            return (
                "quarterly_financial_report",
                "OAM Latvia periodic type 101 with quarterly title",
                ["101"],
                [],
            )
        return (
            "interim_report",
            "OAM Latvia periodic regulated information type 101",
            ["101"],
            [],
        )

    return (
        "other_regulatory_announcement",
        "No accepted periodic report category or title term",
        [],
        [],
    )


def extract_latvia_date_info(
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
    )
    explicit_dates: list[tuple[date, str]] = []
    for pattern, order in patterns:
        for match in re.finditer(pattern, text):
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

    classification = classify_latvia_document(title, category, filename)[0]
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
                    ("ceturksna", 3),
                ):
                    if marker in normalized:
                        quarter_month = month
                        break
                if quarter_month:
                    inferred_period_end = date(
                        reporting_year,
                        quarter_month,
                        31 if quarter_month in {3, 12} else 30,
                    )
                    if published_at and inferred_period_end > published_at:
                        reason += "; inferred quarter end after publication ignored"
                    else:
                        period_end = inferred_period_end
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
class LatviaFile:
    attachment_id: str
    filename: str
    download_url: str
    file_format: str | None


@dataclass(frozen=True, slots=True)
class LatviaNotice:
    record_id: str
    issuer_name: str
    title: str
    category: str
    language: str | None
    published_raw: str
    published_at: date | None
    detail_url: str
    files: tuple[LatviaFile, ...] = ()


@dataclass(frozen=True, slots=True)
class LatviaListingPage:
    notices: tuple[LatviaNotice, ...]
    page_count: int


@dataclass(frozen=True, slots=True)
class LatviaSourceDiagnostic:
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
class LatviaSourceDiscovery:
    source: str
    query: str
    notices: tuple[LatviaNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LatviaIssuerResolution:
    found: bool
    matched_name: str | None = None
    source_record_id: str | None = None
    source_url: str | None = None
    detail_url: str | None = None
    home_member_state: str | None = "Latvia"
    pea_country_check: str | None = "eu_candidate"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


def _category_from_id(value: str) -> str:
    return PERIODIC_TYPES.get(value, value)


def parse_latvia_listing(
    html_text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    category_hint: str = "",
) -> LatviaListingPage:
    soup = BeautifulSoup(html_text, "html.parser")
    notices: list[LatviaNotice] = []

    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        link = row.find("a", href=lambda value: value and "csridocumentsdetails" in value)
        if not link:
            continue
        href = str(link.get("href") or "")
        match = re.search(r"[?&]id=(\d+)", href)
        if not match or "csridocumentsdetails" not in href:
            continue

        values = [cell.get_text(" ", strip=True) for cell in cells]
        published_raw = values[0]
        issuer_name = values[1]
        category_value = category_hint or (values[2] if len(values) > 2 else "")
        category = _category_from_id(category_value)
        language = values[3] if len(values) > 3 else None
        title = link.get_text(" ", strip=True) or values[4]
        detail_url = urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))
        notices.append(
            LatviaNotice(
                record_id=match.group(1),
                issuer_name=issuer_name,
                title=title,
                category=category,
                language=language or None,
                published_raw=published_raw,
                published_at=_parse_date(published_raw),
                detail_url=detail_url,
            )
        )

    page_numbers = [1]
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        page_match = re.search(r"[?&](?:limitstart|start|page)=(\d+)", href)
        if page_match:
            offset = int(page_match.group(1))
            page_numbers.append((offset // 20) + 1 if offset >= 20 else offset + 1)

    return LatviaListingPage(notices=tuple(notices), page_count=max(page_numbers))


def _filename_from_link(link: Any, href: str, file_format: str | None) -> str:
    text = link.get_text(" ", strip=True)
    if not text or "." not in text:
        span = link.find_previous("span")
        if span is not None:
            text = span.get_text(" ", strip=True)
    text = re.sub(r"\s*\(\s*\d+(?:\.\d+)?\s*(?:[KkMm][Bb])\s*\)\s*$", "", text)
    if text and "." in text:
        return text
    query = parse_qs(urlparse(href).query)
    file_id = (query.get("f_id") or ["attachment"])[0]
    suffix = file_format or ("xhtml" if "openxhtml" in href.casefold() else "bin")
    return f"attachment-{file_id}.{suffix}"


def parse_latvia_detail(
    html_text: str,
    notice: LatviaNotice,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> LatviaNotice:
    soup = BeautifulSoup(html_text, "html.parser")
    files: list[LatviaFile] = []
    seen_urls: set[str] = set()

    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if "task=download" not in href and "task=openxhtml" not in href:
            continue
        query = parse_qs(urlparse(href).query)
        attachment_id = (query.get("f_id") or [""])[0]
        if not attachment_id:
            continue
        task = (query.get("task") or ["download"])[0].casefold()
        if task == "openxhtml":
            attachment_id = f"{attachment_id}:xhtml"
        download_url = urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))
        if download_url in seen_urls:
            continue
        file_format = _file_format(link.get_text(" ", strip=True), download_url)
        filename = _filename_from_link(link, href, file_format)
        file_format = _file_format(filename, download_url)
        seen_urls.add(download_url)
        files.append(
            LatviaFile(
                attachment_id=attachment_id,
                filename=filename,
                download_url=download_url,
                file_format=file_format,
            )
        )

    return LatviaNotice(
        record_id=notice.record_id,
        issuer_name=notice.issuer_name,
        title=notice.title,
        category=notice.category,
        language=notice.language,
        published_raw=notice.published_raw,
        published_at=notice.published_at,
        detail_url=notice.detail_url,
        files=tuple(files),
    )


class LatviaOamConnector(Connector):
    market = "Riga Stock Exchange"
    source_name = "latvia_oam"
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
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.search_url = f"{self.base_url}{SEARCH_PATH}"
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.max_pages = max(1, max_pages)
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        self._listing_cache: dict[
            tuple[date, date, str], tuple[LatviaNotice, ...]
        ] = {}
        self._detail_cache: dict[str, LatviaNotice] = {}
        self._scanned_notices = 0
        self._details_visited = 0
        self._cache_hits = 0

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _params(
        self,
        *,
        from_date: date,
        to_date: date,
        document_type: str = "",
        page: int = 1,
    ) -> list[tuple[str, str]]:
        params = [
            ("view", "csridocuments"),
            ("doc_datefrom", from_date.isoformat()),
            ("doc_dateto", to_date.isoformat()),
        ]
        if document_type:
            params.append(("doc_types[]", document_type))
        else:
            for type_id in PERIODIC_TYPES:
                params.append(("doc_types[]", type_id))
        if page > 1:
            params.append(("start", str((page - 1) * 20)))
        return params

    def _fetch_listing(
        self,
        *,
        from_date: date,
        to_date: date,
        document_type: str = "",
        limit: int | None = None,
    ) -> tuple[LatviaNotice, ...]:
        cache_key = (from_date, to_date, document_type)
        if cache_key in self._listing_cache:
            self._cache_hits += 1
            cached = self._listing_cache[cache_key]
            return cached[:limit] if limit is not None else cached

        notices: list[LatviaNotice] = []
        page_count = 1
        for page in range(1, self.max_pages + 1):
            if page > page_count:
                break
            self._wait()
            response: Any | None = None
            try:
                params = self._params(
                    from_date=from_date,
                    to_date=to_date,
                    document_type=document_type,
                    page=page,
                )
                response = self.session.get(
                    self.search_url,
                    params=params,
                    headers={"Accept": "text/html,application/xhtml+xml"},
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
                response.raise_for_status()
                parsed = parse_latvia_listing(
                    response.text,
                    base_url=self.base_url,
                    category_hint=document_type,
                )
                page_count = min(parsed.page_count, self.max_pages)
                notices.extend(parsed.notices)
                self.attempts.append(
                    EndpointAttempt(
                        name=f"OAM Latvia periodic listing page {page}",
                        base_url=self.base_url,
                        dataset="CSRI",
                        endpoint=SEARCH_PATH,
                        method="GET",
                        http_status=response.status_code,
                        success=True,
                        total_count=len(parsed.notices),
                    )
                )
                if not parsed.notices or (
                    limit is not None and len(notices) >= limit
                ):
                    break
            except Exception as exc:
                self.state = ConnectorState.UNAVAILABLE
                self.last_error = str(exc)
                self.attempts.append(
                    EndpointAttempt(
                        name=f"OAM Latvia periodic listing page {page}",
                        base_url=self.base_url,
                        dataset="CSRI",
                        endpoint=SEARCH_PATH,
                        method="GET",
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

    def _notice_candidate(self, notice: LatviaNotice) -> DocumentCandidate:
        document_type, reason, positive, negative = classify_latvia_document(
            notice.title,
            notice.category,
        )
        dates = extract_latvia_date_info(
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
                "issuer_country": "Latvia",
                "home_member_state": "Latvia",
                "pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
                "record_id": notice.record_id,
                "detail_url": notice.detail_url,
                "category": notice.category,
                "language": notice.language,
                "received_at": notice.published_raw,
                "parent_page_url": notice.detail_url,
                "latvia_oam_url": self.search_url,
            },
            classification=document_type,
            classification_reason=reason,
            matched_positive_terms=positive,
            matched_negative_terms=negative,
            **dates,
        )

    def _load_detail(self, candidate: DocumentCandidate) -> LatviaNotice:
        record_id = str(candidate.metadata.get("record_id") or "")
        cached = self._detail_cache.get(record_id)
        if cached is not None:
            self._cache_hits += 1
            return cached

        notice = LatviaNotice(
            record_id=record_id,
            issuer_name=str(candidate.metadata.get("issuer_name") or ""),
            title=candidate.title,
            category=str(candidate.metadata.get("category") or ""),
            language=str(candidate.metadata.get("language") or "") or None,
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
            detailed = parse_latvia_detail(
                response.text,
                notice,
                base_url=self.base_url,
            )
            self._detail_cache[record_id] = detailed
            self._details_visited += 1
            self.attempts.append(
                EndpointAttempt(
                    name=f"OAM Latvia detail {record_id}",
                    base_url=self.base_url,
                    dataset="CSRI",
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
                    name=f"OAM Latvia detail {record_id}",
                    base_url=self.base_url,
                    dataset="CSRI",
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
            document_type, reason, positive, negative = classify_latvia_document(
                notice.title,
                notice.category,
                item.filename,
            )
            dates = extract_latvia_date_info(
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
        notices: list[LatviaNotice] = []
        seen: set[str] = set()
        for document_type in PERIODIC_TYPES:
            remaining = None if limit is None else max(0, limit - len(notices))
            if remaining == 0:
                break
            for notice in self._fetch_listing(
                from_date=start,
                to_date=end,
                document_type=document_type,
                limit=remaining,
            ):
                if notice.record_id in seen:
                    continue
                seen.add(notice.record_id)
                notices.append(notice)
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
        ]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def resolve_issuer(self, issuer: Issuer) -> LatviaIssuerResolution:
        end = date.today()
        start = end - timedelta(days=365 * 3)
        expected = _normalize_issuer(issuer.name)
        try:
            best: tuple[float, LatviaNotice] | None = None
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
            if best is None:
                return LatviaIssuerResolution(
                    found=False,
                    attempts=tuple(self.attempts),
                    error="No matching issuer in OAM Latvia periodic filings",
                )
            score, notice = best
            return LatviaIssuerResolution(
                found=True,
                matched_name=notice.issuer_name,
                source_record_id=notice.record_id,
                source_url=self.search_url,
                detail_url=notice.detail_url,
                match_score=score,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return LatviaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def discover(self, query: str, limit: int = 25) -> LatviaSourceDiscovery:
        normalized = _normalize(query)
        if "half year" in normalized or "semi annual" in normalized:
            document_type = "112"
        elif "quarter" in normalized or "interim" in normalized:
            document_type = "101"
        elif "annual" in normalized:
            document_type = "111"
        else:
            document_type = ""

        end = date.today()
        start = end - timedelta(days=365 * 3)
        try:
            notices = self._fetch_listing(
                from_date=start,
                to_date=end,
                document_type=document_type,
                limit=limit,
            )
            candidates = tuple(self._notice_candidate(notice) for notice in notices)
            return LatviaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=tuple(notices),
                candidates=candidates,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return LatviaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> LatviaSourceDiagnostic:
        end = date.today()
        start = end - timedelta(days=120)
        try:
            notices = list(self._fetch_listing(from_date=start, to_date=end, limit=100))
            categories: dict[str, int] = {}
            for notice in notices:
                categories[notice.category] = categories.get(notice.category, 0) + 1

            periodic = next(
                (
                    notice
                    for notice in notices
                    if classify_latvia_document(notice.title, notice.category)[0]
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
                    "language": periodic.language,
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
            return LatviaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.READY if example else ConnectorState.DEGRADED,
                called_url=self.search_url,
                http_status=status,
                method_used=(
                    "GET global Joomla listing with date and doc_types filters; "
                    "detail GET only for selected notices"
                ),
                total_count=len(notices),
                detected_count=len(notices),
                attachment_count=attachment_count,
                fields=(
                    "record_id",
                    "issuer_name",
                    "title",
                    "category",
                    "language",
                    "published_at",
                    "detail_url",
                    "attachment_url",
                ),
                categories=categories,
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=len(self.attempts),
                request_efficiency=(
                    "One bounded global query per page; no issuer loop; "
                    f"detail pages visited only after local matching; cache hits: "
                    f"{self._cache_hits}"
                ),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return LatviaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.search_url,
                http_status=None,
                method_used="GET OAM Latvia HTML listing",
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
        return len(PERIODIC_TYPES)

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 2
