from __future__ import annotations

import html
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from connectors.base import (
    Connector,
    ConnectorError,
    ConnectorState,
    DocumentCandidate,
    EndpointAttempt,
)
from models import Issuer


DEFAULT_BASE_URL = "https://moam.knf.gov.pl/moam.nsf"

PERIODIC_TYPE_MAP = {
    "RR": "annual_financial_report",
    "SRR": "annual_financial_report",
    "PSR": "half_year_financial_report",
    "QSR": "quarterly_financial_report",
}

PERIODIC_LABELS = {
    "RR": "standalone annual financial report",
    "SRR": "consolidated annual financial report",
    "PSR": "consolidated half-year financial report",
    "QSR": "consolidated quarterly financial report",
}

TITLE_FALLBACK_CODES = frozenset({"RB-W", "RB-W_ASO", "UNI-EN", "UNI-PL"})
TITLE_NEGATIVE_TERMS = (
    "bond",
    "bonds",
    "isin",
    "revenue",
    "revenues",
    "sales",
    "sprzedaz",
    "przychod",
    "dywidend",
    "dividend",
    "announces date",
    "publication date",
    "date for",
    "operations update",
    "zmiana terminu",
    "terminu przekazania",
    "preliminary",
    "wstepna",
    "szacunk",
)

ANNUAL_TITLE_TERMS = (
    "annual financial report",
    "annual report",
    "audited annual financial report",
    "consolidated annual financial report",
    "raport roczny",
    "roczne sprawozdanie finansowe",
)

HALF_YEAR_TITLE_TERMS = (
    "half year financial report",
    "half year report",
    "half yearly financial report",
    "h1 report",
    "h1 financial report",
    "polroczne sprawozdanie finansowe",
    "polroczny raport",
    "i polrocze",
)

QUARTERLY_TITLE_TERMS = (
    "quarterly financial report",
    "quarterly report",
    "consolidated report q1",
    "consolidated report q2",
    "consolidated report q3",
    "consolidated report q4",
    "report q1",
    "report q2",
    "report q3",
    "report q4",
    "interim financial statements",
    "raport kwartal",
    "raportu kwartal",
)


def _normalize(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()


def _parse_listing_date(value: object) -> date | None:
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_oam_timestamp(value: object) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for pattern in (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def classify_poland_document(
    report_code: str,
) -> tuple[str, str, list[str], list[str]]:
    code = (report_code or "").strip()
    normalized_code = code.upper()
    document_type = PERIODIC_TYPE_MAP.get(normalized_code)
    if document_type:
        return (
            document_type,
            f"KNF OAM exact periodic form code: {code}",
            [code],
            [],
        )
    if normalized_code.startswith("RB"):
        return (
            "other_regulatory_announcement",
            f"KNF OAM current-report form rejected: {code}",
            [],
            [code],
        )
    return (
        "other_regulatory_announcement",
        (
            "KNF OAM form is not in the proven periodic allowlist: "
            f"{code or 'missing'}"
        ),
        [],
        [code] if code else [],
    )


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    return sorted(
        term
        for term in terms
        if _normalize(term) and _normalize(term) in text
    )


def classify_poland_notice(
    report_code: str,
    title: str = "",
    filename: str = "",
) -> tuple[str, str, list[str], list[str]]:
    document_type, reason, positive, negative = classify_poland_document(
        report_code
    )
    if document_type != "other_regulatory_announcement":
        return document_type, reason, positive, negative

    code = (report_code or "").strip().upper()
    if code not in TITLE_FALLBACK_CODES:
        return document_type, reason, positive, negative

    title_text = _normalize(title)
    text = _normalize(f"{title} {filename}")
    matched_negative = _matched_terms(text, TITLE_NEGATIVE_TERMS)
    if matched_negative:
        return (
            "other_regulatory_announcement",
            f"KNF OAM title fallback exclusion: {matched_negative[0]}",
            [],
            [code, matched_negative[0]] if code else [matched_negative[0]],
        )

    for inferred_type, terms in (
        ("annual_financial_report", ANNUAL_TITLE_TERMS),
        ("half_year_financial_report", HALF_YEAR_TITLE_TERMS),
    ):
        matched = _matched_terms(text, terms)
        if matched:
            return (
                inferred_type,
                f"KNF OAM title fallback periodic report term: {matched[0]}",
                [code, matched[0]] if code else [matched[0]],
                [],
            )

    if re.search(r"\bh1\b", title_text) and any(
        term in title_text
        for term in ("financial report", "financial statements", "report")
    ):
        return (
            "half_year_financial_report",
            "KNF OAM title fallback H1 report marker",
            [code, "h1"] if code else ["h1"],
            [],
        )

    matched_quarterly = _matched_terms(text, QUARTERLY_TITLE_TERMS)
    if matched_quarterly:
        return (
            "quarterly_financial_report",
            (
                "KNF OAM title fallback periodic report term: "
                f"{matched_quarterly[0]}"
            ),
            [code, matched_quarterly[0]] if code else [matched_quarterly[0]],
            [],
        )

    has_quarter = re.search(r"\bq[1-4]\b|\b[1-4]q\b|\b9m\b", text)
    has_report_context = any(
        term in text
        for term in (
            "financial report",
            "financial statements",
            "consolidated report",
            "management report",
            "raport kwartal",
            "sprawozdanie finansowe",
        )
    )
    if has_quarter and has_report_context:
        return (
            "quarterly_financial_report",
            "KNF OAM title fallback quarterly report marker",
            [code, "quarterly_title_marker"] if code else ["quarterly_title_marker"],
            [],
        )

    return document_type, reason, positive, negative


@dataclass(frozen=True, slots=True)
class PolandNotice:
    record_id: str
    issuer_name: str
    published_date: date | None
    report_code: str
    title: str
    detail_url: str
    appform_url: str
    package_url: str
    package_year: str
    package_date: str
    filename: str
    listing_url: str


@dataclass(frozen=True, slots=True)
class PolandListingPage:
    notices: tuple[PolandNotice, ...]
    total_count: int
    pagination_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolandDetail:
    record_id: str
    issuer_name: str
    report_code: str
    title: str
    published_raw: str | None
    published_at: date | None
    reporting_year: int | None
    report_number: str | None
    filename: str | None
    effective_date_raw: str | None
    detail_url: str


@dataclass(frozen=True, slots=True)
class PolandSourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    http_status: int | None
    method_used: str
    total_count: int
    detected_count: int
    fields: tuple[str, ...]
    categories: dict[str, int]
    formats: tuple[str, ...]
    example_notice: dict[str, Any] | None
    http_calls: int
    request_efficiency: str
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PolandSourceDiscovery:
    source: str
    query: str
    notices: tuple[PolandNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PolandIssuerResolution:
    found: bool
    matched_name: str | None = None
    knf_oam_name: str | None = None
    knf_oam_issuer_url: str | None = None
    knf_oam_detail_url: str | None = None
    knf_oam_record_id: str | None = None
    home_member_state: str | None = "Poland"
    pea_country_check: str | None = "eu_candidate"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


def _canonical_package_url(
    base_url: str,
    *,
    year: str,
    category_date: str,
    filename: str,
) -> str:
    split = urlsplit(base_url)
    origin = f"{split.scheme}://{split.netloc}"
    return (
        f"{origin}/mOAM/{quote(year, safe='')}/"
        f"{quote(category_date, safe='')}/{quote(filename, safe='._-')}"
    )


def parse_poland_listing(
    html_text: str,
    *,
    page_url: str,
    base_url: str = DEFAULT_BASE_URL,
) -> PolandListingPage:
    soup = BeautifulSoup(html_text, "html.parser")
    notices: list[PolandNotice] = []
    table = soup.select_one("table.edittable")
    if table is not None:
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            title_link = cells[2].find("a", href=True)
            package_link = cells[3].find("a", href=True)
            if title_link is None or package_link is None:
                continue
            title_text = cells[2].get_text(" ", strip=True)
            report_code, separator, report_title = title_text.partition(",")
            if not separator:
                report_title = ""
            detail_url = urljoin(page_url, str(title_link["href"]))
            record_match = re.search(r"/([0-9A-F]{32})(?:$|[?#])", detail_url, re.I)
            if record_match is None:
                continue
            appform_url = urljoin(
                page_url,
                html.unescape(str(package_link["href"])),
            )
            params = parse_qs(
                urlsplit(appform_url).query,
                keep_blank_values=True,
            )
            year = (params.get("rok") or [""])[0].strip()
            category_date = (params.get("kat") or [""])[0].strip()
            filename = (params.get("plik") or [""])[0].strip()
            if not year or not category_date or not filename:
                continue
            notices.append(
                PolandNotice(
                    record_id=record_match.group(1).upper(),
                    issuer_name=cells[0].get_text(" ", strip=True),
                    published_date=_parse_listing_date(
                        cells[1].get_text(" ", strip=True)
                    ),
                    report_code=report_code.strip(),
                    title=report_title.strip(),
                    detail_url=detail_url,
                    appform_url=appform_url,
                    package_url=_canonical_package_url(
                        base_url,
                        year=year,
                        category_date=category_date,
                        filename=filename,
                    ),
                    package_year=year,
                    package_date=category_date,
                    filename=filename,
                    listing_url=page_url,
                )
            )

    message = soup.select_one("#message")
    total_count = len(notices)
    if message is not None:
        count_match = re.search(
            r"(\d[\d\s]*)\s+dokument",
            message.get_text(" ", strip=True),
            re.I,
        )
        if count_match:
            total_count = int(count_match.group(1).replace(" ", ""))

    pagination_urls: list[str] = []
    for link in soup.select("#pages a[href]"):
        target = urljoin(page_url, str(link["href"]))
        if target not in pagination_urls:
            pagination_urls.append(target)
    return PolandListingPage(
        notices=tuple(notices),
        total_count=total_count,
        pagination_urls=tuple(pagination_urls),
    )


def _detail_field(soup: BeautifulSoup, label: str) -> str | None:
    node = soup.find(string=lambda value: value and value.strip() == label)
    if node is None:
        return None
    cell = node.find_parent("td")
    if cell is None:
        return None
    sibling = cell
    for _ in range(2):
        sibling = sibling.find_next_sibling("td")
        if sibling is None:
            return None
    value = sibling.get_text(" ", strip=True)
    return value or None


def parse_poland_detail(
    html_text: str,
    *,
    record_id: str,
    detail_url: str,
) -> PolandDetail:
    soup = BeautifulSoup(html_text, "html.parser")
    published_raw = _detail_field(soup, "Data odebrania:")
    year_raw = _detail_field(soup, "Rok:")
    try:
        reporting_year = int(year_raw) if year_raw else None
    except ValueError:
        reporting_year = None
    return PolandDetail(
        record_id=record_id,
        issuer_name=_detail_field(soup, "Emitent:") or "",
        report_code=_detail_field(soup, "Nazwa raportu:") or "",
        title=_detail_field(soup, "Tytu\u0142 raportu:") or "",
        published_raw=published_raw,
        published_at=_parse_oam_timestamp(published_raw),
        reporting_year=reporting_year,
        report_number=_detail_field(soup, "Numer w roku:"),
        filename=_detail_field(soup, "Plik raportu:"),
        effective_date_raw=_detail_field(
            soup,
            "Data obowi\u0105zywania:",
        ),
        detail_url=detail_url,
    )


class PolandKnfOamConnector(Connector):
    market = "Warsaw Stock Exchange"
    source_name = "knf_oam"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str = DEFAULT_BASE_URL,
        rate_limit_seconds: float = 0.2,
        lookback_days: int = 30,
        timeout: int = 45,
        verify_ssl: bool = True,
        max_pages_per_date: int = 25,
        cache_path: str | Path | None = None,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.max_pages_per_date = max(1, max_pages_per_date)
        self.cache_path = Path(cache_path) if cache_path else None
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        self._html_cache: dict[str, str] = {}
        self._detail_cache: dict[str, PolandDetail] = {}
        self._scanned_notices = 0
        self._details_visited = 0
        self._date_cache: dict[str, list[PolandNotice]] = {}
        self._cache_hits = 0
        self._load_date_cache()

    def _load_date_cache(self) -> None:
        if self.cache_path is None or not self.cache_path.is_file():
            return
        try:
            payload = json.loads(
                self.cache_path.read_text(encoding="utf-8")
            )
            raw_dates = payload.get("dates", {})
            if not isinstance(raw_dates, dict):
                return
            for key, raw_notices in raw_dates.items():
                parsed: list[PolandNotice] = []
                if not isinstance(raw_notices, list):
                    continue
                for item in raw_notices:
                    if not isinstance(item, dict):
                        continue
                    parsed.append(
                        PolandNotice(
                            record_id=str(item["record_id"]),
                            issuer_name=str(item["issuer_name"]),
                            published_date=_parse_listing_date(
                                item.get("published_date")
                            ),
                            report_code=str(item["report_code"]),
                            title=str(item.get("title") or ""),
                            detail_url=str(item["detail_url"]),
                            appform_url=str(item["appform_url"]),
                            package_url=str(item["package_url"]),
                            package_year=str(item["package_year"]),
                            package_date=str(item["package_date"]),
                            filename=str(item["filename"]),
                            listing_url=str(item["listing_url"]),
                        )
                    )
                self._date_cache[str(key)] = parsed
        except (OSError, ValueError, KeyError, TypeError):
            self._date_cache = {}

    def _save_date_cache(self) -> None:
        if self.cache_path is None:
            return
        payload = {
            "source": self.source_name,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "dates": {
                key: [
                    {
                        "record_id": notice.record_id,
                        "issuer_name": notice.issuer_name,
                        "published_date": (
                            notice.published_date.isoformat()
                            if notice.published_date
                            else None
                        ),
                        "report_code": notice.report_code,
                        "title": notice.title,
                        "detail_url": notice.detail_url,
                        "appform_url": notice.appform_url,
                        "package_url": notice.package_url,
                        "package_year": notice.package_year,
                        "package_date": notice.package_date,
                        "filename": notice.filename,
                        "listing_url": notice.listing_url,
                    }
                    for notice in notices
                ]
                for key, notices in sorted(self._date_cache.items())
            },
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(
            self.cache_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.cache_path)

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _fetch_html(self, url: str, *, name: str) -> str:
        cached = self._html_cache.get(url)
        if cached is not None:
            return cached
        self._wait()
        response: Any | None = None
        try:
            response = self.session.get(
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            payload = response.text
            self._html_cache[url] = payload
            self.attempts.append(
                EndpointAttempt(
                    name=name,
                    base_url=self.base_url,
                    dataset="KNF OAM",
                    endpoint=url,
                    method="GET",
                    http_status=response.status_code,
                    success=True,
                )
            )
            self.state = ConnectorState.READY
            self.last_error = None
            return payload
        except Exception as exc:
            self.state = ConnectorState.UNAVAILABLE
            self.last_error = str(exc)
            self.attempts.append(
                EndpointAttempt(
                    name=name,
                    base_url=self.base_url,
                    dataset="KNF OAM",
                    endpoint=url,
                    method="GET",
                    http_status=(
                        getattr(response, "status_code", None)
                        if response is not None
                        else None
                    ),
                    success=False,
                    error=str(exc),
                )
            )
            raise
        finally:
            self._last_request_at = time.monotonic()

    def _search_url(
        self,
        *,
        field_name: str,
        value: str,
        resort_descending: bool = False,
    ) -> str:
        params: list[tuple[str, str]] = [
            ("OpenNavigator", ""),
            ("Field", field_name),
            ("Value", value),
        ]
        if resort_descending:
            params.append(("ResortDescending", "1"))
        return f"{self.base_url}/search?{urlencode(params)}"

    def _listing_page(self, url: str, *, name: str) -> PolandListingPage:
        return parse_poland_listing(
            self._fetch_html(url, name=name),
            page_url=url,
            base_url=self.base_url,
        )

    def _all_pages(
        self,
        first_url: str,
        *,
        name: str,
        max_pages: int,
        stop_before: date | None = None,
        require_complete: bool = False,
    ) -> list[PolandNotice]:
        first = self._listing_page(first_url, name=name)
        notices = list(first.notices)
        page_urls = list(first.pagination_urls)
        expected_pages = max(1, math.ceil(first.total_count / 20))
        if require_complete and expected_pages > max_pages:
            raise ConnectorError(
                f"KNF OAM pagination requires {expected_pages} pages for "
                f"{first_url}, above the configured limit of {max_pages}"
            )
        for page_url in page_urls[: max_pages - 1]:
            if stop_before and notices:
                dated = [
                    notice.published_date
                    for notice in notices[-20:]
                    if notice.published_date
                ]
                if dated and max(dated) < stop_before:
                    break
            page = self._listing_page(page_url, name=name)
            notices.extend(page.notices)
        return notices

    def _date_notices(self, target_date: date) -> list[PolandNotice]:
        cache_key = target_date.isoformat()
        stable_before = date.today() - timedelta(days=1)
        if target_date < stable_before and cache_key in self._date_cache:
            self._cache_hits += 1
            return list(self._date_cache[cache_key])
        url = self._search_url(
            field_name="DataOdebrania",
            value=target_date.isoformat(),
        )
        notices = self._all_pages(
            url,
            name=f"KNF OAM global date {target_date.isoformat()}",
            max_pages=self.max_pages_per_date,
            require_complete=True,
        )
        self._date_cache[cache_key] = list(notices)
        self._save_date_cache()
        return notices

    def _field_notices(
        self,
        *,
        field_name: str,
        value: str,
        max_pages: int,
        stop_before: date | None = None,
        resort_descending: bool = False,
    ) -> list[PolandNotice]:
        url = self._search_url(
            field_name=field_name,
            value=value,
            resort_descending=resort_descending,
        )
        return self._all_pages(
            url,
            name=f"KNF OAM search {field_name}={value}",
            max_pages=max_pages,
            stop_before=stop_before,
        )

    def _candidate(self, notice: PolandNotice) -> DocumentCandidate:
        document_type, reason, positive, negative = (
            classify_poland_notice(
                notice.report_code,
                notice.title,
                notice.filename,
            )
        )
        code_key = notice.report_code.upper()
        source_title = notice.title.strip(" -")
        title = source_title or (
            f"{notice.issuer_name} "
            f"{PERIODIC_LABELS.get(code_key, 'regulatory report')} "
            f"({notice.report_code})"
        )
        file_format = (
            PurePosixPath(notice.filename).suffix.casefold().lstrip(".")
            or None
        )
        return DocumentCandidate(
            title=title,
            url=notice.package_url,
            published_date=notice.published_date,
            document_type=document_type,
            source=self.source_name,
            source_document_id=(
                f"{notice.record_id}:{notice.filename}"
            ),
            metadata={
                "official_source": 1,
                "issuer_name": notice.issuer_name,
                "strict_issuer_name_match": True,
                "issuer_country": "Poland",
                "home_member_state": "Poland",
                "pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
                "record_id": notice.record_id,
                "report_code": notice.report_code,
                "report_number": None,
                "detail_url": notice.detail_url,
                "knf_oam_issuer_url": notice.listing_url,
                "knf_oam_detail_url": notice.detail_url,
                "knf_oam_record_id": notice.record_id,
                "filename": notice.filename,
                "file_id": notice.record_id,
                "file_format": file_format,
                "appform_url": notice.appform_url,
                "package_year": notice.package_year,
                "package_date": notice.package_date,
                "parent_page_url": notice.detail_url,
            },
            classification=document_type,
            classification_reason=reason,
            matched_positive_terms=positive,
            matched_negative_terms=negative,
            published_at=notice.published_date,
            source_publication_date_raw=(
                notice.published_date.isoformat()
                if notice.published_date
                else None
            ),
            date_confidence=(
                "high" if notice.published_date is not None else "low"
            ),
            date_extraction_reason=(
                "KNF OAM listing receipt date; detail timestamp is fetched "
                "only after local issuer matching"
            ),
        )

    def _detail(self, candidate: DocumentCandidate) -> PolandDetail:
        record_id = str(candidate.metadata.get("record_id") or "")
        detail_url = str(candidate.metadata.get("detail_url") or "")
        cached = self._detail_cache.get(record_id)
        if cached is not None:
            return cached
        payload = self._fetch_html(
            detail_url,
            name=f"KNF OAM detail {record_id}",
        )
        detail = parse_poland_detail(
            payload,
            record_id=record_id,
            detail_url=detail_url,
        )
        self._detail_cache[record_id] = detail
        self._details_visited += 1
        return detail

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != self.market.casefold():
            return []
        cutoff = since or (date.today() - timedelta(days=self.lookback_days))
        current = date.today()
        periodic: list[DocumentCandidate] = []
        rejected: list[DocumentCandidate] = []
        self._scanned_notices = 0
        while current >= cutoff:
            notices = self._date_notices(current)
            self._scanned_notices += len(notices)
            for notice in notices:
                candidate = self._candidate(notice)
                if candidate.document_type in PERIODIC_TYPE_MAP.values():
                    periodic.append(candidate)
                else:
                    rejected.append(candidate)
            current -= timedelta(days=1)
        candidates = periodic + rejected
        return candidates[:limit] if limit is not None else candidates

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        if candidate.document_type == "other_regulatory_announcement":
            return [candidate]
        detail = self._detail(candidate)
        document_type, reason, positive, negative = (
            classify_poland_notice(
                detail.report_code,
                detail.title or candidate.title,
                detail.filename or str(candidate.metadata.get("filename") or ""),
            )
        )
        metadata = dict(candidate.metadata)
        metadata.update(
            {
                "issuer_name": detail.issuer_name
                or candidate.metadata.get("issuer_name"),
                "report_code": detail.report_code,
                "report_number": detail.report_number,
                "effective_date_raw": detail.effective_date_raw,
                "filename": detail.filename
                or candidate.metadata.get("filename"),
            }
        )
        title = detail.title.strip(" -") or candidate.title
        published_at = detail.published_at or candidate.published_at
        return [
            replace(
                candidate,
                title=title,
                document_type=document_type,
                source_document_id=(
                    f"{detail.record_id}:"
                    f"{detail.filename or metadata.get('filename')}"
                ),
                metadata=metadata,
                classification=document_type,
                classification_reason=reason,
                matched_positive_terms=positive,
                matched_negative_terms=negative,
                published_date=published_at,
                published_at=published_at,
                reporting_year=detail.reporting_year,
                source_publication_date_raw=detail.published_raw,
                date_confidence="high" if published_at else "low",
                date_extraction_reason=(
                    "KNF OAM Data odebrania parsed as MM/DD/YYYY; "
                    "reporting year kept separately from publication date"
                ),
            )
        ]

    def search_documents_for_issuer(
        self,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        notices = self._field_notices(
            field_name="NazwaPodmiot",
            value=issuer.name,
            max_pages=self.max_pages_per_date,
        )
        expected = _normalize(issuer.name)
        return [
            self._candidate(notice)
            for notice in notices
            if _normalize(notice.issuer_name) == expected
        ]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def resolve_issuer(self, issuer: Issuer) -> PolandIssuerResolution:
        try:
            notices = self._field_notices(
                field_name="NazwaPodmiot",
                value=issuer.name,
                max_pages=min(3, self.max_pages_per_date),
            )
        except Exception as exc:
            return PolandIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )
        expected = _normalize(issuer.name)
        best: tuple[float, PolandNotice] | None = None
        for notice in notices:
            observed = _normalize(notice.issuer_name)
            score = 0.0
            if expected == observed:
                score = 90.0
            elif expected in observed or observed in expected:
                score = 80.0
            if score and (best is None or score > best[0]):
                best = (score, notice)
        if best is None:
            return PolandIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error="No matching issuer found in the KNF OAM public search",
            )
        score, notice = best
        return PolandIssuerResolution(
            found=True,
            matched_name=notice.issuer_name,
            knf_oam_name=notice.issuer_name,
            knf_oam_issuer_url=notice.listing_url,
            knf_oam_detail_url=notice.detail_url,
            knf_oam_record_id=notice.record_id,
            match_score=score,
            attempts=tuple(self.attempts),
        )

    def discover(
        self,
        query: str,
        limit: int = 25,
    ) -> PolandSourceDiscovery:
        normalized = _normalize(query)
        if "annual" in normalized or "rocz" in normalized:
            codes = ("SRR", "RR")
        elif "quarter" in normalized or "kwart" in normalized:
            codes = ("QSr",)
        elif (
            "half" in normalized
            or "semi annual" in normalized
            or "polrocz" in normalized
        ):
            codes = ("PSr",)
        else:
            codes = ()
        try:
            notices: list[PolandNotice] = []
            if codes:
                for code in codes:
                    remaining_pages = max(
                        1,
                        math.ceil(max(1, limit - len(notices)) / 20),
                    )
                    notices.extend(
                        self._field_notices(
                            field_name="NazwaRaportu",
                            value=code,
                            max_pages=remaining_pages,
                            resort_descending=True,
                        )
                    )
                    if len(notices) >= limit:
                        break
            else:
                notices.extend(
                    self._field_notices(
                        field_name="all",
                        value=query,
                        max_pages=max(1, math.ceil(limit / 20)),
                        resort_descending=True,
                    )
                )
            candidates = [
                self._candidate(notice)
                for notice in notices
                if notice.report_code.upper() in PERIODIC_TYPE_MAP
            ][:limit]
            return PolandSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=tuple(notices[:limit]),
                candidates=tuple(candidates),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return PolandSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> PolandSourceDiagnostic:
        try:
            current = self._listing_page(
                self.base_url,
                name="KNF OAM current global listing",
            )
            notices = list(current.notices)
            periodic = next(
                (
                    notice
                    for notice in notices
                    if notice.report_code.upper() in PERIODIC_TYPE_MAP
                ),
                None,
            )
            if periodic is None:
                sample = self._field_notices(
                    field_name="NazwaRaportu",
                    value="QSr",
                    max_pages=1,
                    resort_descending=True,
                )
                periodic = next(
                    (
                        notice
                        for notice in sample
                        if notice.report_code.upper() == "QSR"
                    ),
                    None,
                )
                notices.extend(sample[:1])
            categories: dict[str, int] = {}
            formats: set[str] = set()
            for notice in notices:
                categories[notice.report_code] = (
                    categories.get(notice.report_code, 0) + 1
                )
                suffix = (
                    PurePosixPath(notice.filename)
                    .suffix.casefold()
                    .lstrip(".")
                )
                if suffix:
                    formats.add(suffix)
            example = None
            if periodic is not None:
                example = {
                    "record_id": periodic.record_id,
                    "issuer": periodic.issuer_name,
                    "report_code": periodic.report_code,
                    "title": periodic.title,
                    "published_at": (
                        periodic.published_date.isoformat()
                        if periodic.published_date
                        else None
                    ),
                    "detail_url": periodic.detail_url,
                    "filename": periodic.filename,
                    "format": (
                        PurePosixPath(periodic.filename)
                        .suffix.casefold()
                        .lstrip(".")
                    ),
                    "download_url": periodic.package_url,
                }
            status = next(
                (
                    attempt.http_status
                    for attempt in reversed(self.attempts)
                    if attempt.success
                ),
                None,
            )
            return PolandSourceDiagnostic(
                source=self.source_name,
                state=(
                    ConnectorState.READY
                    if current.total_count >= 0 and periodic is not None
                    else ConnectorState.DEGRADED
                ),
                called_url=self.base_url,
                http_status=status,
                method_used=(
                    "server-rendered global HTML listing; exact form-code "
                    "classification; no JavaScript or session"
                ),
                total_count=current.total_count,
                detected_count=len(current.notices),
                fields=(
                    "issuer_name",
                    "published_at",
                    "report_code",
                    "title",
                    "source_document_id",
                    "detail_url",
                    "attachment_url",
                ),
                categories=categories,
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=len(self.attempts),
                request_efficiency=(
                    "One current global page plus one periodic sample page "
                    "only when the current page has no periodic form; "
                    f"persistent historical-date cache hits: {self._cache_hits}"
                ),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return PolandSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.base_url,
                http_status=None,
                method_used="server-rendered KNF OAM HTML",
                total_count=0,
                detected_count=0,
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
        cutoff = since or (date.today() - timedelta(days=self.lookback_days))
        current = date.today()
        uncached_days = 0
        stable_before = current - timedelta(days=1)
        while current >= cutoff:
            if (
                current >= stable_before
                or current.isoformat() not in self._date_cache
            ):
                uncached_days += 1
            current -= timedelta(days=1)
        return min(
            max(1, uncached_days) * 4,
            max(1, uncached_days) * self.max_pages_per_date,
        )

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return min(3, self.max_pages_per_date)
