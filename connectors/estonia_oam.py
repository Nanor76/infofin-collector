from __future__ import annotations

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


DEFAULT_BASE_URL = "https://oam.fi.ee"
SEARCH_PATH = "/en/borsiteated"

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
    "investor presentation",
    "press release",
    "financial calendar",
    "webcast",
    "factsheet",
    "fund",
    "net asset value",
    "nav",
    "dashboard",
    "ucits",
    "kid",
    "priips",
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
    # Remove common Estonian suffix forms
    return re.sub(
        r"\b(?:as|aktsiaselts|ou|osaühing|osaühingu|se)\b",
        " ",
        normalized,
    ).strip()


def _parse_date(value: object) -> date | None:
    raw = str(value or "").strip()
    for pattern in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%Y-%m-%d",
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
    return None


def classify_estonia_document(
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

    quarterly = (
        "quarterly report",
        "quarter report",
        "first quarter",
        "second quarter",
        "third quarter",
        "fourth quarter",
        "vahearuanne",
        "q1",
        "q2",
        "q3",
        "q4",
    )
    half_year = (
        "half year",
        "half-year",
        "semi annual",
        "semi-annual",
        "poolaastaaruanne",
        "poolaasta",
    )
    interim = (
        "interim report",
        "interim financial",
        "vahekokkuvõte",
    )
    annual = (
        "annual financial report",
        "annual report",
        "audited annual report",
        "annual financial statements",
        "consolidated annual report",
        "standalone annual report",
        "year end report",
        "year-end report",
        "majandusaasta",
        "aastaaruanne",
    )

    rules = (
        ("quarterly_financial_report", quarterly),
        ("half_year_financial_report", half_year),
        ("interim_report", interim),
        ("annual_financial_report", annual),
    )
    for document_type, terms in rules:
        matched = sorted(
            {term for term in terms if _normalize(term) in haystack}
        )
        if matched:
            return (
                document_type,
                f"Periodic report term: {matched[0]}",
                matched,
                [],
            )

    category_normalized = _normalize(category)
    if category_normalized == _normalize("Annual financial report"):
        return (
            "annual_financial_report",
            "OAM Estonia exact annual-report category 57",
            ["57"],
            [],
        )
    if category_normalized == _normalize("Half-yearly financial report"):
        return (
            "half_year_financial_report",
            "OAM Estonia exact half-year-report category 66",
            ["66"],
            [],
        )
    if category_normalized in {
        _normalize("Management interim statement or quarterly financial report"),
        _normalize("Quarterly financial report"),
    }:
        return (
            "quarterly_financial_report",
            "OAM Estonia exact quarterly-report category 55",
            ["55"],
            [],
        )

    return (
        "other_regulatory_announcement",
        "No accepted periodic report category or title term",
        [],
        [],
    )


def extract_estonia_date_info(
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

    classification = classify_estonia_document(
        title,
        category,
        filename,
    )[0]
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
class EstoniaFile:
    attachment_id: str
    filename: str
    download_url: str
    file_format: str | None


@dataclass(frozen=True, slots=True)
class EstoniaNotice:
    record_id: str
    issuer_name: str
    title: str
    category: str
    published_raw: str
    published_at: date | None
    detail_url: str
    files: tuple[EstoniaFile, ...] = ()


@dataclass(frozen=True, slots=True)
class EstoniaListingPage:
    notices: tuple[EstoniaNotice, ...]
    page_count: int


@dataclass(frozen=True, slots=True)
class EstoniaSourceDiagnostic:
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
class EstoniaSourceDiscovery:
    source: str
    query: str
    notices: tuple[EstoniaNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EstoniaIssuerResolution:
    found: bool
    matched_name: str | None = None
    source_record_id: str | None = None
    source_url: str | None = None
    detail_url: str | None = None
    home_member_state: str | None = "Estonia"
    pea_country_check: str | None = "eu_candidate"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


def parse_estonia_listing(
    html_text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> EstoniaListingPage:
    soup = BeautifulSoup(html_text, "html.parser")
    notices: list[EstoniaNotice] = []
    
    table = soup.find("table")
    if not table:
        return EstoniaListingPage(notices=(), page_count=1)
        
    tbody = table.find("tbody")
    rows = tbody.find_all("tr", recursive=False) if tbody else table.find_all("tr", recursive=False)
    
    for row in rows:
        cells = row.find_all("td", recursive=False)
        if len(cells) < 6:
            continue
            
        published_raw = cells[0].get_text(" ", strip=True)
        published_at = _parse_date(published_raw)
        issuer_name = cells[1].get_text(" ", strip=True)
        category = cells[2].get_text(" ", strip=True)
        title = cells[3].get_text(" ", strip=True)
        
        view_link = cells[5].find("a", href=True)
        if not view_link:
            continue
        detail_href = view_link.get("href")
        match = re.search(r"/borsiteated/(\d+)", detail_href)
        if not match:
            continue
        record_id = match.group(1)
        detail_url = urljoin(base_url.rstrip("/") + "/", detail_href.lstrip("/"))
        
        notices.append(
            EstoniaNotice(
                record_id=record_id,
                issuer_name=issuer_name,
                title=title,
                category=category,
                published_raw=published_raw,
                published_at=published_at,
                detail_url=detail_url,
            )
        )
        
    page_numbers = [1]
    for link in soup.select("nav.pager li.page-item a.page-link"):
        href = link.get("href") or ""
        page_match = re.search(r"[?&]page=(\d+)", href)
        if page_match:
            page_numbers.append(int(page_match.group(1)) + 1)
            
    return EstoniaListingPage(
        notices=tuple(notices),
        page_count=max(page_numbers),
    )


def parse_estonia_detail(
    html_text: str,
    notice: EstoniaNotice,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> EstoniaNotice:
    soup = BeautifulSoup(html_text, "html.parser")
    files: list[EstoniaFile] = []
    
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if "/sites/default/files/" not in href:
            continue
            
        text = link.get_text(" ", strip=True)
        filename = re.sub(r"\s*\(\s*\d+(?:\.\d+)?\s*(?:[KkMm][Bb])\s*\)\s*$", "", text)
        if not filename:
            filename = PurePosixPath(urlparse(href).path).name
            
        download_url = urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))
        attachment_id = PurePosixPath(urlparse(href).path).name
        
        files.append(
            EstoniaFile(
                attachment_id=attachment_id,
                filename=filename,
                download_url=download_url,
                file_format=_file_format(filename, download_url),
            )
        )
    return EstoniaNotice(
        record_id=notice.record_id,
        issuer_name=notice.issuer_name,
        title=notice.title,
        category=notice.category,
        published_raw=notice.published_raw,
        published_at=notice.published_at,
        detail_url=notice.detail_url,
        files=tuple(files),
    )


class EstoniaOamConnector(Connector):
    market = "Tallinn Stock Exchange"
    source_name = "estonia_oam"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str = DEFAULT_BASE_URL,
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 30,
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
            tuple[date, date, str], tuple[EstoniaNotice, ...]
        ] = {}
        self._detail_cache: dict[str, EstoniaNotice] = {}
        self._scanned_notices = 0
        self._details_visited = 0
        self._cache_hits = 0

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _fetch_listing(
        self,
        *,
        from_date: date,
        to_date: date,
        document_type: str = "",
        limit: int | None = None,
    ) -> tuple[EstoniaNotice, ...]:
        cache_key = (from_date, to_date, document_type)
        if cache_key in self._listing_cache:
            self._cache_hits += 1
            cached = self._listing_cache[cache_key]
            return cached[:limit] if limit is not None else cached
            
        notices: list[EstoniaNotice] = []
        page_count = 1
        for page in range(1, self.max_pages + 1):
            if page > page_count:
                break
            self._wait()
            response: Any | None = None
            try:
                # Drupal uses GET params for pagination and filters on this endpoint
                params = {
                    "publication_date_from": from_date.strftime("%d.%m.%Y"),
                    "publication_date_to": to_date.strftime("%d.%m.%Y"),
                    "issuer": "",
                    "category": document_type,
                    "title": "",
                    "page": str(page - 1),
                }
                
                response = self.session.get(
                    self.search_url,
                    params=params,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                    },
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
                response.raise_for_status()
                parsed = parse_estonia_listing(
                    response.text,
                    base_url=self.base_url,
                )
                page_count = min(parsed.page_count, self.max_pages)
                notices.extend(parsed.notices)
                self.attempts.append(
                    EndpointAttempt(
                        name=f"OAM Estonia periodic listing page {page}",
                        base_url=self.base_url,
                        dataset="INFO STORAGE",
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
                        name=f"OAM Estonia periodic listing page {page}",
                        base_url=self.base_url,
                        dataset="INFO STORAGE",
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

    def _notice_candidate(self, notice: EstoniaNotice) -> DocumentCandidate:
        document_type, reason, positive, negative = classify_estonia_document(
            notice.title,
            notice.category,
        )
        dates = extract_estonia_date_info(
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
                "issuer_country": "Estonia",
                "home_member_state": "Estonia",
                "pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
                "record_id": notice.record_id,
                "detail_url": notice.detail_url,
                "category": notice.category,
                "received_at": notice.published_raw,
                "parent_page_url": notice.detail_url,
                "estonia_oam_url": self.search_url,
            },
            classification=document_type,
            classification_reason=reason,
            matched_positive_terms=positive,
            matched_negative_terms=negative,
            **dates,
        )

    def _load_detail(self, candidate: DocumentCandidate) -> EstoniaNotice:
        record_id = str(candidate.metadata.get("record_id") or "")
        cached = self._detail_cache.get(record_id)
        if cached is not None:
            self._cache_hits += 1
            return cached
            
        notice = EstoniaNotice(
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
            detailed = parse_estonia_detail(
                response.text,
                notice,
                base_url=self.base_url,
            )
            self._detail_cache[record_id] = detailed
            self._details_visited += 1
            self.attempts.append(
                EndpointAttempt(
                    name=f"OAM Estonia detail {record_id}",
                    base_url=self.base_url,
                    dataset="INFO STORAGE",
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
                    name=f"OAM Estonia detail {record_id}",
                    base_url=self.base_url,
                    dataset="INFO STORAGE",
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
            document_type, reason, positive, negative = (
                classify_estonia_document(
                    notice.title,
                    notice.category,
                    item.filename,
                )
            )
            dates = extract_estonia_date_info(
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
                    source_document_id=(
                        f"{notice.record_id}:{item.attachment_id}"
                    ),
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
        notices = self._fetch_listing(
            from_date=start,
            to_date=end,
            limit=limit,
        )
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

    def resolve_issuer(self, issuer: Issuer) -> EstoniaIssuerResolution:
        end = date.today()
        start = end - timedelta(days=365 * 3)
        expected = _normalize_issuer(issuer.name)
        try:
            best: tuple[float, EstoniaNotice] | None = None
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
                return EstoniaIssuerResolution(
                    found=False,
                    attempts=tuple(self.attempts),
                    error="No matching issuer in OAM Estonia periodic filings",
                )
            score, notice = best
            return EstoniaIssuerResolution(
                found=True,
                matched_name=notice.issuer_name,
                source_record_id=notice.record_id,
                source_url=self.search_url,
                detail_url=notice.detail_url,
                match_score=score,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return EstoniaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def discover(
        self,
        query: str,
        limit: int = 25,
    ) -> EstoniaSourceDiscovery:
        normalized = _normalize(query)
        if "half year" in normalized or "semi annual" in normalized:
            document_type = "66"
        elif "quarter" in normalized:
            document_type = "55"
        elif "interim" in normalized:
            document_type = "55"
        elif "annual" in normalized:
            document_type = "57"
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
            candidates = tuple(
                self._notice_candidate(notice) for notice in notices
            )
            return EstoniaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=tuple(notices),
                candidates=candidates,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return EstoniaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> EstoniaSourceDiagnostic:
        end = date.today()
        start = end - timedelta(days=120)
        try:
            notices = list(
                self._fetch_listing(
                    from_date=start,
                    to_date=end,
                    limit=100,
                )
            )
            categories: dict[str, int] = {}
            for notice in notices:
                categories[notice.category] = categories.get(notice.category, 0) + 1
                
            periodic = next(
                (
                    notice
                    for notice in notices
                    if classify_estonia_document(
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
                    item.file_format
                    for item in detailed.files
                    if item.file_format
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
            return EstoniaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.READY if example else ConnectorState.DEGRADED,
                called_url=self.search_url,
                http_status=status,
                method_used=(
                    "GET global HTML listing with date filters; "
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
            return EstoniaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.search_url,
                http_status=None,
                method_used="GET OAM Estonia HTML listing",
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
        return 1

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 2
