from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from connectors.base import Connector, ConnectorState, DocumentCandidate, EndpointAttempt
from models import Issuer


DEFAULT_BASE_URL = "https://www.borzamalta.com.mt"
CDN_BASE_URL = "https://cdn.borzamalta.com.mt"
OAM_PATH = "/officially-appointed-mechanism"
DISCOVER_FALLBACK_ISSUER_QUERIES = (
    "Trident",
    "Simonds Farsons",
    "Bank of Valletta",
    "Malta International Airport",
)
SUPPORTED_FORMATS = {"pdf", "zip", "xhtml", "xht", "xml", "xbrl", "xbri"}
REJECTED_MARKETS = frozenset({"other", "ifsm"})
ESEF_PERIOD_RE = re.compile(
    r"_(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})_(?P<scope>CON|IND)_AFR_"
)
LEI_IN_ESEF_RE = re.compile(r"_AFR_([0-9A-Z]{20})_")

NEGATIVE_TERMS = (
    "prospectus",
    "final terms",
    "bond",
    "bonds",
    "notes",
    "debt",
    "share buyback",
    "share buy-back",
    "share buy back",
    "buy-back",
    "buyback",
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
    "annual general meeting",
    "extraordinary general meeting",
    "notice of meeting",
    "scheduled annual general meeting",
    "agm held",
    "agm agenda",
    "dividend announcement",
    "dividend distribution",
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
    "financial analysis summary",
    "p3 disclosures",
    "pillar 3",
    "governance code",
    "corporate governance",
    "resignation of",
    "appointment of director",
    "traffic results",
    "scrip dividend",
    "bond issue",
    "bondholder meeting",
    "convocation",
    "nomination of directors",
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
        r"\b(?:plc|p l c|ltd|limited|sicav|mtf)\b",
        " ",
        normalized,
    ).strip()


def _parse_published(value: object) -> date | None:
    raw = str(value or "").strip()
    for pattern in (
        "%d-%m-%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def normalize_malta_cdn_url(href: str, *, base_url: str = DEFAULT_BASE_URL) -> str:
    cleaned = str(href or "").strip()
    if not cleaned:
        return ""
    cleaned = cleaned.replace("\\", "")
    if cleaned.startswith("//"):
        cleaned = f"https:{cleaned}"
    return urljoin(base_url.rstrip("/") + "/", cleaned)


def _file_format(filename: str, url: str = "") -> str | None:
    for value in (filename, urlparse(url).path):
        suffix = PurePosixPath(value).suffix.casefold().lstrip(".")
        if suffix in SUPPORTED_FORMATS:
            return suffix
    lowered = _normalize(f"{filename} {url}")
    if "inlineviewer" in lowered or lowered.endswith(" xhtml"):
        return "xhtml"
    return None


def build_malta_oam_url(
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    market: str = "",
    issuer: str = "",
    search: str = "",
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    params: dict[str, str] = {}
    if from_date is not None:
        params["from"] = from_date.isoformat()
    if to_date is not None:
        params["to"] = to_date.isoformat()
    if market:
        params["market"] = market
    if issuer:
        params["issuer"] = issuer
    if search:
        params["search"] = search
    query = f"?{urlencode(params)}" if params else ""
    return f"{base_url.rstrip('/')}{OAM_PATH}{query}"


def extract_malta_lei(*urls: str) -> str | None:
    for url in urls:
        match = LEI_IN_ESEF_RE.search(str(url or ""))
        if match:
            return match.group(1)
        path_match = re.search(r"/([0-9A-Z]{20})[-/]", str(url or ""))
        if path_match and len(path_match.group(1)) == 20:
            return path_match.group(1)
    return None


def extract_malta_esef_period(*urls: str) -> tuple[date | None, int | None]:
    for url in urls:
        match = ESEF_PERIOD_RE.search(str(url or ""))
        if not match:
            continue
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        try:
            period_end = date(year, month, day)
        except ValueError:
            continue
        return period_end, year
    return None, None


def classify_malta_document(
    title: str,
    *,
    market: str = "",
    filenames: tuple[str, ...] = (),
    esef_urls: tuple[str, ...] = (),
) -> tuple[str, str, list[str], list[str]]:
    haystack = _normalize(
        " ".join((title, market, *filenames, *esef_urls))
    )
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

    if _normalize(market) in REJECTED_MARKETS:
        return (
            "other_regulatory_announcement",
            f"Rejected Malta market segment: {market}",
            [],
            [market],
        )

    esef_joined = " ".join(esef_urls).casefold()
    if "_afr_" in esef_joined or "esefapp" in haystack:
        if any(token in haystack for token in ("semi", "half year", "half yearly")):
            return (
                "half_year_financial_report",
                "Malta ESEF semi-annual financial report",
                ["esef_afr"],
                [],
            )
        if any(token in haystack for token in ("quarter", "interim", "q1", "q2", "q3", "q4")):
            document_type = (
                "quarterly_financial_report"
                if "quarter" in haystack or re.search(r"\bq[1-4]\b", haystack)
                else "interim_report"
            )
            return (
                document_type,
                "Malta ESEF interim/quarterly financial report",
                ["esef_afr"],
                [],
            )
        return (
            "annual_financial_report",
            "Malta ESEF annual financial report",
            ["esef_afr"],
            [],
        )

    quarterly = (
        "quarterly report",
        "quarterly trading update",
        "group quarterly",
        "interim report q1",
        "interim report q2",
        "interim report q3",
        "interim report q4",
        "q1 20",
        "q2 20",
        "q3 20",
        "q4 20",
    )
    half_year = (
        "half year",
        "half-year",
        "half yearly",
        "semi annual",
        "semi-annual",
    )
    interim = (
        "interim report",
        "interim financial",
        "interim management",
        "trading update",
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
        "annual consolidated accounts",
        "consolidated audited financial statements",
        "approval of annual",
        "audited financial statements",
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

    return (
        "other_regulatory_announcement",
        "No accepted periodic report category or title term",
        [],
        [],
    )


def extract_malta_date_info(
    title: str,
    published_raw: str | None,
    *,
    filenames: tuple[str, ...] = (),
    esef_urls: tuple[str, ...] = (),
) -> dict[str, Any]:
    published_at = _parse_published(published_raw)
    text = " ".join((title, *filenames, *esef_urls))
    period_end, reporting_year = extract_malta_esef_period(*esef_urls)
    source_period_raw: str | None = None
    reason = "No unambiguous reporting period detected"

    if period_end is not None:
        source_period_raw = period_end.isoformat()
        reason = "ESEF package period end in CDN path"

    patterns = (
        (r"\b(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})\b", (1, 2, 3)),
        (r"\b(\d{1,2})[.](\d{1,2})[.](20\d{2})\b", (3, 2, 1)),
        (r"\bfy\s*(?:ended\s*)?(\d{1,2})\s+([a-z]+)\s+(20\d{2})\b", None),
    )
    if period_end is None:
        fy_match = re.search(
            r"\bfy\s*(?:ended\s*)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*(20\d{2})\b",
            _normalize(text),
        )
        if fy_match:
            reporting_year = int(fy_match.group(1))
            source_period_raw = fy_match.group(0)
            reason = "Fiscal year token in Malta title"

    if period_end is None:
        explicit_dates: list[tuple[date, str]] = []
        for pattern, order in patterns[:2]:
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
            reason = "Explicit reporting-period date in title or attachment path"

    classification = classify_malta_document(
        title,
        filenames=filenames,
        esef_urls=esef_urls,
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
            reason = "Reporting year extracted from periodic report title"
            if classification == "annual_financial_report" and period_end is None:
                period_end = date(reporting_year, 12, 31)
                reason += "; annual period end inferred"
            elif classification == "half_year_financial_report" and period_end is None:
                period_end = date(reporting_year, 6, 30)
                reason += "; half-year period end inferred"

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
class MaltaFile:
    attachment_id: str
    filename: str
    download_url: str
    file_format: str | None
    link_label: str | None = None


@dataclass(frozen=True, slots=True)
class MaltaNotice:
    record_id: str
    issuer_name: str
    title: str
    market: str
    published_raw: str
    published_at: date | None
    issuer_lei: str | None
    listing_url: str
    files: tuple[MaltaFile, ...] = ()


@dataclass(frozen=True, slots=True)
class MaltaListingPage:
    notices: tuple[MaltaNotice, ...]


@dataclass(frozen=True, slots=True)
class MaltaSourceDiagnostic:
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
class MaltaSourceDiscovery:
    source: str
    query: str
    notices: tuple[MaltaNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MaltaIssuerResolution:
    found: bool
    matched_name: str | None = None
    source_record_id: str | None = None
    source_url: str | None = None
    detail_url: str | None = None
    home_member_state: str | None = "Malta"
    pea_country_check: str | None = "eu_candidate"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


def _row_published_raw(date_cell: Any) -> str:
    spans = date_cell.find_all("span")
    if len(spans) >= 2:
        return f"{spans[0].get_text(strip=True)} {spans[1].get_text(strip=True)}".strip()
    return date_cell.get_text(" ", strip=True)


def _announcement_code(filename: str) -> str:
    stem = PurePosixPath(filename).stem
    return stem or filename


def parse_malta_listing(
    html_text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    listing_url: str = "",
) -> MaltaListingPage:
    soup = BeautifulSoup(html_text, "html.parser")
    notices: list[MaltaNotice] = []
    parent_url = listing_url or f"{base_url.rstrip('/')}{OAM_PATH}"

    for row in soup.select("table tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue

        issuer_name = cells[1].get_text(" ", strip=True)
        title = cells[2].get_text(" ", strip=True)
        if not issuer_name or not title:
            continue
        if _normalize(issuer_name) in {"issuer", "date"}:
            continue

        market = cells[3].get_text(" ", strip=True) if len(cells) > 3 else ""
        published_raw = _row_published_raw(cells[0])
        files: list[MaltaFile] = []
        esef_urls: list[str] = []
        for link in row.find_all("a", href=True):
            href = normalize_malta_cdn_url(str(link.get("href") or ""))
            if not href.startswith("http"):
                continue
            label = link.get_text(" ", strip=True) or None
            filename = PurePosixPath(urlparse(href).path).name or href
            if "/download/announcements/" in href.casefold():
                code = _announcement_code(filename)
                files.append(
                    MaltaFile(
                        attachment_id=code,
                        filename=filename,
                        download_url=href,
                        file_format=_file_format(filename, href),
                        link_label=label,
                    )
                )
            elif "/esefapp/" in href.casefold():
                esef_urls.append(href)
                files.append(
                    MaltaFile(
                        attachment_id=filename,
                        filename=filename,
                        download_url=href,
                        file_format=_file_format(filename, href),
                        link_label=label,
                    )
                )

        if not files:
            continue

        primary_pdf = next(
            (item for item in files if item.file_format == "pdf"),
            files[0],
        )
        record_id = (
            f"{_normalize_issuer(issuer_name)}:"
            f"{published_raw}:"
            f"{_normalize(title)}:"
            f"{primary_pdf.attachment_id}"
        )
        issuer_lei = extract_malta_lei(*esef_urls)

        notices.append(
            MaltaNotice(
                record_id=record_id,
                issuer_name=issuer_name,
                title=title,
                market=market,
                published_raw=published_raw,
                published_at=_parse_published(published_raw),
                issuer_lei=issuer_lei,
                listing_url=parent_url,
                files=tuple(files),
            )
        )

    return MaltaListingPage(notices=tuple(notices))


class MaltaMseOamConnector(Connector):
    market = "Malta Stock Exchange"
    source_name = "malta_mse_oam"
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
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.oam_url = f"{self.base_url}{OAM_PATH}"
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        self._listing_cache: dict[str, tuple[MaltaNotice, ...]] = {}
        self._notice_cache: dict[str, MaltaNotice] = {}
        self._scanned_notices = 0
        self._details_visited = 0
        self._cache_hits = 0
        self._http_calls = 0

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
    ) -> tuple[MaltaNotice, ...]:
        cache_key = f"{from_date.isoformat()}:{to_date.isoformat()}"
        if cache_key in self._listing_cache:
            self._cache_hits += 1
            return self._listing_cache[cache_key]

        listing_url = build_malta_oam_url(
            from_date=from_date,
            to_date=to_date,
            base_url=self.base_url,
        )
        self._wait()
        response: Any | None = None
        try:
            response = self.session.get(
                listing_url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            self._http_calls += 1
            response.raise_for_status()
            parsed = parse_malta_listing(
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
                    name="MSE Malta OAM listing",
                    base_url=self.base_url,
                    dataset="OAM",
                    endpoint=OAM_PATH,
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
                    name="MSE Malta OAM listing",
                    base_url=self.base_url,
                    dataset="OAM",
                    endpoint=OAM_PATH,
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
    ) -> tuple[MaltaNotice, ...]:
        end = date.today()
        start = since or (end - timedelta(days=self.lookback_days))
        collected: list[MaltaNotice] = []
        for notice in self._fetch_listing(from_date=start, to_date=end):
            if since and notice.published_at and notice.published_at < since:
                continue
            if not self._accepted_notice(notice):
                continue
            collected.append(notice)
            if limit is not None and len(collected) >= limit:
                break
        return tuple(collected)

    def _notice_esef_urls(self, notice: MaltaNotice) -> tuple[str, ...]:
        return tuple(
            item.download_url
            for item in notice.files
            if "/esefapp/" in item.download_url.casefold()
        )

    def _notice_filenames(self, notice: MaltaNotice) -> tuple[str, ...]:
        return tuple(item.filename for item in notice.files)

    def _accepted_notice(self, notice: MaltaNotice) -> bool:
        return (
            classify_malta_document(
                notice.title,
                market=notice.market,
                filenames=self._notice_filenames(notice),
                esef_urls=self._notice_esef_urls(notice),
            )[0]
            != "other_regulatory_announcement"
        )

    def _notice_candidate(self, notice: MaltaNotice) -> DocumentCandidate:
        esef_urls = self._notice_esef_urls(notice)
        filenames = self._notice_filenames(notice)
        document_type, reason, positive, negative = classify_malta_document(
            notice.title,
            market=notice.market,
            filenames=filenames,
            esef_urls=esef_urls,
        )
        dates = extract_malta_date_info(
            notice.title,
            notice.published_raw,
            filenames=filenames,
            esef_urls=esef_urls,
        )
        primary = next(
            (item for item in notice.files if item.file_format == "pdf"),
            notice.files[0],
        )
        return DocumentCandidate(
            title=notice.title,
            url=primary.download_url,
            published_date=dates["published_at"],
            document_type=document_type,
            source=self.source_name,
            source_document_id=notice.record_id,
            metadata={
                "official_source": 1,
                "issuer_name": notice.issuer_name,
                "issuer_lei": notice.issuer_lei,
                "issuer_country": "Malta",
                "home_member_state": "Malta",
                "pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
                "record_id": notice.record_id,
                "market_segment": notice.market,
                "received_at": notice.published_raw,
                "parent_page_url": notice.listing_url,
                "malta_mse_oam_url": self.oam_url,
                "files": [
                    {
                        "attachment_id": item.attachment_id,
                        "filename": item.filename,
                        "download_url": item.download_url,
                        "file_format": item.file_format,
                        "link_label": item.link_label,
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
    ) -> MaltaNotice | None:
        record_id = str(candidate.metadata.get("record_id") or "")
        cached = self._notice_cache.get(record_id)
        if cached is not None:
            self._cache_hits += 1
            return cached

        files_meta = candidate.metadata.get("files") or []
        files: list[MaltaFile] = []
        for item in files_meta:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or item.get("attachment_id") or "")
            download_url = str(item.get("download_url") or candidate.url)
            files.append(
                MaltaFile(
                    attachment_id=str(item.get("attachment_id") or filename),
                    filename=filename,
                    download_url=download_url,
                    file_format=item.get("file_format"),
                    link_label=item.get("link_label"),
                )
            )
        if not files:
            return None
        esef_urls = tuple(
            item.download_url
            for item in files
            if "/esefapp/" in item.download_url.casefold()
        )
        return MaltaNotice(
            record_id=record_id,
            issuer_name=str(candidate.metadata.get("issuer_name") or ""),
            title=candidate.title,
            market=str(candidate.metadata.get("market_segment") or ""),
            published_raw=candidate.source_publication_date_raw or "",
            published_at=candidate.published_at,
            issuer_lei=str(candidate.metadata.get("issuer_lei") or "") or None,
            listing_url=str(
                candidate.metadata.get("parent_page_url") or self.oam_url
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

        esef_urls = self._notice_esef_urls(notice)
        materialized: list[DocumentCandidate] = []
        for item in notice.files:
            document_type, reason, positive, negative = classify_malta_document(
                notice.title,
                market=notice.market,
                filenames=(item.filename,),
                esef_urls=esef_urls if "/esefapp/" in item.download_url.casefold() else (),
            )
            if document_type == "other_regulatory_announcement":
                continue
            dates = extract_malta_date_info(
                notice.title,
                notice.published_raw,
                filenames=(item.filename,),
                esef_urls=esef_urls if "/esefapp/" in item.download_url.casefold() else (),
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
        notices = self._fetch_recent_notices(since=since, limit=limit)
        candidates = [self._notice_candidate(notice) for notice in notices]
        return candidates[:limit] if limit is not None else candidates

    def search_documents_for_issuer(
        self,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        end = date.today()
        start = end - timedelta(days=self.lookback_days)
        expected = _normalize_issuer(issuer.name)
        symbol_expected = _normalize(issuer.symbol)
        notices = self._fetch_recent_notices(since=start, limit=None)
        matched: list[DocumentCandidate] = []
        for notice in notices:
            observed = _normalize_issuer(notice.issuer_name)
            if expected and (
                expected == observed
                or expected in observed
                or observed in expected
            ):
                matched.append(self._notice_candidate(notice))
                continue
            if symbol_expected and symbol_expected in observed:
                matched.append(self._notice_candidate(notice))
        return matched

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def resolve_issuer(self, issuer: Issuer) -> MaltaIssuerResolution:
        expected = _normalize_issuer(issuer.name)
        try:
            best: tuple[float, MaltaNotice] | None = None
            for notice in self._fetch_recent_notices(since=None, limit=200):
                observed = _normalize_issuer(notice.issuer_name)
                score = 0.0
                if expected == observed:
                    score = 100.0
                elif expected and (expected in observed or observed in expected):
                    score = 85.0
                if score and (best is None or score > best[0]):
                    best = (score, notice)
            if best is None:
                return MaltaIssuerResolution(
                    found=False,
                    attempts=tuple(self.attempts),
                    error="No matching issuer in MSE Malta OAM filings",
                )
            score, notice = best
            primary = next(
                (item for item in notice.files if item.file_format == "pdf"),
                notice.files[0],
            )
            return MaltaIssuerResolution(
                found=True,
                matched_name=notice.issuer_name,
                source_record_id=notice.record_id,
                source_url=self.oam_url,
                detail_url=primary.download_url,
                match_score=score,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return MaltaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def discover(self, query: str, limit: int = 25) -> MaltaSourceDiscovery:
        normalized = _normalize(query)
        issuer_query = query.strip()
        if any(token in normalized for token in ("annual", "half year", "quarter", "interim")):
            issuer_query = ""

        try:
            notices: list[MaltaNotice] = []
            seen: set[str] = set()
            queries = (
                (issuer_query,)
                if issuer_query
                else DISCOVER_FALLBACK_ISSUER_QUERIES
            )
            for issuer_name in queries:
                for notice in self._fetch_recent_notices(since=None, limit=limit * 6):
                    if notice.record_id in seen:
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
            return MaltaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=notice_tuple,
                candidates=candidates,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return MaltaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> MaltaSourceDiagnostic:
        try:
            notices = list(self._fetch_recent_notices(since=None, limit=500))
            categories: dict[str, int] = {}
            for notice in notices:
                key = notice.market or "unknown"
                categories[key] = categories.get(key, 0) + 1

            periodic = next(
                (
                    notice
                    for notice in notices
                    if self._accepted_notice(notice)
                    and classify_malta_document(
                        notice.title,
                        market=notice.market,
                        filenames=self._notice_filenames(notice),
                        esef_urls=self._notice_esef_urls(notice),
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
                    "lei": periodic.issuer_lei,
                    "title": periodic.title,
                    "market": periodic.market,
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
            return MaltaSourceDiagnostic(
                source=self.source_name,
                state=self.state,
                called_url=build_malta_oam_url(
                    from_date=date.today() - timedelta(days=self.lookback_days),
                    to_date=date.today(),
                    base_url=self.base_url,
                ),
                http_status=status,
                method_used="GET",
                total_count=len(notices),
                detected_count=sum(1 for notice in notices if self._accepted_notice(notice)),
                attachment_count=attachment_count,
                fields=(
                    "published_at",
                    "issuer_name",
                    "title",
                    "market_segment",
                    "issuer_lei",
                    "files",
                ),
                categories=categories,
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=self._http_calls,
                request_efficiency=(
                    f"{self._http_calls} listing call(s); "
                    f"{self._scanned_notices} notices scanned; "
                    f"{self._cache_hits} cache hits"
                ),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return MaltaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.oam_url,
                http_status=None,
                method_used="GET",
                total_count=0,
                detected_count=0,
                attachment_count=0,
                fields=(),
                categories={},
                formats=(),
                example_notice=None,
                http_calls=self._http_calls,
                request_efficiency=f"{self._http_calls} listing call(s)",
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
        return 1