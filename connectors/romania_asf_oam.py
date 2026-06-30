from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from connectors.base import Connector, ConnectorState, DocumentCandidate, EndpointAttempt
from models import Issuer


DEFAULT_BASE_URL = "https://oam.asfromania.ro"
LISTING_PATH = "/oam/loadedPDFReportsForPublic.jsp"
DOWNLOAD_PATH = "/oam/DownloadPDFFile.do"
PERIODIC_PERIOD_TYPES = frozenset({"anuala", "semestriala", "trimestriala"})
DISCOVER_FALLBACK_ISSUER_QUERIES = ("PETROM", "Transilvania", "Romgaz", "Hidroelectrica")
SUPPORTED_FORMATS = {"pdf"}

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
    "prospekt",
    "obligatiuni",
    "obligatie",
    "obligatii",
    "dividend",
    "dividende",
    "plata dividend",
    "convocare",
    "adunarea generala",
    "valabile generale",
    "majorare capital",
    "achizitii",
    "instrainari",
    "calendar financiar",
    "raport curent",
    "comunicat",
    "tlacova sprava",
    "tender",
    "buyback",
    "rcdiv",
    "rc17",
    "rc12",
    "rc21",
    "rc22",
    "rc24",
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
        r"\b(?:sa|s a|s r l|srl|spv)\b",
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
            return suffix
    if "pdf" in _normalize(f"{filename} {url}"):
        return "pdf"
    return None


def _reporting_filename(nume_raportare: str) -> str:
    raw = unquote(nume_raportare).strip()
    if "`" in raw:
        raw = raw.split("`")[-1]
    if not raw.lower().endswith(".pdf"):
        raw = f"{raw}.pdf" if raw else "document.pdf"
    return raw


def build_romania_download_url(
    href: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[str, str]:
    cleaned = str(href or "").strip().lstrip("./")
    if "nume_raportare=" not in cleaned:
        raise ValueError(f"Invalid Romania OAM download href: {href!r}")
    if cleaned.startswith("participants/"):
        cleaned = cleaned[len("participants/") :]

    match = re.search(r"nume_raportare=([^&]+)", cleaned)
    if match is None:
        raise ValueError(f"Missing nume_raportare in Romania OAM href: {href!r}")
    nume_raportare = unquote(match.group(1))
    if not nume_raportare:
        raise ValueError(f"Missing nume_raportare in Romania OAM href: {href!r}")

    filename = _reporting_filename(nume_raportare)
    download_url = (
        f"{base_url.rstrip('/')}{DOWNLOAD_PATH}"
        f"?nume_raportare={nume_raportare}"
    )
    return filename, download_url


def build_romania_listing_url(
    *,
    sort_column: str,
    page: int = 1,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    params = [
        ("xF4F59A60sortDir", "desc"),
        ("xF4F59A60sortColumn", sort_column),
    ]
    if page > 1:
        params.append(("xF4F59A60page", str(page)))
    query = "&".join(f"{key}={value}" for key, value in params)
    return f"{base_url.rstrip('/')}{LISTING_PATH}?{query}"


def classify_romania_document(
    title: str,
    period_type: str = "",
    filename: str = "",
) -> tuple[str, str, list[str], list[str]]:
    haystack = _normalize(" ".join((title, period_type, filename)))
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

    period_normalized = _normalize(period_type)
    if period_normalized == "exceptionala":
        return (
            "other_regulatory_announcement",
            "Romania OAM exceptional report type",
            [],
            ["exceptionala"],
        )

    quarterly = (
        "quarterly report",
        "quarter report",
        "first quarter",
        "second quarter",
        "third quarter",
        "fourth quarter",
        "trimestriala",
        "trimestrial",
        "raport financiar trimestrial",
        "rft",
        "rtrim",
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
        "semestriala",
        "semestrial",
        "situatia semi",
        "semi anuala",
        "rsem",
    )
    interim = (
        "interim report",
        "interim financial",
        "raport interimar",
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
        "anuala",
        "anual",
        "raport anual",
        "situatia anuala",
        "stfinanual",
        "ifrs",
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

    if period_normalized == "anuala":
        return (
            "annual_financial_report",
            "Romania OAM annual period type",
            ["anuala"],
            [],
        )
    if period_normalized == "semestriala":
        return (
            "half_year_financial_report",
            "Romania OAM semi-annual period type",
            ["semestriala"],
            [],
        )
    if period_normalized == "trimestriala":
        return (
            "quarterly_financial_report",
            "Romania OAM quarterly period type",
            ["trimestriala"],
            [],
        )

    return (
        "other_regulatory_announcement",
        "No accepted periodic report category or title term",
        [],
        [],
    )


def extract_romania_date_info(
    title: str,
    published_raw: str | None,
    period_type: str = "",
    refdate_raw: str = "",
    filename: str = "",
) -> dict[str, Any]:
    published_at = _parse_date(published_raw)
    text = " ".join((title, refdate_raw, filename))
    period_end: date | None = None
    reporting_year: int | None = None
    source_period_raw: str | None = refdate_raw or None
    reason = "No unambiguous reporting period detected"

    quarter_match = re.search(
        r"\btrim\s*([1-4])\s*/\s*(20\d{2})\b",
        _normalize(text),
    )
    if quarter_match:
        quarter = int(quarter_match.group(1))
        reporting_year = int(quarter_match.group(2))
        month = {1: 3, 2: 6, 3: 9, 4: 12}[quarter]
        period_end = date(
            reporting_year,
            month,
            31 if month in {3, 12} else 30,
        )
        source_period_raw = quarter_match.group(0)
        reason = "Quarter token in Romania reference period"

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
    if explicit_dates and period_end is None:
        period_end, source_period_raw = max(explicit_dates, key=lambda item: item[0])
        reporting_year = period_end.year
        reason = "Explicit reporting-period date in title or reference period"

    classification = classify_romania_document(title, period_type, filename)[0]
    if reporting_year is None and classification != "other_regulatory_announcement":
        years = [
            int(value)
            for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)
            if published_at is None or int(value) <= published_at.year
        ]
        if years:
            reporting_year = years[-1]
            source_period_raw = str(reporting_year)
            reason = "Reporting year extracted from periodic report title or reference"
            normalized = _normalize(text)
            if classification == "annual_financial_report":
                period_end = date(reporting_year, 12, 31)
                reason += "; annual period end inferred"
            elif classification == "half_year_financial_report":
                period_end = date(reporting_year, 6, 30)
                reason += "; half-year period end inferred"
            elif classification == "quarterly_financial_report" and period_end is None:
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
                    ("trim 1", 3),
                    ("trim 2", 6),
                    ("trim 3", 9),
                    ("trim 4", 12),
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
class RomaniaFile:
    attachment_id: str
    filename: str
    download_url: str
    file_format: str | None


@dataclass(frozen=True, slots=True)
class RomaniaNotice:
    record_id: str
    issuer_name: str
    cui: str | None
    isin: str | None
    title: str
    period_type: str
    refdate_raw: str
    published_raw: str
    published_at: date | None
    language: str | None
    listing_url: str
    files: tuple[RomaniaFile, ...] = ()


@dataclass(frozen=True, slots=True)
class RomaniaListingPage:
    notices: tuple[RomaniaNotice, ...]


@dataclass(frozen=True, slots=True)
class RomaniaSourceDiagnostic:
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
class RomaniaSourceDiscovery:
    source: str
    query: str
    notices: tuple[RomaniaNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RomaniaIssuerResolution:
    found: bool
    matched_name: str | None = None
    source_record_id: str | None = None
    source_url: str | None = None
    detail_url: str | None = None
    home_member_state: str | None = "Romania"
    pea_country_check: str | None = "eu_candidate"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


def _row_download_href(row: Any) -> str | None:
    for link in row.find_all("a", href=True):
        href = str(link.get("href") or "")
        if "DownloadPDFFile" in href:
            return href
    return None


def parse_romania_listing(
    html_text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    listing_url: str = "",
) -> RomaniaListingPage:
    soup = BeautifulSoup(html_text, "html.parser")
    notices: list[RomaniaNotice] = []
    parent_url = listing_url or f"{base_url.rstrip('/')}{LISTING_PATH}"

    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 12:
            continue

        issuer_name = cells[0].get_text(" ", strip=True)
        if not issuer_name:
            continue
        issuer_normalized = _normalize(issuer_name)
        if "filtreaza" in issuer_normalized or "denumire emitent" in issuer_normalized:
            continue

        href = _row_download_href(row)
        if not href:
            continue

        try:
            filename, download_url = build_romania_download_url(href, base_url=base_url)
        except ValueError:
            continue

        title = cells[4].get_text(" ", strip=True)
        period_type = cells[5].get_text(" ", strip=True)
        refdate_raw = cells[6].get_text(" ", strip=True)
        published_raw = cells[7].get_text(" ", strip=True)
        language = cells[10].get_text(" ", strip=True) if len(cells) > 10 else None
        query = urlparse(download_url).query
        record_id = (parse_qs(query).get("nume_raportare") or [filename])[0]
        file_format = _file_format(filename, download_url)

        notices.append(
            RomaniaNotice(
                record_id=record_id,
                issuer_name=issuer_name,
                cui=cells[1].get_text(" ", strip=True) or None,
                isin=cells[2].get_text(" ", strip=True) or None,
                title=title,
                period_type=period_type,
                refdate_raw=refdate_raw,
                published_raw=published_raw,
                published_at=_parse_date(published_raw),
                language=language or None,
                listing_url=parent_url,
                files=(
                    RomaniaFile(
                        attachment_id=record_id,
                        filename=filename,
                        download_url=download_url,
                        file_format=file_format,
                    ),
                ),
            )
        )

    return RomaniaListingPage(notices=tuple(notices))


class RomaniaAsfOamConnector(Connector):
    market = "Bucharest Stock Exchange"
    source_name = "romania_asf_oam"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str = DEFAULT_BASE_URL,
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 365,
        timeout: int = 30,
        verify_ssl: bool = True,
        max_pages: int = 3,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.listing_url = f"{self.base_url}{LISTING_PATH}"
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.max_pages = max(1, max_pages)
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        self._listing_cache: dict[str, tuple[RomaniaNotice, ...]] = {}
        self._notice_cache: dict[str, RomaniaNotice] = {}
        self._scanned_notices = 0
        self._details_visited = 0
        self._cache_hits = 0
        self._session_bootstrapped = False

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _bootstrap_session(self) -> None:
        if self._session_bootstrapped:
            return
        self._wait()
        try:
            self.session.get(
                self.listing_url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            self._session_bootstrapped = True
        finally:
            self._last_request_at = time.monotonic()

    def _fetch_listing_page(
        self,
        *,
        sort_column: str,
        page: int,
    ) -> tuple[RomaniaNotice, ...]:
        cache_key = f"{sort_column}:{page}"
        if cache_key in self._listing_cache:
            self._cache_hits += 1
            return self._listing_cache[cache_key]

        self._bootstrap_session()
        listing_url = build_romania_listing_url(
            sort_column=sort_column,
            page=page,
            base_url=self.base_url,
        )
        self._wait()
        response: Any | None = None
        try:
            response = self.session.get(
                listing_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": self.listing_url,
                },
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            parsed = parse_romania_listing(
                response.text,
                base_url=self.base_url,
                listing_url=listing_url,
            )
            result = tuple(parsed.notices)
            self._listing_cache[cache_key] = result
            for notice in result:
                self._notice_cache[notice.record_id] = notice
            self._scanned_notices += len(result)
            self.attempts.append(
                EndpointAttempt(
                    name=(
                        f"ASF OAM Romania listing {sort_column} page {page}"
                    ),
                    base_url=self.base_url,
                    dataset="OAM",
                    endpoint=LISTING_PATH,
                    method="GET",
                    http_status=response.status_code,
                    success=True,
                    total_count=len(result),
                )
            )
            self.state = ConnectorState.READY
            self.last_error = None
            return result
        except Exception as exc:
            self.state = ConnectorState.UNAVAILABLE
            self.last_error = str(exc)
            self.attempts.append(
                EndpointAttempt(
                    name=(
                        f"ASF OAM Romania listing {sort_column} page {page}"
                    ),
                    base_url=self.base_url,
                    dataset="OAM",
                    endpoint=LISTING_PATH,
                    method="GET",
                    http_status=getattr(response, "status_code", None),
                    success=False,
                    error=str(exc),
                )
            )
            raise
        finally:
            self._last_request_at = time.monotonic()

    def _fetch_recent_notices(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> tuple[RomaniaNotice, ...]:
        collected: list[RomaniaNotice] = []
        seen: set[str] = set()
        for sort_column in ("refdate", "Time"):
            for page in range(1, self.max_pages + 1):
                for notice in self._fetch_listing_page(
                    sort_column=sort_column,
                    page=page,
                ):
                    if notice.record_id in seen:
                        continue
                    if since and notice.published_at and notice.published_at < since:
                        continue
                    if not self._accepted_notice(notice):
                        continue
                    seen.add(notice.record_id)
                    collected.append(notice)
                    if limit is not None and len(collected) >= limit:
                        return tuple(collected)
        return tuple(collected)

    def _accepted_notice(self, notice: RomaniaNotice) -> bool:
        if _normalize(notice.period_type) == "exceptionala":
            return (
                classify_romania_document(
                    notice.title,
                    notice.period_type,
                )[0]
                != "other_regulatory_announcement"
            )
        if _normalize(notice.period_type) in PERIODIC_PERIOD_TYPES:
            return True
        return (
            classify_romania_document(
                notice.title,
                notice.period_type,
            )[0]
            != "other_regulatory_announcement"
        )

    def _notice_candidate(self, notice: RomaniaNotice) -> DocumentCandidate:
        primary = notice.files[0] if notice.files else None
        document_type, reason, positive, negative = classify_romania_document(
            notice.title,
            notice.period_type,
            primary.filename if primary else "",
        )
        dates = extract_romania_date_info(
            notice.title,
            notice.published_raw,
            notice.period_type,
            notice.refdate_raw,
            primary.filename if primary else "",
        )
        return DocumentCandidate(
            title=notice.title,
            url=primary.download_url if primary else notice.listing_url,
            published_date=dates["published_at"],
            document_type=document_type,
            source=self.source_name,
            source_document_id=notice.record_id,
            metadata={
                "official_source": 1,
                "issuer_name": notice.issuer_name,
                "issuer_cui": notice.cui,
                "issuer_isin": notice.isin,
                "issuer_country": "Romania",
                "home_member_state": "Romania",
                "pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
                "record_id": notice.record_id,
                "period_type": notice.period_type,
                "refdate_raw": notice.refdate_raw,
                "language": notice.language,
                "received_at": notice.published_raw,
                "parent_page_url": notice.listing_url,
                "romania_asf_oam_url": self.listing_url,
                "files": [
                    {
                        "attachment_id": item.attachment_id,
                        "filename": item.filename,
                        "download_url": item.download_url,
                        "file_format": item.file_format,
                    }
                    for item in notice.files
                ],
            },
            classification=document_type,
            classification_reason=reason,
            matched_positive_terms=positive,
            matched_negative_terms=negative,
            **dates,
        )

    def _notice_from_candidate(
        self,
        candidate: DocumentCandidate,
    ) -> RomaniaNotice | None:
        record_id = str(candidate.metadata.get("record_id") or "")
        cached = self._notice_cache.get(record_id)
        if cached is not None:
            self._cache_hits += 1
            return cached

        files_meta = candidate.metadata.get("files") or []
        files: list[RomaniaFile] = []
        for item in files_meta:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or item.get("attachment_id") or "")
            download_url = str(item.get("download_url") or candidate.url)
            files.append(
                RomaniaFile(
                    attachment_id=str(item.get("attachment_id") or filename),
                    filename=filename,
                    download_url=download_url,
                    file_format=item.get("file_format"),
                )
            )
        if not files:
            return None
        return RomaniaNotice(
            record_id=record_id,
            issuer_name=str(candidate.metadata.get("issuer_name") or ""),
            cui=str(candidate.metadata.get("issuer_cui") or "") or None,
            isin=str(candidate.metadata.get("issuer_isin") or "") or None,
            title=candidate.title,
            period_type=str(candidate.metadata.get("period_type") or ""),
            refdate_raw=str(candidate.metadata.get("refdate_raw") or ""),
            published_raw=candidate.source_publication_date_raw or "",
            published_at=candidate.published_at,
            language=str(candidate.metadata.get("language") or "") or None,
            listing_url=str(
                candidate.metadata.get("parent_page_url") or self.listing_url
            ),
            files=tuple(files),
        )

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        if candidate.document_type == "other_regulatory_announcement":
            return [candidate]

        notice = self._notice_from_candidate(candidate)
        if notice is None:
            return [candidate]

        materialized: list[DocumentCandidate] = []
        for item in notice.files:
            document_type, reason, positive, negative = classify_romania_document(
                notice.title,
                notice.period_type,
                item.filename,
            )
            if document_type == "other_regulatory_announcement":
                continue
            dates = extract_romania_date_info(
                notice.title,
                notice.published_raw,
                notice.period_type,
                notice.refdate_raw,
                item.filename,
            )
            metadata = dict(candidate.metadata)
            metadata.update(
                {
                    "attachment_id": item.attachment_id,
                    "file_id": item.attachment_id,
                    "filename": item.filename,
                    "file_format": item.file_format,
                    "parent_page_url": notice.listing_url,
                }
            )
            metadata.pop("files", None)
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
        return materialized or [candidate]

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
        notices = self._fetch_recent_notices(since=start, limit=limit)
        candidates = [self._notice_candidate(notice) for notice in notices]
        return candidates[:limit] if limit is not None else candidates

    def search_documents_for_issuer(
        self,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        end = date.today()
        start = end - timedelta(days=self.lookback_days)
        expected = _normalize_issuer(issuer.name)
        isin_expected = str(issuer.isin or "").strip().casefold()
        notices = self._fetch_recent_notices(since=start, limit=None)
        matched: list[DocumentCandidate] = []
        for notice in notices:
            observed = _normalize_issuer(notice.issuer_name)
            isin_observed = str(notice.isin or "").strip().casefold()
            if isin_expected and isin_observed == isin_expected:
                matched.append(self._notice_candidate(notice))
                continue
            if expected and (
                expected == observed
                or expected in observed
                or observed in expected
            ):
                matched.append(self._notice_candidate(notice))
        return matched

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def resolve_issuer(self, issuer: Issuer) -> RomaniaIssuerResolution:
        expected = _normalize_issuer(issuer.name)
        isin_expected = str(issuer.isin or "").strip().casefold()
        try:
            best: tuple[float, RomaniaNotice] | None = None
            for notice in self._fetch_recent_notices(since=None, limit=200):
                observed = _normalize_issuer(notice.issuer_name)
                isin_observed = str(notice.isin or "").strip().casefold()
                score = 0.0
                if isin_expected and isin_observed == isin_expected:
                    score = 100.0
                elif expected == observed:
                    score = 100.0
                elif expected and (expected in observed or observed in expected):
                    score = 85.0
                if score and (best is None or score > best[0]):
                    best = (score, notice)
            if best is None:
                return RomaniaIssuerResolution(
                    found=False,
                    attempts=tuple(self.attempts),
                    error="No matching issuer in ASF OAM Romania filings",
                )
            score, notice = best
            primary = notice.files[0] if notice.files else None
            return RomaniaIssuerResolution(
                found=True,
                matched_name=notice.issuer_name,
                source_record_id=notice.record_id,
                source_url=self.listing_url,
                detail_url=primary.download_url if primary else self.listing_url,
                match_score=score,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return RomaniaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def discover(self, query: str, limit: int = 25) -> RomaniaSourceDiscovery:
        normalized = _normalize(query)
        period_filter = ""
        issuer_query = query.strip()
        if "half year" in normalized or "semi annual" in normalized or "semestriala" in normalized:
            period_filter = "semestriala"
            issuer_query = ""
        elif "quarter" in normalized or "trimestriala" in normalized:
            period_filter = "trimestriala"
            issuer_query = ""
        elif "annual" in normalized or "anuala" in normalized:
            period_filter = "anuala"
            issuer_query = ""

        try:
            notices: list[RomaniaNotice] = []
            seen: set[str] = set()
            queries = (
                (issuer_query,)
                if issuer_query
                else DISCOVER_FALLBACK_ISSUER_QUERIES
            )
            for issuer_name in queries:
                for notice in self._fetch_recent_notices(since=None, limit=limit * 4):
                    if notice.record_id in seen:
                        continue
                    if period_filter and _normalize(notice.period_type) != period_filter:
                        continue
                    if issuer_query:
                        observed = _normalize_issuer(notice.issuer_name)
                        expected = _normalize_issuer(issuer_name)
                        if expected not in observed and observed not in expected:
                            continue
                    if not self._accepted_notice(notice):
                        continue
                    seen.add(notice.record_id)
                    notices.append(notice)
                    if len(notices) >= limit:
                        break
                if len(notices) >= limit:
                    break
            notice_tuple = tuple(notices[:limit])
            candidates = tuple(
                self._notice_candidate(notice) for notice in notice_tuple
            )
            return RomaniaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=notice_tuple,
                candidates=candidates,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return RomaniaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> RomaniaSourceDiagnostic:
        try:
            notices = list(self._fetch_recent_notices(since=None, limit=200))
            categories: dict[str, int] = {}
            for notice in notices:
                key = notice.period_type or "unknown"
                categories[key] = categories.get(key, 0) + 1

            periodic = next(
                (
                    notice
                    for notice in notices
                    if self._accepted_notice(notice)
                    and classify_romania_document(
                        notice.title,
                        notice.period_type,
                    )[0]
                    != "other_regulatory_announcement"
                ),
                None,
            )
            formats: set[str] = set()
            attachment_count = 0
            example = None
            if periodic is not None:
                attachment_count = len(periodic.files)
                formats.update(
                    item.file_format for item in periodic.files if item.file_format
                )
                example = {
                    "record_id": periodic.record_id,
                    "issuer": periodic.issuer_name,
                    "isin": periodic.isin,
                    "cui": periodic.cui,
                    "title": periodic.title,
                    "period_type": periodic.period_type,
                    "refdate_raw": periodic.refdate_raw,
                    "published_at": (
                        periodic.published_at.isoformat()
                        if periodic.published_at
                        else None
                    ),
                    "listing_url": periodic.listing_url,
                    "files": [
                        {
                            "attachment_id": item.attachment_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": item.download_url,
                        }
                        for item in periodic.files
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
            return RomaniaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.READY if example else ConnectorState.DEGRADED,
                called_url=self.listing_url,
                http_status=status,
                method_used=(
                    "GET global ASF OAM listing sorted by refdate and Time; "
                    "PDF URLs normalized to /oam/DownloadPDFFile.do; "
                    "no detail fetch"
                ),
                total_count=len(notices),
                detected_count=len(notices),
                attachment_count=attachment_count,
                fields=(
                    "record_id",
                    "issuer_name",
                    "issuer_cui",
                    "issuer_isin",
                    "title",
                    "period_type",
                    "refdate_raw",
                    "published_at",
                    "language",
                    "download_url",
                ),
                categories=categories,
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=len(self.attempts),
                request_efficiency=(
                    "Bounded listing GET per sort column and page; "
                    "materialization uses listing metadata only; cache hits: "
                    f"{self._cache_hits}"
                ),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return RomaniaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.listing_url,
                http_status=None,
                method_used="GET ASF OAM Romania HTML listing",
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
        return self.max_pages * 2

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return self.max_pages * 2