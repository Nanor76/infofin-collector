from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from connectors.base import Connector, ConnectorState, DocumentCandidate, EndpointAttempt
from models import Issuer


DEFAULT_BASE_URL = "https://ceri.nbs.sk"
SEARCH_PATH = "/search"
SEARCH_SUBMIT = "Hľadaj"
GLOBAL_CATEGORY = "1000"
DISCOVER_FALLBACK_ISSUER_QUERIES = ("Tatry", "Slovnaft", "eustream")
SUPPORTED_FORMATS = {"pdf", "zip", "xhtml", "xht", "xml", "xbrl", "xbri"}

PERIODIC_CATEGORY_CODES = frozenset({"1", "2", "3", "33", "36", "38"})
REJECTED_INZERAT_CODES = frozenset({"35", "37"})

PERIODIC_TYPES: dict[str, str] = {
    "1": "Ročná finančná správa - zverejnenie",
    "2": "Polročná finančná správa - zverejnenie",
    "3": "Predbežné vyhlásenie alebo Štvrťročná finančná správa",
    "33": "Audítorská správa s elektronickým podpisom",
    "36": "Ročná finančná správa - doplnenie/oprava",
    "38": "Polročná finančná správa - doplnenie/oprava",
}
REJECTED_INZERAT_TYPES: dict[str, str] = {
    "35": "Ročná finančná správa - inzerát",
    "37": "Polročná finančná správa - inzerát",
}
_CATEGORY_CODE_BY_LABEL = {
    _normalize_label: code
    for code, label in (
        *PERIODIC_TYPES.items(),
        *REJECTED_INZERAT_TYPES.items(),
    )
    if (_normalize_label := re.sub(r"\s+", " ", label.casefold().strip()))
}

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
    "dlhopis",
    "dlhopisy",
    "obligacia",
    "obligacie",
    "obligacii",
    "vlastne akcie",
    "valne zhromazdenie",
    "tlacova sprava",
    "ponuka na prevzatie",
    "dividend",
    "dividenda",
    "dividendy",
    "fond",
    "transakcie manazerov",
    "oznamenie o konani vz",
    "konani valneho zhromazdenia",
    "hlasovacich pravach",
    "podieloch na hlasovacich pravach",
    "inzerat",
    "v likvidacii",
    "v konkurze",
    "financing",
    "funding",
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
        r"\b(?:as|a s|spol|s r o|akciova spolocnost)\b",
        " ",
        normalized,
    ).strip()


def _parse_date(value: object) -> date | None:
    raw = str(value or "").strip()
    for pattern in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def _format_ceri_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _category_code(category: str) -> str:
    normalized = re.sub(r"\s+", " ", category.casefold().strip())
    if normalized in _CATEGORY_CODE_BY_LABEL:
        return _CATEGORY_CODE_BY_LABEL[normalized]
    for code, label in {**PERIODIC_TYPES, **REJECTED_INZERAT_TYPES}.items():
        if _normalize(label) == _normalize(category):
            return code
    return ""


def _file_format(filename: str, url: str = "") -> str | None:
    for value in (filename, url):
        suffix = PurePosixPath(value).suffix.casefold().lstrip(".")
        if suffix in SUPPORTED_FORMATS:
            return "xhtml" if suffix == "xht" else suffix
    normalized = _normalize(f"{filename} {url}")
    for marker in ("xbri", "xbrl", "xhtml", "esef", "zip", "pdf"):
        if marker in normalized:
            return "zip" if marker == "esef" else marker
    return None


def build_ceri_download_url(
    span_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[str, str]:
    filename = span_id[3:] if span_id.startswith(("img", "tit")) else span_id
    if not filename:
        raise ValueError(f"Invalid CERI ceridoc id: {span_id!r}")
    prefix = filename[:5]
    path = f"/static/data/{prefix}/{filename}"
    return filename, urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def build_ceri_search_payload(
    *,
    from_date: date,
    to_date: date,
    category: str = GLOBAL_CATEGORY,
    query: str = "",
) -> dict[str, str]:
    payload = {
        "qissuer": "0",
        "qcathegory": category,
        "qico": "",
        "qisin": "",
        "qrdfrom": _format_ceri_date(from_date),
        "qrdto": _format_ceri_date(to_date),
        "search_set": SEARCH_SUBMIT,
    }
    if query:
        payload["query"] = query
    return payload


def classify_slovakia_document(
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

    category_code = _category_code(category)
    if category_code in REJECTED_INZERAT_CODES:
        return (
            "other_regulatory_announcement",
            f"Rejected CERI inzerát category {category_code}",
            [],
            ["inzerat"],
        )
    if category_code == "1" or category_code == "36":
        return (
            "annual_financial_report",
            f"CERI Slovakia periodic annual category {category_code or category}",
            [category_code or category],
            [],
        )
    if category_code == "2" or category_code == "38":
        return (
            "half_year_financial_report",
            f"CERI Slovakia periodic half-year category {category_code or category}",
            [category_code or category],
            [],
        )
    if category_code == "3":
        return (
            "quarterly_financial_report",
            f"CERI Slovakia periodic quarterly category {category_code or category}",
            [category_code or category],
            [],
        )
    if category_code == "33":
        return (
            "annual_financial_report",
            "CERI Slovakia signed audit report linked to annual filing",
            ["33"],
            [],
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
        "stvrtrocna financna sprava",
        "stvrtrocna sprava",
        "predbezne vyhlasenie",
    )
    half_year = (
        "half year",
        "half-year",
        "semi annual",
        "semi-annual",
        "polrocna financna sprava",
        "polrocna sprava",
    )
    interim = (
        "interim report",
        "interim financial",
        "stredrocna sprava",
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
        "rocna financna sprava",
        "auditorska sprava",
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

    if category_code and category_code not in PERIODIC_CATEGORY_CODES:
        return (
            "other_regulatory_announcement",
            f"Non-periodic CERI category: {category}",
            [],
            [],
        )

    return (
        "other_regulatory_announcement",
        "No accepted periodic report category or title term",
        [],
        [],
    )


def extract_slovakia_date_info(
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

    fiscal_match = re.search(r"\b(20\d{2})\s*/\s*(\d{2})\b", text)
    if fiscal_match:
        start_year = int(fiscal_match.group(1))
        end_suffix = int(fiscal_match.group(2))
        reporting_year = (start_year // 100) * 100 + end_suffix
        if reporting_year < start_year:
            reporting_year = start_year // 100 * 100 + end_suffix
        if reporting_year < start_year:
            reporting_year = start_year + 1
        source_period_raw = fiscal_match.group(0)
        reason = "Fiscal year pattern in title or filename"

    patterns = (
        (r"\b(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})\b", (1, 2, 3)),
        (r"\b(\d{1,2})[.](\d{1,2})[.](20\d{2})\b", (3, 2, 1)),
        (r"\bk\s+(\d{1,2})[.]\s*([a-z]+)\s+(\d{4})\b", None),
    )
    explicit_dates: list[tuple[date, str]] = []
    for pattern, order in patterns[:2]:
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

    slovak_months = {
        "januara": 1,
        "januar": 1,
        "februara": 2,
        "februar": 2,
        "marca": 3,
        "marec": 3,
        "aprila": 4,
        "april": 4,
        "maja": 5,
        "maj": 5,
        "juna": 6,
        "jun": 6,
        "jula": 7,
        "jul": 7,
        "augusta": 8,
        "august": 8,
        "septembra": 9,
        "september": 9,
        "oktobra": 10,
        "oktober": 10,
        "novembra": 11,
        "november": 11,
        "decembra": 12,
        "december": 12,
    }
    for match in re.finditer(
        r"\bk\s+(\d{1,2})[.]\s*([a-z]+)\s+(\d{4})\b",
        _normalize(text),
    ):
        day = int(match.group(1))
        month = slovak_months.get(match.group(2))
        year = int(match.group(3))
        if month is None:
            continue
        try:
            parsed = date(year, month, day)
        except ValueError:
            continue
        explicit_dates.append((parsed, match.group(0)))

    if explicit_dates:
        period_end, source_period_raw = max(explicit_dates, key=lambda item: item[0])
        reporting_year = period_end.year
        reason = "Explicit reporting-period date in title or attachment filename"

    classification = classify_slovakia_document(title, category, filename)[0]
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
                    ("stvrtrocna", 3),
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
    elif (
        reporting_year is not None
        and period_end is None
        and classification == "annual_financial_report"
    ):
        period_end = date(reporting_year, 12, 31)
        reason += "; annual period end inferred from fiscal year"

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
class SlovakiaFile:
    attachment_id: str
    filename: str
    download_url: str
    file_format: str | None


@dataclass(frozen=True, slots=True)
class SlovakiaNotice:
    record_id: str
    issuer_name: str
    title: str
    category: str
    category_code: str
    regulated: bool
    published_raw: str
    published_at: date | None
    files: tuple[SlovakiaFile, ...] = ()


@dataclass(frozen=True, slots=True)
class SlovakiaListingPage:
    notices: tuple[SlovakiaNotice, ...]


@dataclass(frozen=True, slots=True)
class SlovakiaSourceDiagnostic:
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
class SlovakiaSourceDiscovery:
    source: str
    query: str
    notices: tuple[SlovakiaNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SlovakiaIssuerResolution:
    found: bool
    matched_name: str | None = None
    source_record_id: str | None = None
    source_url: str | None = None
    detail_url: str | None = None
    home_member_state: str | None = "Slovakia"
    pea_country_check: str | None = "eu_candidate"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


def parse_slovakia_listing(
    html_text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> SlovakiaListingPage:
    soup = BeautifulSoup(html_text, "html.parser")
    notices: list[SlovakiaNotice] = []
    table = soup.find("table", class_="results")
    if table is None:
        return SlovakiaListingPage(notices=())

    rows = table.find_all("tr", recursive=False)
    index = 0
    while index < len(rows):
        row = rows[index]
        row_classes = row.get("class") or []
        if "even" not in row_classes:
            index += 1
            continue
        if index + 1 >= len(rows):
            break
        detail_row = rows[index + 1]
        detail_classes = detail_row.get("class") or []
        if "odd" not in detail_classes:
            index += 1
            continue

        img_span = row.find("span", class_="ceridoc", id=re.compile(r"^img"))
        title_span = detail_row.find("span", class_="ceridoc", id=re.compile(r"^tit"))
        if img_span is None or title_span is None:
            index += 2
            continue

        span_id = str(img_span.get("id") or "")
        try:
            filename, download_url = build_ceri_download_url(span_id, base_url=base_url)
        except ValueError:
            index += 2
            continue

        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            index += 2
            continue

        issuer_name = cells[1].get_text(" ", strip=True)
        regulated_marker = cells[2].get_text(" ", strip=True)
        category = cells[3].get_text(" ", strip=True)
        published_raw = cells[4].get_text(" ", strip=True) if len(cells) > 4 else ""
        title = title_span.get_text(" ", strip=True)
        category_code = _category_code(category)
        file_format = _file_format(filename, download_url)
        notices.append(
            SlovakiaNotice(
                record_id=filename,
                issuer_name=issuer_name,
                title=title,
                category=category,
                category_code=category_code,
                regulated="§" in regulated_marker
                or "regulovan" in _normalize(regulated_marker),
                published_raw=published_raw,
                published_at=_parse_date(published_raw),
                files=(
                    SlovakiaFile(
                        attachment_id=filename,
                        filename=filename,
                        download_url=download_url,
                        file_format=file_format,
                    ),
                ),
            )
        )
        index += 2

    return SlovakiaListingPage(notices=tuple(notices))


class SlovakiaNbsCeriConnector(Connector):
    market = "Bratislava Stock Exchange"
    source_name = "slovakia_nbs_ceri"
    supports_source_first = True
    requires_watchlist_queries = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str = DEFAULT_BASE_URL,
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 90,
        timeout: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.search_url = f"{self.base_url}{SEARCH_PATH}"
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        self._listing_cache: dict[
            tuple[date, date, str], tuple[SlovakiaNotice, ...]
        ] = {}
        self._notice_cache: dict[str, SlovakiaNotice] = {}
        self._scanned_notices = 0
        self._details_visited = 0
        self._cache_hits = 0

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _form_payload(
        self,
        *,
        from_date: date,
        to_date: date,
        category: str = GLOBAL_CATEGORY,
        query: str = "",
    ) -> dict[str, str]:
        payload = {
            "qissuer": "0",
            "qcathegory": category,
            "qico": "",
            "qisin": "",
            "qrdfrom": _format_ceri_date(from_date),
            "qrdto": _format_ceri_date(to_date),
            "search_set": SEARCH_SUBMIT,
        }
        if query:
            payload["query"] = query
        return payload

    def _fetch_listing(
        self,
        *,
        from_date: date,
        to_date: date,
        category: str = GLOBAL_CATEGORY,
        query: str = "",
        limit: int | None = None,
    ) -> tuple[SlovakiaNotice, ...]:
        cache_key = (from_date, to_date, category if not query else f"{category}:{query}")
        if cache_key in self._listing_cache:
            self._cache_hits += 1
            cached = self._listing_cache[cache_key]
            return cached[:limit] if limit is not None else cached

        self._wait()
        response: Any | None = None
        try:
            response = self.session.post(
                self.search_url,
                data=self._form_payload(
                    from_date=from_date,
                    to_date=to_date,
                    category=category,
                    query=query,
                ),
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            parsed = parse_slovakia_listing(response.text, base_url=self.base_url)
            result = tuple(parsed.notices)
            self._listing_cache[cache_key] = result
            for notice in result:
                self._notice_cache[notice.record_id] = notice
            self._scanned_notices += len(result)
            self.attempts.append(
                EndpointAttempt(
                    name="NBS CERI Slovakia periodic listing",
                    base_url=self.base_url,
                    dataset="CERI",
                    endpoint=SEARCH_PATH,
                    method="POST",
                    http_status=response.status_code,
                    success=True,
                    total_count=len(result),
                )
            )
            self.state = ConnectorState.READY
            self.last_error = None
            return result[:limit] if limit is not None else result
        except Exception as exc:
            self.state = ConnectorState.UNAVAILABLE
            self.last_error = str(exc)
            self.attempts.append(
                EndpointAttempt(
                    name="NBS CERI Slovakia periodic listing",
                    base_url=self.base_url,
                    dataset="CERI",
                    endpoint=SEARCH_PATH,
                    method="POST",
                    http_status=getattr(response, "status_code", None),
                    success=False,
                    error=str(exc),
                )
            )
            raise
        finally:
            self._last_request_at = time.monotonic()

    def _accepted_notice(self, notice: SlovakiaNotice) -> bool:
        if notice.category_code in REJECTED_INZERAT_CODES:
            return False
        if notice.category_code in PERIODIC_CATEGORY_CODES:
            return True
        return (
            classify_slovakia_document(
                notice.title,
                notice.category,
            )[0]
            != "other_regulatory_announcement"
        )

    def _notice_candidate(self, notice: SlovakiaNotice) -> DocumentCandidate:
        primary = notice.files[0] if notice.files else None
        document_type, reason, positive, negative = classify_slovakia_document(
            notice.title,
            notice.category,
            primary.filename if primary else "",
        )
        dates = extract_slovakia_date_info(
            notice.title,
            notice.published_raw,
            notice.category,
            primary.filename if primary else "",
        )
        return DocumentCandidate(
            title=notice.title,
            url=primary.download_url if primary else self.search_url,
            published_date=dates["published_at"],
            document_type=document_type,
            source=self.source_name,
            source_document_id=notice.record_id,
            metadata={
                "official_source": 1,
                "issuer_name": notice.issuer_name,
                "issuer_country": "Slovakia",
                "home_member_state": "Slovakia",
                "pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
                "record_id": notice.record_id,
                "category": notice.category,
                "category_code": notice.category_code,
                "regulated": int(notice.regulated),
                "received_at": notice.published_raw,
                "parent_page_url": self.search_url,
                "slovakia_nbs_ceri_url": self.search_url,
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

    def _notice_from_candidate(self, candidate: DocumentCandidate) -> SlovakiaNotice | None:
        record_id = str(candidate.metadata.get("record_id") or "")
        cached = self._notice_cache.get(record_id)
        if cached is not None:
            self._cache_hits += 1
            return cached

        files_meta = candidate.metadata.get("files") or []
        files: list[SlovakiaFile] = []
        for item in files_meta:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or item.get("attachment_id") or "")
            download_url = str(item.get("download_url") or candidate.url)
            files.append(
                SlovakiaFile(
                    attachment_id=str(item.get("attachment_id") or filename),
                    filename=filename,
                    download_url=download_url,
                    file_format=item.get("file_format"),
                )
            )
        if not files:
            return None
        return SlovakiaNotice(
            record_id=record_id,
            issuer_name=str(candidate.metadata.get("issuer_name") or ""),
            title=candidate.title,
            category=str(candidate.metadata.get("category") or ""),
            category_code=str(candidate.metadata.get("category_code") or ""),
            regulated=bool(candidate.metadata.get("regulated")),
            published_raw=candidate.source_publication_date_raw or "",
            published_at=candidate.published_at,
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
            document_type, reason, positive, negative = classify_slovakia_document(
                notice.title,
                notice.category,
                item.filename,
            )
            if document_type == "other_regulatory_announcement":
                continue
            dates = extract_slovakia_date_info(
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
                    "parent_page_url": self.search_url,
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
        notices: list[SlovakiaNotice] = []
        seen: set[str] = set()
        search_categories = ("1", "2", "3", GLOBAL_CATEGORY)
        for category in search_categories:
            remaining = None if limit is None else max(0, (limit or 0) - len(notices))
            if remaining == 0:
                break
            for notice in self._fetch_listing(
                from_date=start,
                to_date=end,
                category=category,
                limit=remaining,
            ):
                if notice.record_id in seen:
                    continue
                if not self._accepted_notice(notice):
                    continue
                seen.add(notice.record_id)
                notices.append(notice)
        candidates = [self._notice_candidate(notice) for notice in notices]
        return candidates[:limit] if limit is not None else candidates

    def search_documents_for_issuer(
        self,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        end = date.today()
        start = end - timedelta(days=self.lookback_days)
        issuer_query = str(issuer.name or "").split(",")[0].strip()
        notices: list[SlovakiaNotice] = []
        seen: set[str] = set()
        for category in ("1", "2", "3", GLOBAL_CATEGORY):
            for notice in self._fetch_listing(
                from_date=start,
                to_date=end,
                category=category,
                query=issuer_query,
            ):
                if notice.record_id in seen:
                    continue
                if not self._accepted_notice(notice):
                    continue
                seen.add(notice.record_id)
                notices.append(notice)
        return [self._notice_candidate(notice) for notice in notices]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def resolve_issuer(self, issuer: Issuer) -> SlovakiaIssuerResolution:
        end = date.today()
        start = end - timedelta(days=365 * 3)
        expected = _normalize_issuer(issuer.name)
        try:
            best: tuple[float, SlovakiaNotice] | None = None
            for notice in self._fetch_listing(
                from_date=start,
                to_date=end,
                limit=200,
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
                return SlovakiaIssuerResolution(
                    found=False,
                    attempts=tuple(self.attempts),
                    error="No matching issuer in NBS CERI Slovakia filings",
                )
            score, notice = best
            primary = notice.files[0] if notice.files else None
            return SlovakiaIssuerResolution(
                found=True,
                matched_name=notice.issuer_name,
                source_record_id=notice.record_id,
                source_url=self.search_url,
                detail_url=primary.download_url if primary else self.search_url,
                match_score=score,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return SlovakiaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def _discover_notices(
        self,
        *,
        from_date: date,
        to_date: date,
        category: str,
        issuer_query: str,
        limit: int,
    ) -> tuple[SlovakiaNotice, ...]:
        collected: list[SlovakiaNotice] = []
        seen: set[str] = set()
        for notice in self._fetch_listing(
            from_date=from_date,
            to_date=to_date,
            category=category,
            query=issuer_query,
            limit=limit,
        ):
            if notice.record_id in seen:
                continue
            if not self._accepted_notice(notice):
                continue
            seen.add(notice.record_id)
            collected.append(notice)
            if len(collected) >= limit:
                break
        return tuple(collected)

    def discover(self, query: str, limit: int = 25) -> SlovakiaSourceDiscovery:
        normalized = _normalize(query)
        issuer_query = ""
        type_keyword = False
        if "half year" in normalized or "semi annual" in normalized or "polrocna" in normalized:
            category = "2"
            type_keyword = True
        elif (
            "quarter" in normalized
            or "interim" in normalized
            or "stvrtrocna" in normalized
            or "predbezne" in normalized
        ):
            category = "3"
            type_keyword = True
        elif "annual" in normalized or "rocna" in normalized or "audit" in normalized:
            category = "1"
            type_keyword = True
        else:
            category = GLOBAL_CATEGORY
            issuer_query = query.strip()

        end = date.today()
        start = end - timedelta(days=365 * 3)
        try:
            notices: list[SlovakiaNotice] = []
            seen: set[str] = set()
            if type_keyword and not issuer_query:
                for fallback in DISCOVER_FALLBACK_ISSUER_QUERIES:
                    for notice in self._discover_notices(
                        from_date=start,
                        to_date=end,
                        category=category,
                        issuer_query=fallback,
                        limit=limit,
                    ):
                        if notice.record_id in seen:
                            continue
                        seen.add(notice.record_id)
                        notices.append(notice)
                        if len(notices) >= limit:
                            break
                    if len(notices) >= limit:
                        break
            else:
                notices.extend(
                    self._discover_notices(
                        from_date=start,
                        to_date=end,
                        category=category,
                        issuer_query=issuer_query,
                        limit=limit,
                    )
                )
            notice_tuple = tuple(notices[:limit])
            candidates = tuple(
                self._notice_candidate(notice) for notice in notice_tuple
            )
            return SlovakiaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=notice_tuple,
                candidates=candidates,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return SlovakiaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> SlovakiaSourceDiagnostic:
        end = date.today()
        start = end - timedelta(days=120)
        try:
            notices: list[SlovakiaNotice] = []
            seen: set[str] = set()
            for category in (GLOBAL_CATEGORY, "1", "2", "3"):
                for notice in self._fetch_listing(
                    from_date=start,
                    to_date=end,
                    category=category,
                    limit=200,
                ):
                    if notice.record_id in seen:
                        continue
                    seen.add(notice.record_id)
                    notices.append(notice)
            if not any(self._accepted_notice(notice) for notice in notices):
                for fallback in DISCOVER_FALLBACK_ISSUER_QUERIES:
                    for category in ("1", "2"):
                        for notice in self._fetch_listing(
                            from_date=start,
                            to_date=end,
                            category=category,
                            query=fallback,
                            limit=50,
                        ):
                            if notice.record_id in seen:
                                continue
                            seen.add(notice.record_id)
                            notices.append(notice)
            categories: dict[str, int] = {}
            for notice in notices:
                categories[notice.category] = categories.get(notice.category, 0) + 1

            periodic = next(
                (
                    notice
                    for notice in notices
                    if self._accepted_notice(notice)
                    and classify_slovakia_document(notice.title, notice.category)[0]
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
                    "title": periodic.title,
                    "category": periodic.category,
                    "category_code": periodic.category_code,
                    "regulated": periodic.regulated,
                    "published_at": (
                        periodic.published_at.isoformat()
                        if periodic.published_at
                        else None
                    ),
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
            return SlovakiaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.READY if example else ConnectorState.DEGRADED,
                called_url=self.search_url,
                http_status=status,
                method_used=(
                    "POST global CERI search with qissuer=0 and date window; "
                    "file URLs built client-side from ceridoc ids; no detail fetch"
                ),
                total_count=len(notices),
                detected_count=len(notices),
                attachment_count=attachment_count,
                fields=(
                    "record_id",
                    "issuer_name",
                    "title",
                    "category",
                    "category_code",
                    "regulated",
                    "published_at",
                    "download_url",
                ),
                categories=categories,
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=len(self.attempts),
                request_efficiency=(
                    "Bounded global POST /search per category plus optional "
                    "watchlist issuer queries when global filters are sparse; "
                    "materialization uses listing metadata only; cache hits: "
                    f"{self._cache_hits}"
                ),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return SlovakiaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.search_url,
                http_status=None,
                method_used="POST NBS CERI Slovakia HTML listing",
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
        return 4

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 4