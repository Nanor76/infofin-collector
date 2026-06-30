from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, unquote, urlparse

import requests

from connectors.base import Connector, ConnectorState, DocumentCandidate, EndpointAttempt
from models import Issuer


DEFAULT_BASE_URL = "https://download.bse-sofia.bg"
COMPANIES_PATH = "/x3news_companies/"
SUPPORTED_FORMATS = {"pdf", "zip"}
DISCOVER_FALLBACK_ISSUER_QUERIES = ("Тибиш", "Интерсолар", "DKJ")

PERIODIC_BUCKET_MARKERS = (
    "finansovi otche",
    "konsolidirani",
    "godishni finansovi",
    "финансови отчети",
    "консолидирани",
    "годишни финансови",
)

REJECT_BUCKET_MARKERS = (
    "vytreshna informacia",
    "вътрешна информация",
    "чл. 17",
    "чл.17",
)

NEGATIVE_TERMS = (
    "prospectus",
    "prospekt",
    "bondreport",
    "bond report",
    "pokana",
    "convoc",
    "general meeting",
    "share buyback",
    "tender offer",
    "capital increase",
    "rights issue",
    "dividend",
    "voting rights",
    "press release",
    "presentation",
    "investor presentation",
    "corporate governance",
    "governance code",
    "financial calendar",
    "webcast",
    "factsheet",
    "fund",
    "ucits",
    "kid",
    "priips",
    "spravki na kfn",
    "справки на кфн",
    "deklaracia chl 100",
    "декларация чл.100",
    "декларация чл. 100",
    "vytreshna informacia",
    "вътрешна информация",
    "чл.7",
    "чл. 7",
    "mar",
    "reglament es 596",
    "регламент ес 596",
)

POSITIVE_FILE_TERMS = (
    "gfo",
    "гфо",
    "godishen doklad",
    "годишен доклад",
    "doklad za deinostta",
    "доклад за дейността",
    "doklad za deynostta",
    "finansov otch",
    "финансов отчет",
    "forma 1",
    "форма 1",
    "forma1",
    "oditorski doklad",
    "одиторски доклад",
    "polugodishen",
    "полугодишен",
    "mezhdinen",
    "междинен",
    "trimest",
    "тримест",
    "konsolidiran",
    "консолидиран",
    "annual financial",
    "half year",
    "quarterly",
)


def _normalize(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9\u0400-\u04ff]+", " ", ascii_value.casefold()).strip()


def _normalize_issuer(value: object) -> str:
    normalized = _normalize(value)
    return re.sub(
        r"\b(?:ad|ead|eood|ood|doo|plc|sa)\b",
        " ",
        normalized,
    ).strip()


def _issuer_query_match(
    query: str,
    issuer_name: str,
    *,
    mode: str = "strict",
) -> bool:
    normalized_query = _normalize_issuer(query)
    if not normalized_query:
        return True
    observed = _normalize_issuer(issuer_name)
    if normalized_query == observed:
        return True
    if normalized_query in observed or observed in normalized_query:
        return True
    if mode == "discover":
        prefix_len = min(3, len(normalized_query), len(observed))
        if prefix_len >= 3 and normalized_query[:prefix_len] == observed[:prefix_len]:
            return True
    return False


def _decode_display_name(value: str) -> str:
    try:
        return unquote(value, encoding="cp1251")
    except Exception:
        return unquote(value)


def _parse_last_modified(value: object) -> date | None:
    raw = str(value or "").strip()
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def _parse_period_end(bucket_name: str) -> date | None:
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", bucket_name)
    if not match:
        match = re.search(r"(20\d{2})\s*г", bucket_name)
        if match:
            return date(int(match.group(1)), 12, 31)
        return None
    day, month, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _file_format(filename: str, url: str = "") -> str | None:
    for value in (filename, urlparse(url).path):
        suffix = PurePosixPath(value).suffix.casefold().lstrip(".")
        if suffix in SUPPORTED_FORMATS:
            return suffix
    return None


def _bucket_kind(bucket_name: str) -> str:
    normalized = _normalize(bucket_name)
    if any(marker in normalized for marker in REJECT_BUCKET_MARKERS):
        return "reject"
    if any(marker in normalized for marker in PERIODIC_BUCKET_MARKERS):
        return "periodic"
    return "other"


def classify_bulgaria_document(
    filename: str,
    *,
    bucket_name: str = "",
) -> tuple[str, str, list[str], list[str]]:
    haystack = _normalize(" ".join((filename, bucket_name)))
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

    bucket_type = _bucket_kind(bucket_name)
    if bucket_type == "reject":
        return (
            "other_regulatory_announcement",
            "Rejected bucket type",
            [],
            ["reject_bucket"],
        )
    if bucket_name and bucket_type != "periodic":
        return (
            "other_regulatory_announcement",
            "Non-periodic bucket",
            [],
            ["non_periodic_bucket"],
        )

    period_end = _parse_period_end(bucket_name)
    quarterly_markers = ("30.09", "31.03", "trimest", "тримест", "q1", "q2", "q3", "q4")
    half_year_markers = ("30.06", "semi", "half year", "polugod", "полугод")
    annual_markers = ("31.12", "godishen", "годишен", "annual", "doklad za deinostta")

    if any(marker in haystack for marker in quarterly_markers) or (
        period_end and period_end.month in {3, 9}
    ):
        doc_type = "quarterly_financial_report"
    elif any(marker in haystack for marker in half_year_markers) or (
        period_end and period_end.month == 6
    ):
        doc_type = "half_year_financial_report"
    elif any(marker in haystack for marker in annual_markers) or (
        period_end and period_end.month == 12
    ):
        doc_type = "annual_financial_report"
    else:
        matched = sorted(
            term for term in POSITIVE_FILE_TERMS if _normalize(term) in haystack
        )
        if not matched:
            return (
                "other_regulatory_announcement",
                "No accepted periodic report term",
                [],
                [],
            )
        doc_type = "annual_financial_report"
        return (
            doc_type,
            f"Periodic report term: {matched[0]}",
            matched,
            [],
        )

    matched = sorted(
        term for term in POSITIVE_FILE_TERMS if _normalize(term) in haystack
    )
    if not matched and doc_type == "annual_financial_report":
        if "декларация" in haystack or "deklaracia" in haystack:
            return (
                "other_regulatory_announcement",
                "Standalone declaration file",
                [],
                ["declaration_only"],
            )
    if not matched and filename.casefold().endswith(".zip"):
        zip_terms = ("oditorski", "одиторски", "gfo", "гфо", "finansov", "финансов")
        matched = [term for term in zip_terms if _normalize(term) in haystack]
        if not matched:
            return (
                "other_regulatory_announcement",
                "ZIP without periodic report markers",
                [],
                ["zip_without_periodic_marker"],
            )

    if not matched:
        return (
            "other_regulatory_announcement",
            "No accepted periodic report term",
            [],
            [],
        )
    return (
        doc_type,
        f"Periodic report term: {matched[0]}",
        matched,
        [],
    )


def extract_bulgaria_date_info(
    *,
    bucket_name: str,
    last_modified: str = "",
    filename: str = "",
) -> dict[str, Any]:
    published_at = _parse_last_modified(last_modified)
    period_end = _parse_period_end(bucket_name)
    reporting_year = period_end.year if period_end else None
    return {
        "published_at": published_at,
        "period_end_date": period_end,
        "reporting_year": reporting_year,
        "source_publication_date_raw": last_modified or None,
        "source_period_date_raw": period_end.isoformat() if period_end else None,
        "date_confidence": "medium" if published_at else "low",
        "date_extraction_reason": (
            "Apache Last-Modified proxy; period end from bucket name"
        ),
    }


@dataclass(frozen=True, slots=True)
class BulgariaIndexEntry:
    href: str
    name: str
    last_modified: str
    size: int | None = None


@dataclass(frozen=True, slots=True)
class BulgariaFileEntry:
    href: str
    filename: str
    last_modified: str
    size: int | None
    download_url: str
    file_format: str


@dataclass(frozen=True, slots=True)
class BulgariaFiling:
    source_document_id: str
    issuer_name: str
    bucket_name: str
    bucket_href: str
    filename: str
    download_url: str
    file_format: str
    last_modified: str
    published_at: date | None
    period_end_date: date | None
    reporting_year: int | None


@dataclass(frozen=True, slots=True)
class BulgariaSourceDiagnostic:
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
class BulgariaSourceDiscovery:
    source: str
    query: str
    filings: tuple[BulgariaFiling, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


def parse_apache_index(html: str) -> list[BulgariaIndexEntry]:
    pattern = re.compile(
        r'<a href="([^"]+)">([^<]+)</a></td>'
        r'<td align="right">\s*([^<]*?)\s*</td>'
        r'<td align="right">\s*([^<]*?)\s*</td>',
        re.I | re.S,
    )
    rows: list[BulgariaIndexEntry] = []
    for href, name, modified, size in pattern.findall(html):
        if href in ("../", "/") or href.startswith("?"):
            continue
        size_clean = size.strip().replace(",", "")
        rows.append(
            BulgariaIndexEntry(
                href=href,
                name=unescape(name.strip()),
                last_modified=modified.strip(),
                size=int(size_clean) if size_clean.isdigit() else None,
            )
        )
    return rows


def _stable_document_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def select_active_buckets(
    buckets: list[BulgariaIndexEntry],
    *,
    since: date | None,
    max_buckets: int,
    today: date | None = None,
) -> list[BulgariaIndexEntry]:
    current = today or date.today()
    periodic = [
        bucket
        for bucket in buckets
        if _bucket_kind(bucket.name) == "periodic"
    ]
    scored: list[tuple[tuple[int, int, date], BulgariaIndexEntry]] = []
    for bucket in periodic:
        bucket_modified = _parse_last_modified(bucket.last_modified) or date.min
        period_end = _parse_period_end(bucket.name) or date.min
        freshness = 0
        if since is None or bucket_modified >= since:
            freshness += 2
        if period_end >= current - timedelta(days=400):
            freshness += 2
        if period_end.month in {6, 12}:
            freshness += 1
        scored.append(((freshness, bucket_modified.toordinal(), period_end), bucket))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected: list[BulgariaIndexEntry] = []
    seen_names: set[str] = set()
    for _, bucket in scored:
        key = _normalize(bucket.name)
        if key in seen_names:
            continue
        seen_names.add(key)
        selected.append(bucket)
        if len(selected) >= max_buckets:
            break
    annual_candidates = sorted(
        (
            bucket
            for bucket in periodic
            if "31.12" in bucket.name
        ),
        key=lambda bucket: _parse_period_end(bucket.name) or date.min,
        reverse=True,
    )
    if annual_candidates:
        latest_annual = annual_candidates[0]
        if _normalize(latest_annual.name) not in seen_names:
            if len(selected) >= max_buckets:
                selected = selected[: max_buckets - 1]
            selected.append(latest_annual)
    return selected[:max_buckets]


class BulgariaBseX3NewsConnector(Connector):
    market = "Bulgarian Stock Exchange"
    source_name = "bulgaria_bse_x3news"
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
        max_active_buckets: int = 3,
        max_issuer_scans: int = 30,
        max_candidates_per_source: int = 40,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.companies_url = f"{self.base_url}{COMPANIES_PATH}"
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.max_active_buckets = max(1, max_active_buckets)
        self.max_issuer_scans = max(1, max_issuer_scans)
        self.max_candidates_per_source = max(1, max_candidates_per_source)
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        self._index_cache: dict[str, list[BulgariaIndexEntry]] = {}
        self._filing_cache: dict[str, BulgariaFiling] = {}
        self._scanned_notices = 0
        self._issuer_scans = 0

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _fetch_text(self, url: str, *, label: str) -> str:
        self._wait()
        response: requests.Response | None = None
        try:
            response = self.session.get(
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            self.attempts.append(
                EndpointAttempt(
                    name=label,
                    base_url=self.base_url,
                    dataset="x3news_companies",
                    endpoint=urlparse(url).path,
                    method="GET",
                    http_status=response.status_code,
                    success=True,
                )
            )
            self.state = ConnectorState.READY
            self.last_error = None
            return response.text
        except Exception as exc:
            self.state = ConnectorState.UNAVAILABLE
            self.last_error = str(exc)
            self.attempts.append(
                EndpointAttempt(
                    name=label,
                    base_url=self.base_url,
                    dataset="x3news_companies",
                    endpoint=urlparse(url).path,
                    method="GET",
                    http_status=getattr(response, "status_code", None),
                    success=False,
                    error=str(exc),
                )
            )
            raise
        finally:
            self._last_request_at = time.monotonic()

    def _fetch_index(self, url: str, *, label: str) -> list[BulgariaIndexEntry]:
        if url in self._index_cache:
            return self._index_cache[url]
        html = self._fetch_text(url, label=label)
        parsed = parse_apache_index(html)
        self._index_cache[url] = parsed
        return parsed

    def _issuer_should_scan(
        self,
        issuer: BulgariaIndexEntry,
        bucket: BulgariaIndexEntry,
        since: date | None,
    ) -> bool:
        issuer_modified = _parse_last_modified(issuer.last_modified)
        bucket_modified = _parse_last_modified(bucket.last_modified)
        if since is None:
            return True
        if issuer_modified and issuer_modified >= since:
            return True
        if bucket_modified and bucket_modified >= since:
            return True
        period_end = _parse_period_end(bucket.name)
        if period_end:
            publication_window_end = period_end + timedelta(days=450)
            if publication_window_end >= since:
                return True
        return False

    def _build_filing(
        self,
        *,
        issuer_name: str,
        bucket: BulgariaIndexEntry,
        bucket_url: str,
        file_entry: BulgariaIndexEntry,
        file_url: str,
    ) -> BulgariaFiling | None:
        file_format = _file_format(file_entry.name, file_url)
        if file_format not in SUPPORTED_FORMATS:
            return None
        document_type, _, _, _ = classify_bulgaria_document(
            file_entry.name,
            bucket_name=bucket.name,
        )
        if document_type == "other_regulatory_announcement":
            return None
        dates = extract_bulgaria_date_info(
            bucket_name=bucket.name,
            last_modified=file_entry.last_modified,
            filename=file_entry.name,
        )
        return BulgariaFiling(
            source_document_id=_stable_document_id(file_url),
            issuer_name=issuer_name.rstrip("/").strip(),
            bucket_name=bucket.name,
            bucket_href=bucket.href,
            filename=file_entry.name,
            download_url=file_url,
            file_format=file_format,
            last_modified=file_entry.last_modified,
            published_at=dates["published_at"],
            period_end_date=dates["period_end_date"],
            reporting_year=dates["reporting_year"],
        )

    def _collect_filings(
        self,
        *,
        since: date | None,
        limit: int | None,
        issuer_query: str = "",
        match_mode: str = "strict",
    ) -> list[BulgariaFiling]:
        root_entries = self._fetch_index(
            self.companies_url,
            label="BSE x3news companies index",
        )
        buckets = select_active_buckets(
            root_entries,
            since=since,
            max_buckets=self.max_active_buckets,
        )
        filings: list[BulgariaFiling] = []
        issuer_scans = 0
        for bucket in buckets:
            bucket_url = urljoin(self.companies_url, bucket.href)
            issuers = self._fetch_index(
                bucket_url,
                label=f"BSE x3news bucket {bucket.name}",
            )
            for issuer in issuers:
                if issuer_scans >= self.max_issuer_scans:
                    break
                issuer_name = issuer.name.rstrip("/")
                if issuer_query and not _issuer_query_match(
                    issuer_query,
                    issuer_name,
                    mode=match_mode,
                ):
                    continue
                if not self._issuer_should_scan(issuer, bucket, since):
                    continue
                issuer_url = urljoin(bucket_url, issuer.href)
                issuer_scans += 1
                self._issuer_scans += 1
                files = self._fetch_index(
                    issuer_url,
                    label=f"BSE x3news issuer {issuer_name}",
                )
                for file_entry in files:
                    file_url = urljoin(issuer_url, file_entry.href)
                    filing = self._build_filing(
                        issuer_name=issuer_name,
                        bucket=bucket,
                        bucket_url=bucket_url,
                        file_entry=file_entry,
                        file_url=file_url,
                    )
                    if filing is None:
                        continue
                    filings.append(filing)
                    self._filing_cache[filing.source_document_id] = filing
                    if limit is not None and len(filings) >= limit:
                        return filings
            if issuer_scans >= self.max_issuer_scans:
                break
        self._scanned_notices = len(filings)
        return filings

    def _filing_candidate(self, filing: BulgariaFiling) -> DocumentCandidate:
        document_type, reason, positive, negative = classify_bulgaria_document(
            filing.filename,
            bucket_name=filing.bucket_name,
        )
        dates = extract_bulgaria_date_info(
            bucket_name=filing.bucket_name,
            last_modified=filing.last_modified,
            filename=filing.filename,
        )
        issuer_aliases = {
            filing.issuer_name,
            filing.issuer_name.rstrip("/"),
            _decode_display_name(filing.issuer_name),
        }
        return DocumentCandidate(
            title=f"{filing.bucket_name} - {filing.filename}",
            url=filing.download_url,
            published_date=dates["published_at"],
            document_type=document_type,
            source=self.source_name,
            source_document_id=filing.source_document_id,
            metadata={
                "official_source": 1,
                "issuer_name": filing.issuer_name,
                "issuer_aliases": sorted(issuer_aliases),
                "strict_issuer_name_match": True,
                "issuer_country": "Bulgaria",
                "home_member_state": "Bulgaria",
                "pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
                "bucket_name": filing.bucket_name,
                "bucket_href": filing.bucket_href,
                "filename": filing.filename,
                "file_id": filing.source_document_id,
                "file_format": filing.file_format,
                "parent_page_url": self.companies_url,
                "bulgaria_bse_x3news_url": self.companies_url,
                "last_modified": filing.last_modified,
            },
            classification=document_type,
            classification_reason=reason,
            matched_positive_terms=positive,
            matched_negative_terms=negative,
            **dates,
        )

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != self.market.casefold():
            return []
        effective_limit = min(
            limit or self.max_candidates_per_source,
            self.max_candidates_per_source,
        )
        start = since or (date.today() - timedelta(days=self.lookback_days))
        filings = self._collect_filings(since=start, limit=effective_limit)
        return [self._filing_candidate(filing) for filing in filings]

    def search_documents_for_issuer(self, issuer: Issuer) -> list[DocumentCandidate]:
        start = date.today() - timedelta(days=self.lookback_days)
        filings = self._collect_filings(
            since=start,
            limit=self.max_candidates_per_source,
            issuer_query=issuer.name,
        )
        expected = _normalize_issuer(issuer.name)
        matched: list[DocumentCandidate] = []
        for filing in filings:
            observed = _normalize_issuer(filing.issuer_name)
            if expected == observed or expected in observed or observed in expected:
                matched.append(self._filing_candidate(filing))
        return matched

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        return [candidate]

    def discover(self, query: str, limit: int = 25) -> BulgariaSourceDiscovery:
        try:
            filings = self._collect_filings(
                since=date.today() - timedelta(days=self.lookback_days),
                limit=limit,
                issuer_query=query.strip(),
                match_mode="discover",
            )
            if not filings and not query.strip():
                for fallback in DISCOVER_FALLBACK_ISSUER_QUERIES:
                    filings = self._collect_filings(
                        since=date.today() - timedelta(days=self.lookback_days),
                        limit=limit,
                        issuer_query=fallback,
                    )
                    if filings:
                        break
            candidates = tuple(
                self._filing_candidate(filing) for filing in filings[:limit]
            )
            return BulgariaSourceDiscovery(
                source=self.source_name,
                query=query,
                filings=tuple(filings[:limit]),
                candidates=candidates,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return BulgariaSourceDiscovery(
                source=self.source_name,
                query=query,
                filings=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> BulgariaSourceDiagnostic:
        try:
            root_entries = self._fetch_index(
                self.companies_url,
                label="BSE x3news diagnostic index",
            )
            buckets = [
                bucket
                for bucket in root_entries
                if _bucket_kind(bucket.name) == "periodic"
            ]
            categories: dict[str, int] = {}
            formats: dict[str, int] = {}
            example: dict[str, Any] | None = None
            filings = self._collect_filings(
                since=date.today() - timedelta(days=self.lookback_days),
                limit=10,
            )
            for filing in filings:
                categories[filing.bucket_name] = (
                    categories.get(filing.bucket_name, 0) + 1
                )
                formats[filing.file_format] = formats.get(filing.file_format, 0) + 1
            if filings:
                first = filings[0]
                example = {
                    "issuer_name": first.issuer_name,
                    "bucket_name": first.bucket_name,
                    "filename": first.filename,
                    "download_url": first.download_url,
                    "last_modified": first.last_modified,
                    "file_format": first.file_format,
                }
            return BulgariaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.READY,
                called_url=self.companies_url,
                http_status=200,
                method_used="GET Apache index",
                total_count=len(buckets),
                detected_count=len(filings),
                attachment_count=sum(formats.values()),
                fields=(
                    "issuer_name",
                    "bucket_name",
                    "filename",
                    "last_modified",
                    "download_url",
                    "period_end_date",
                ),
                categories=categories,
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=len(self.attempts),
                request_efficiency=(
                    f"{len(self.attempts)} HTTP calls; "
                    f"{self.max_active_buckets} buckets max; "
                    f"{self.max_issuer_scans} issuer scans max"
                ),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return BulgariaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.companies_url,
                http_status=None,
                method_used="GET Apache index",
                total_count=0,
                detected_count=0,
                attachment_count=0,
                fields=(),
                categories={},
                formats=(),
                example_notice=None,
                http_calls=len(self.attempts),
                request_efficiency="Diagnostic failed",
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        return 1 + self.max_active_buckets + min(self.max_issuer_scans, 30)

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 1 + self.max_active_buckets + 1