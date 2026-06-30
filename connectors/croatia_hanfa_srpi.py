from __future__ import annotations

import html
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from connectors.base import Connector, ConnectorState, DocumentCandidate, EndpointAttempt
from models import Issuer


DEFAULT_BASE_URL = "https://www.hanfa.hr"
SEARCH_PATH = "/Api/SRPI/GetData"
REGISTER_PATH = (
    "/areas-of-supervision/capital-market/"
    "officially-appointed-mechanism-for-the-central-storage-of-regulated-information/"
)

CATEGORIES = {
    "17": ("Annual financial report (art.462. CMA)", "annual_financial_report"),
    "24": ("Semi-annual financial report", "half_year_financial_report"),
    "18": ("Quarterly financial report (art.468.CMA)", "quarterly_financial_report"),
}
SUPPORTED_FORMATS = {"pdf", "zip", "xhtml", "xht", "xml", "xbri"}


def _normalize(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()
    return re.sub(
        r"\b(?:d d|d o o|dd|doo|u stecaju|stecaj)\b",
        " ",
        normalized,
    ).strip()


def _parse_published(value: object) -> date | None:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _extract_int(label: str, value: str) -> int | None:
    match = re.search(rf"{label}\s*:\s*</span>\s*(\d+)", value, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _extract_isins(*values: object) -> tuple[str, ...]:
    found: set[str] = set()
    for value in values:
        found.update(
            re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", str(value or "").upper())
        )
    return tuple(sorted(found))


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a":
            return
        self._href = dict(attrs).get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, html.unescape("".join(self._text).strip())))
            self._href = None
            self._text = []


@dataclass(frozen=True, slots=True)
class CroatiaFile:
    attachment_id: str
    filename: str
    file_format: str
    download_url: str


@dataclass(frozen=True, slots=True)
class CroatiaNotice:
    published_raw: str
    published_at: date | None
    issuer_name: str
    category_id: str
    category: str
    reporting_year: int | None
    quarter: int | None
    metadata_html: str
    superseded: bool
    files: tuple[CroatiaFile, ...]


@dataclass(frozen=True, slots=True)
class CroatiaSourceDiagnostic:
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
class CroatiaSourceDiscovery:
    source: str
    query: str
    notices: tuple[CroatiaNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CroatiaIssuerResolution:
    found: bool
    matched_name: str | None = None
    source_record_id: str | None = None
    source_url: str | None = None
    detail_url: str | None = None
    home_member_state: str | None = "Croatia"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


def _attachment_id(url: str) -> str:
    stem = PurePosixPath(urlparse(url).path).stem
    match = re.search(r"-(\d+)_([^./]+)$", stem)
    return f"{match.group(1)}:{match.group(2)}" if match else stem


def parse_croatia_payload(
    payload: object,
    *,
    category_id: str,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[CroatiaNotice, ...]:
    if isinstance(payload, dict) and payload.get("data") is None:
        return ()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("HANFA SRPI response is missing the DataTables data array")
    if category_id not in CATEGORIES:
        raise ValueError(f"Unsupported HANFA periodic category: {category_id}")

    notices: list[CroatiaNotice] = []
    for row in payload["data"]:
        if not isinstance(row, list) or len(row) < 6:
            continue
        metadata_html = str(row[3] or "")
        parser = _AnchorParser()
        parser.feed(str(row[5] or ""))
        files: list[CroatiaFile] = []
        for href, label in parser.links:
            if not re.match(
                r"^/SRPI/(?:HR|EN)/20\d{2}/20\d{2}_\d{2}_\d{2}-",
                href,
                re.IGNORECASE,
            ):
                continue
            suffix = PurePosixPath(urlparse(href).path).suffix.casefold().lstrip(".")
            if suffix not in SUPPORTED_FORMATS:
                continue
            normalized_format = "xhtml" if suffix == "xht" else "zip" if suffix == "xbri" else suffix
            absolute_url = urljoin(base_url.rstrip("/") + "/", href)
            files.append(
                CroatiaFile(
                    attachment_id=_attachment_id(absolute_url),
                    filename=label or PurePosixPath(urlparse(href).path).name,
                    file_format=normalized_format,
                    download_url=absolute_url,
                )
            )
        notices.append(
            CroatiaNotice(
                published_raw=str(row[0] or "").strip(),
                published_at=_parse_published(row[0]),
                issuer_name=str(row[1] or "").strip(),
                category_id=category_id,
                category=str(row[2] or "").strip(),
                reporting_year=_extract_int("Year", metadata_html),
                quarter=_extract_int("Quarter", metadata_html),
                metadata_html=metadata_html,
                superseded=bool(
                    re.search(r"Correction\s*:\s*</span>\s*Last version", metadata_html, re.IGNORECASE)
                ),
                files=tuple(files),
            )
        )
    return tuple(notices)


def _explicit_period_date(text: str, published_at: date | None) -> date | None:
    patterns = (
        (r"\b(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})\b", (1, 2, 3)),
        (r"\b(\d{1,2})[.](\d{1,2})[.](20\d{2})\b", (3, 2, 1)),
    )
    for pattern, order in patterns:
        for match in re.finditer(pattern, text):
            parts = [int(match.group(index)) for index in order]
            try:
                parsed = date(parts[0], parts[1], parts[2])
            except ValueError:
                continue
            if published_at is None or (
                parsed <= published_at
                and (published_at - parsed) >= timedelta(days=14)
            ):
                return parsed
    return None


def extract_croatia_date_info(
    notice: CroatiaNotice,
    filename: str,
) -> tuple[date | None, int | None, str]:
    explicit = _explicit_period_date(filename, notice.published_at)
    if explicit:
        return explicit, explicit.year, "Explicit reporting-period date in attachment filename"
    year = notice.reporting_year
    if year is None:
        years = [
            int(value)
            for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", filename)
            if notice.published_at is None or int(value) <= notice.published_at.year
        ]
        year = years[-1] if years else None
    if year is None:
        return None, None, "No unambiguous reporting period in HANFA metadata"
    if notice.category_id == "17":
        return date(year, 12, 31), year, "Annual category and explicit HANFA reporting year"
    if notice.category_id == "18" and notice.quarter in {1, 2, 3, 4}:
        month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
        month, day = month_day[notice.quarter]
        return date(year, month, day), year, "Quarter number and explicit HANFA reporting year"
    return None, year, "Reporting year extracted; period end not explicit enough"


class CroatiaHanfaSrpiConnector(Connector):
    market = "Zagreb Stock Exchange"
    source_name = "croatia_hanfa_srpi"
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
        page_size: int = 100,
        max_pages: int = 10,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.search_url = f"{self.base_url}{SEARCH_PATH}"
        self.register_url = f"{self.base_url}{REGISTER_PATH}"
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.page_size = max(1, min(page_size, 100))
        self.max_pages = max(1, max_pages)
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        self._cache: dict[tuple[str, date, date], tuple[CroatiaNotice, ...]] = {}
        self._scanned_notices = 0
        self._cache_hits = 0

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _payload(
        self,
        *,
        category_id: str,
        start: int,
        from_date: date,
        to_date: date,
    ) -> dict[str, str]:
        payload = {
            "draw": str(start // self.page_size + 1),
            "order[0][column]": "0",
            "order[0][dir]": "desc",
            "start": str(start),
            "length": str(self.page_size),
            "search[value]": "",
            "search[regex]": "false",
            "Lang": "EN",
            "IssuerId": "",
            "KatId": category_id,
            "LangId": "0",
            "From": from_date.strftime("%d/%m/%Y"),
            "To": to_date.strftime("%d/%m/%Y"),
            "RecordsTotal": str(self.page_size),
        }
        for index in range(6):
            payload.update(
                {
                    f"columns[{index}][data]": str(index),
                    f"columns[{index}][name]": "",
                    f"columns[{index}][searchable]": "true",
                    f"columns[{index}][orderable]": "true",
                    f"columns[{index}][search][value]": "",
                    f"columns[{index}][search][regex]": "false",
                }
            )
        return payload

    def _fetch_category(
        self,
        category_id: str,
        *,
        from_date: date,
        to_date: date,
        limit: int | None = None,
    ) -> tuple[CroatiaNotice, ...]:
        cache_key = (category_id, from_date, to_date)
        if cache_key in self._cache:
            self._cache_hits += 1
            cached = self._cache[cache_key]
            return cached[:limit] if limit is not None else cached
        notices: list[CroatiaNotice] = []
        for page in range(self.max_pages):
            parsed: tuple[CroatiaNotice, ...] | None = None
            for request_number in range(2):
                self._wait()
                response: Any | None = None
                try:
                    response = self.session.post(
                        self.search_url,
                        data=self._payload(
                            category_id=category_id,
                            start=page * self.page_size,
                            from_date=from_date,
                            to_date=to_date,
                        ),
                        headers={
                            "Accept": "application/json, text/javascript, */*; q=0.01",
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": self.register_url,
                            "Origin": self.base_url,
                            "Connection": "close",
                        },
                        timeout=self.timeout,
                        verify=self.verify_ssl,
                    )
                    response.raise_for_status()
                    parsed = parse_croatia_payload(
                        response.json(),
                        category_id=category_id,
                        base_url=self.base_url,
                    )
                    self.attempts.append(
                        EndpointAttempt(
                            name=(
                                f"HANFA SRPI category {category_id} "
                                f"page {page + 1}"
                            ),
                            base_url=self.base_url,
                            dataset="SRPI",
                            endpoint=SEARCH_PATH,
                            method="POST",
                            http_status=response.status_code,
                            success=True,
                            total_count=len(parsed),
                        )
                    )
                    break
                except Exception as exc:
                    self.attempts.append(
                        EndpointAttempt(
                            name=(
                                f"HANFA SRPI category {category_id} "
                                f"page {page + 1} attempt {request_number + 1}"
                            ),
                            base_url=self.base_url,
                            dataset="SRPI",
                            endpoint=SEARCH_PATH,
                            method="POST",
                            http_status=getattr(response, "status_code", None),
                            success=False,
                            error=str(exc),
                        )
                    )
                    if request_number == 1:
                        self.state = ConnectorState.UNAVAILABLE
                        self.last_error = str(exc)
                        raise
                finally:
                    self._last_request_at = time.monotonic()
            if parsed is None:
                break
            notices.extend(parsed)
            if len(parsed) < self.page_size or (limit and len(notices) >= limit):
                break
        result = tuple(notices)
        self._cache[cache_key] = result
        self._scanned_notices += len(result)
        self.state = ConnectorState.READY
        self.last_error = None
        return result[:limit] if limit is not None else result

    def _candidate(
        self,
        notice: CroatiaNotice,
        file: CroatiaFile,
    ) -> DocumentCandidate:
        expected_category, document_type = CATEGORIES[notice.category_id]
        period_end, reporting_year, date_reason = extract_croatia_date_info(
            notice,
            file.filename,
        )
        isins = _extract_isins(file.filename, file.download_url)
        return DocumentCandidate(
            title=f"{notice.category} - {file.filename}",
            url=file.download_url,
            published_date=notice.published_at,
            document_type=document_type,
            source=self.source_name,
            source_document_id=file.attachment_id,
            metadata={
                "official_source": 1,
                "issuer_name": notice.issuer_name,
                "strict_issuer_name_match": True,
                "issuer_isins": list(isins),
                "issuer_country": "Croatia",
                "home_member_state": "Croatia",
                "pea_geography_status": "eu_candidate",
                "pea_country_check": "eu_candidate",
                "category_id": notice.category_id,
                "category": notice.category,
                "filename": file.filename,
                "file_id": file.attachment_id,
                "file_format": file.file_format,
                "parent_page_url": self.register_url,
                "hanfa_srpi_url": self.register_url,
            },
            classification=document_type,
            classification_reason=(
                f"HANFA SRPI exact periodic category {notice.category_id}: "
                f"{expected_category}"
            ),
            matched_positive_terms=[expected_category],
            matched_negative_terms=[],
            published_at=notice.published_at,
            period_end_date=period_end,
            reporting_year=reporting_year,
            source_publication_date_raw=notice.published_raw,
            source_period_date_raw=(
                period_end.isoformat() if period_end else str(reporting_year or "")
            ) or None,
            date_confidence="high" if notice.published_at else "low",
            date_extraction_reason=date_reason,
        )

    def _notice_candidates(self, notice: CroatiaNotice) -> list[DocumentCandidate]:
        if notice.superseded:
            return []
        return [self._candidate(notice, item) for item in notice.files]

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != self.market.casefold():
            return []
        end = date.today()
        configured_start = end - timedelta(days=self.lookback_days)
        start = min(since, configured_start) if since else configured_start
        candidates: list[DocumentCandidate] = []
        for category_id in CATEGORIES:
            remaining = None if limit is None else max(0, limit - len(candidates))
            if remaining == 0:
                break
            for notice in self._fetch_category(
                category_id,
                from_date=start,
                to_date=end,
                limit=remaining,
            ):
                candidates.extend(self._notice_candidates(notice))
        candidates.sort(
            key=lambda item: (item.published_at or date.min, item.url),
            reverse=True,
        )
        return candidates[:limit] if limit is not None else candidates

    def search_documents_for_issuer(self, issuer: Issuer) -> list[DocumentCandidate]:
        candidates = self.search_recent_documents(self.market)
        expected = _normalize(issuer.name)
        return [
            candidate
            for candidate in candidates
            if _normalize(candidate.metadata.get("issuer_name")) == expected
        ]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def discover(self, query: str, limit: int = 25) -> CroatiaSourceDiscovery:
        normalized = _normalize(query)
        if "semi annual" in normalized or "half year" in normalized:
            category_ids = ("24",)
        elif "quarter" in normalized:
            category_ids = ("18",)
        elif "annual" in normalized:
            category_ids = ("17",)
        else:
            category_ids = tuple(CATEGORIES)
        end = date.today()
        start = end - timedelta(days=365 * 3)
        notices: list[CroatiaNotice] = []
        candidates: list[DocumentCandidate] = []
        try:
            for category_id in category_ids:
                fetched = self._fetch_category(
                    category_id,
                    from_date=start,
                    to_date=end,
                    limit=limit,
                )
                notices.extend(fetched)
                for notice in fetched:
                    candidates.extend(self._notice_candidates(notice))
                    if len(candidates) >= limit:
                        break
                if len(candidates) >= limit:
                    break
            return CroatiaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=tuple(notices[:limit]),
                candidates=tuple(candidates[:limit]),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return CroatiaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def resolve_issuer(self, issuer: Issuer) -> CroatiaIssuerResolution:
        end = date.today()
        start = end - timedelta(days=365 * 3)
        expected = _normalize(issuer.name)
        try:
            best: tuple[float, CroatiaNotice] | None = None
            for category_id in CATEGORIES:
                for notice in self._fetch_category(
                    category_id,
                    from_date=start,
                    to_date=end,
                    limit=100,
                ):
                    observed = _normalize(notice.issuer_name)
                    score = 100.0 if expected == observed else 85.0 if expected in observed or observed in expected else 0.0
                    if score and (best is None or score > best[0]):
                        best = (score, notice)
            if best is None:
                return CroatiaIssuerResolution(
                    found=False,
                    attempts=tuple(self.attempts),
                    error="No matching issuer in HANFA SRPI periodic filings",
                )
            score, notice = best
            record_id = notice.files[0].attachment_id if notice.files else None
            return CroatiaIssuerResolution(
                found=True,
                matched_name=notice.issuer_name,
                source_record_id=record_id,
                source_url=self.register_url,
                detail_url=self.register_url,
                match_score=score,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return CroatiaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> CroatiaSourceDiagnostic:
        end = date.today()
        start = end - timedelta(days=120)
        try:
            notices: list[CroatiaNotice] = []
            for category_id in CATEGORIES:
                notices.extend(
                    self._fetch_category(
                        category_id,
                        from_date=start,
                        to_date=end,
                        limit=100,
                    )
                )
            categories: dict[str, int] = {}
            formats: set[str] = set()
            attachment_count = 0
            example: dict[str, Any] | None = None
            for notice in notices:
                categories[notice.category] = categories.get(notice.category, 0) + 1
                attachment_count += len(notice.files)
                formats.update(item.file_format for item in notice.files)
                if example is None and notice.files and not notice.superseded:
                    example = {
                        "issuer": notice.issuer_name,
                        "category_id": notice.category_id,
                        "category": notice.category,
                        "published_at": notice.published_raw,
                        "reporting_year": notice.reporting_year,
                        "attachment_count": len(notice.files),
                        "filename": notice.files[0].filename,
                        "format": notice.files[0].file_format,
                        "download_url": notice.files[0].download_url,
                    }
            status = next(
                (
                    attempt.http_status
                    for attempt in reversed(self.attempts)
                    if attempt.success
                ),
                None,
            )
            return CroatiaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.READY if example else ConnectorState.DEGRADED,
                called_url=self.search_url,
                http_status=status,
                method_used="POST DataTables API filtered by exact periodic category",
                total_count=len(notices),
                detected_count=len(notices),
                attachment_count=attachment_count,
                fields=(
                    "published_at",
                    "issuer_name",
                    "category",
                    "reporting_year",
                    "source_document_id",
                    "attachment_url",
                ),
                categories=categories,
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=len(self.attempts),
                request_efficiency=(
                    "Three bounded global category queries; no issuer loop; "
                    f"cache hits: {self._cache_hits}"
                ),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return CroatiaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.search_url,
                http_status=None,
                method_used="POST HANFA SRPI DataTables API",
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
        return 3

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 3
