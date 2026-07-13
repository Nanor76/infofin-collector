from __future__ import annotations

import hashlib
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag

from connectors.base import (
    Connector,
    ConnectorState,
    DocumentCandidate,
    EndpointAttempt,
)
from models import Issuer

LOGGER = logging.getLogger(__name__)

ITALIAN_MARKETS = {
    "euronext milan",
    "euronext star milan",
    "euronext growth milan",
    "euronext miv milan",
}

FINANCIAL_CATEGORY_QUERIES = (
    ("100", "1.1"),
    ("101", "1.2"),
    ("1", "DOAG annual"),
    ("2", "DOAG half-year"),
    ("3", "DOAG interim"),
)

SUPPORTED_SUFFIXES = {".pdf", ".xhtml", ".xht", ".zip", ".xbri"}


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", ascii_value.casefold()).strip()


def _clean_company_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", normalize_text(value)).strip()
    suffixes = {
        "as",
        "asa",
        "corp",
        "corporation",
        "group",
        "holding",
        "holdings",
        "inc",
        "limited",
        "ltd",
        "nv",
        "plc",
        "sa",
        "spa",
        "srl",
    }
    parts = normalized.split()
    while parts and parts[-1] in suffixes:
        parts.pop()
    return " ".join(parts)


def _category_kind(category: str | None) -> str | None:
    value = normalize_text(category or "")
    if value in {"100", "1.1"} or value.startswith("1.1 "):
        return "annual"
    if value in {"101", "1.2"} or value.startswith("1.2 "):
        return "half_year"
    if value in {"1", "58"} or "doag annual" in value:
        return "annual"
    if value in {"2", "59"} or "doag half-year" in value:
        return "half_year"
    if value in {"3", "60"} or "doag interim" in value:
        return "interim"
    if "doag" in value:
        return "financial"
    return None


def classify_italy_document(
    title: str,
    category: str | None = None,
    url: str = "",
) -> str | None:
    title_norm = normalize_text(title)
    category_kind = _category_kind(category)

    if any(
        term in title_norm
        for term in (
            "audit",
            "collegio sindacale",
            "relazione della societa di revisione",
            "relazione societa di revisione",
            "societa di revisione",
            "relazione di revisione",
            "revisione contabile",
            "revisore",
        )
    ):
        return "audit_report"

    if any(
        term in title_norm
        for term in (
            "assemblea",
            "shareholders meeting",
            "minutes of the",
            "verbale",
            "odg",
            "ordine del giorno",
            "approva il bilancio",
            "approvazione del bilancio",
            "remunerazione",
            "remuneration",
            "governo societario",
            "corporate governance",
            "assetti proprietari",
            "relazione illustrativa",
            "presentation",
            "analyst presentation",
            "investor presentation",
            "investors presentation",
        )
    ):
        return "other_regulatory_announcement"

    annual_terms = (
        "relazione finanziaria annuale",
        "relazioni finanziarie annuali",
        "annual financial report",
        "annual financial statements",
        "statutory annual",
        "annual report",
        "bilancio d esercizio",
        "bilancio di esercizio",
        "bilancio consolidato",
        "bilancio esef",
        "fascicolo di bilancio",
    )
    half_year_terms = (
        "relazione finanziaria semestrale",
        "relazioni finanziarie semestrali",
        "relazione semestrale",
        "half-year financial report",
        "half year financial report",
        "half-year report",
        "half yearly report",
    )
    interim_terms = (
        "informazioni finanziarie periodiche aggiuntive",
        "interim financial report",
        "interim report",
        "quarterly report",
        "resoconti intermedi",
        "resoconto intermedio",
    )

    if any(term in title_norm for term in annual_terms):
        return "annual_financial_report"
    if any(term in title_norm for term in half_year_terms):
        return "half_year_financial_report"
    if any(term in title_norm for term in interim_terms):
        return "quarterly_financial_report"
    if category_kind == "annual":
        return "annual_financial_report"
    if category_kind == "half_year":
        return "half_year_financial_report"
    if category_kind == "interim":
        return "quarterly_financial_report"
    if category_kind == "financial":
        return "financial_report"
    if "financial report" in title_norm:
        return "financial_report"
    return None


def _file_format(url: str) -> str | None:
    suffix = PurePosixPath(urlparse(url).path).suffix.casefold()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".xhtml", ".xht"}:
        return "xhtml"
    if suffix in {".zip", ".xbri"}:
        return "zip"
    return None


def _parse_date(value: str) -> date | None:
    compact = " ".join((value or "").split())
    for pattern in (
        r"(\d{2}/\d{2}/\d{4})(?:\s*-\s*\d{2}:\d{2})?",
        r"(\d{4}-\d{2}-\d{2})",
    ):
        match = re.search(pattern, compact)
        if not match:
            continue
        for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(match.group(1), fmt).date()
            except ValueError:
                continue
    return None


def _is_document_url(url: str) -> bool:
    parsed = urlparse(url)
    suffix = PurePosixPath(parsed.path).suffix.casefold()
    return (
        suffix in SUPPORTED_SUFFIXES
        or "/sites/default/files/comunicati/" in parsed.path
        or "/sites/default/files/xbrl/" in parsed.path
    )


def _first_text(root: Tag, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        node = root.select_one(selector)
        if node:
            value = " ".join(node.get_text(" ", strip=True).split())
            if value:
                return value
    return ""


@dataclass(frozen=True, slots=True)
class ItalyNotice:
    published_date: date | None
    company: str
    title: str
    document_url: str
    detail_url: str | None
    protocol: str | None
    category: str | None


@dataclass(frozen=True, slots=True)
class ParsedItalyPage:
    notices: tuple[ItalyNotice, ...]
    categories: tuple[str, ...]
    companies: tuple[tuple[str, str], ...]
    markets: tuple[str, ...]
    next_url: str | None
    has_pagination: bool


def parse_emarket_html(
    html: str,
    *,
    base_url: str,
    category: str | None = None,
) -> ParsedItalyPage:
    soup = BeautifulSoup(html or "", "html.parser")
    categories = tuple(
        " ".join(option.get_text(" ", strip=True).split())
        for option in soup.select('select[name="categoria"] option')
        if option.get("value") not in {None, "", "All"}
        and option.get_text(strip=True)
    )
    companies = tuple(
        (
            " ".join(option.get_text(" ", strip=True).split()),
            str(option.get("value")),
        )
        for option in soup.select('select[name="azienda"] option')
        if option.get("value") not in {None, "", "All"}
        and option.get_text(strip=True)
    )
    markets = tuple(
        " ".join(option.get_text(" ", strip=True).split())
        for option in soup.select('select[name="mercato"] option')
        if option.get("value") not in {None, "", "All", "---"}
        and option.get_text(strip=True)
    )

    rows = list(soup.select(".views-row"))
    if not rows:
        seen_rows: set[int] = set()
        for wrapper in soup.select("[data-protocollo]"):
            row = wrapper.find_parent(
                lambda tag: isinstance(tag, Tag)
                and "views-row" in (tag.get("class") or [])
            )
            candidate = row or wrapper
            if id(candidate) not in seen_rows:
                rows.append(candidate)
                seen_rows.add(id(candidate))

    notices: list[ItalyNotice] = []
    for row in rows:
        wrapper = row.select_one("[data-protocollo]")
        protocol = (
            str(wrapper.get("data-protocollo")).strip()
            if wrapper and wrapper.get("data-protocollo")
            else None
        )
        if not protocol:
            protocol_match = re.search(
                r"(?:N\.?\s*PROT\.?|protocollo)\s*:?\s*(\d+)",
                row.get_text(" ", strip=True),
                flags=re.IGNORECASE,
            )
            protocol = protocol_match.group(1) if protocol_match else None

        time_node = row.select_one("time")
        date_text = time_node.get_text(" ", strip=True) if time_node else ""
        published_date = _parse_date(date_text)
        if published_date is None and time_node:
            published_date = _parse_date(str(time_node.get("datetime") or ""))

        company = _first_text(
            row,
            (
                ".news-azienda",
                ".company",
                ".issuer",
                "[data-company]",
            ),
        )
        title_node = None
        for selector in (
            ".news-title",
            ".views-field-title",
            "h2",
            "h3",
        ):
            title_node = row.select_one(selector)
            if title_node:
                break
        title = (
            " ".join(title_node.get_text(" ", strip=True).split())
            if title_node
            else ""
        )

        anchors = [
            anchor
            for anchor in row.select("a[href]")
            if str(anchor.get("href") or "").strip()
        ]
        document_anchor = next(
            (
                anchor
                for anchor in anchors
                if _is_document_url(
                    urljoin(base_url, str(anchor.get("href")).strip())
                )
            ),
            None,
        )
        if document_anchor is None:
            continue
        document_url = urljoin(
            base_url,
            str(document_anchor.get("href")).strip(),
        )
        if not title:
            title = " ".join(
                document_anchor.get_text(" ", strip=True).split()
            )
        detail_anchor = next(
            (
                anchor
                for anchor in anchors
                if not _is_document_url(
                    urljoin(base_url, str(anchor.get("href")).strip())
                )
            ),
            None,
        )
        detail_url = (
            urljoin(base_url, str(detail_anchor.get("href")).strip())
            if detail_anchor
            else None
        )
        row_category = category
        if not row_category:
            category_node = row.select_one(
                ".news-category, .category, [data-category]"
            )
            if category_node:
                row_category = (
                    str(category_node.get("data-category") or "").strip()
                    or category_node.get_text(" ", strip=True)
                )
        notices.append(
            ItalyNotice(
                published_date=published_date,
                company=company,
                title=title,
                document_url=document_url,
                detail_url=detail_url,
                protocol=protocol,
                category=row_category,
            )
        )

    next_anchor = soup.select_one(
        'a[rel="next"], .pager__item--next a, li.next a'
    )
    next_url = (
        urljoin(base_url, str(next_anchor.get("href")).strip())
        if next_anchor and next_anchor.get("href")
        else None
    )
    has_pagination = bool(
        next_url
        or soup.select_one("nav.pager, ul.pager, ul.pagination")
    )
    return ParsedItalyPage(
        notices=tuple(notices),
        categories=categories,
        companies=companies,
        markets=markets,
        next_url=next_url,
        has_pagination=has_pagination,
    )


def issuer_notice_match_score(issuer: Issuer, notice: ItalyNotice) -> float:
    target = _clean_company_name(issuer.name)
    company = _clean_company_name(notice.company)
    if not target or not company:
        return 0.0
    if target == company:
        return 1.0
    score = SequenceMatcher(None, target, company).ratio()
    if min(len(target), len(company)) >= 5 and (
        target in company or company in target
    ):
        score = max(score, 0.94)

    symbol = re.sub(r"[^a-z0-9]", "", normalize_text(issuer.symbol))
    company_tokens = company.split()
    initials = "".join(token[0] for token in company_tokens if token)
    notice_text = normalize_text(f"{notice.company} {notice.title}")
    if len(symbol) >= 2 and (
        symbol == initials
        or re.search(rf"\b{re.escape(symbol)}\b", notice_text)
    ):
        score = max(score, 0.9)
    return score


def match_issuer_notice(
    issuer: Issuer,
    notice: ItalyNotice,
    *,
    threshold: float = 0.84,
) -> bool:
    return issuer_notice_match_score(issuer, notice) >= threshold


@dataclass(frozen=True, slots=True)
class ItalyEndpointCandidate:
    url: str
    role: str
    format: str
    pagination: str | None
    fields: tuple[str, ...]
    verified: bool
    state: ConnectorState
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class ItalySourceDiscovery:
    source: str
    query: str
    candidates: tuple[ItalyEndpointCandidate, ...]
    notices: tuple[ItalyNotice, ...]
    attempts: tuple[EndpointAttempt, ...]


@dataclass(frozen=True, slots=True)
class ItalySourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    http_status: int | None
    total_count: int | None
    detected_count: int
    categories: tuple[str, ...]
    example_document: dict[str, Any] | None
    checks: dict[str, bool]
    fallback_sources: dict[str, str]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ItalyIssuerResolution:
    found: bool
    requested_name: str
    matched_name: str | None
    symbol: str
    emarket_issuer_id: str | None
    storage_provider: str
    emarket_url: str | None
    oneinfo_url: str
    borsa_italiana_company_url: str | None
    match_score: float | None
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


class ItalyEmarketStorageConnector(Connector):
    source_name = "emarketstorage"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        home_url: str = "https://www.emarketstorage.it/",
        press_releases_url: str,
        documents_url: str,
        oneinfo_url: str = "https://www.1info.it/PORTALE1INFO",
        borsa_company_base_url: str = (
            "https://www.borsaitaliana.it/borsa/azioni/scheda"
        ),
        market: str = "Euronext Milan",
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 400,
        timeout: int = 30,
        verify_ssl: bool = True,
        max_pages: int = 2,
    ) -> None:
        self.session = session
        self.home_url = home_url
        self.press_releases_url = press_releases_url
        self.documents_url = documents_url
        self.oneinfo_url = oneinfo_url
        self.borsa_company_base_url = borsa_company_base_url.rstrip("/")
        self.market = market
        self.rate_limit_seconds = rate_limit_seconds
        self.lookback_days = lookback_days
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.max_pages = max(1, max_pages)
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self._last_request_at: float | None = None
        self._robots_parsers: dict[str, RobotFileParser | None] = {}
        self._company_options: tuple[tuple[str, str], ...] | None = None
        self._company_attempts: list[EndpointAttempt] = []
        self._market_notices: tuple[ItalyNotice, ...] | None = None
        self._market_attempts: list[EndpointAttempt] = []

        self.session.headers.update(
            {
                "User-Agent": self.session.headers.get(
                    "User-Agent",
                    "InfoFin/1.0 (+financial disclosure monitor)",
                )
            }
        )
        if not verify_ssl:
            LOGGER.warning(
                "ITALY_EMARKET_VERIFY_SSL=false: validation TLS désactivée"
            )

    def _wait(self) -> None:
        if self._last_request_at is None or not self.rate_limit_seconds:
            return
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _raw_get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> requests.Response:
        self._wait()
        try:
            return self.session.get(
                url,
                params=params,
                timeout=self.timeout,
                verify=self.verify_ssl,
                stream=stream,
            )
        finally:
            self._last_request_at = time.monotonic()

    def _robots_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_parsers:
            parser = RobotFileParser()
            try:
                response = self._raw_get(
                    f"{origin}/robots.txt",
                    stream=False,
                )
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                    self._robots_parsers[origin] = parser
                else:
                    self._robots_parsers[origin] = None
                response.close()
            except Exception as exc:
                LOGGER.debug("robots.txt indisponible pour %s: %s", origin, exc)
                self._robots_parsers[origin] = None
        parser = self._robots_parsers[origin]
        if parser is None:
            return True
        return parser.can_fetch(
            self.session.headers.get("User-Agent", "InfoFin"),
            url,
        )

    def _request_text(
        self,
        *,
        name: str,
        url: str,
        params: dict[str, Any] | None = None,
        check_robots: bool = True,
    ) -> tuple[EndpointAttempt, str | None]:
        endpoint = requests.Request("GET", url, params=params).prepare().url or url
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        if check_robots and not self._robots_allowed(endpoint):
            return (
                EndpointAttempt(
                    name=name,
                    base_url=base_url,
                    dataset=None,
                    endpoint=endpoint,
                    method="GET",
                    http_status=None,
                    success=False,
                    error="interdit par robots.txt",
                ),
                None,
            )
        try:
            response = self._raw_get(url, params=params)
        except Exception as exc:
            return (
                EndpointAttempt(
                    name=name,
                    base_url=base_url,
                    dataset=None,
                    endpoint=endpoint,
                    method="GET",
                    http_status=None,
                    success=False,
                    error=f"réseau: {exc}",
                ),
                None,
            )

        status = int(response.status_code)
        response_url = getattr(response, "url", endpoint)
        text = response.text
        response.close()
        if status >= 400:
            return (
                EndpointAttempt(
                    name=name,
                    base_url=base_url,
                    dataset=None,
                    endpoint=response_url,
                    method="GET",
                    http_status=status,
                    success=False,
                    response_excerpt=text[:600],
                    error=f"HTTP {status}",
                ),
                None,
            )
        if not text.strip():
            return (
                EndpointAttempt(
                    name=name,
                    base_url=base_url,
                    dataset=None,
                    endpoint=response_url,
                    method="GET",
                    http_status=status,
                    success=False,
                    error="parsing: réponse vide",
                ),
                None,
            )
        return (
            EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=None,
                endpoint=response_url,
                method="GET",
                http_status=status,
                success=True,
            ),
            text,
        )

    def _request_head(
        self,
        *,
        name: str,
        url: str,
    ) -> EndpointAttempt:
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        try:
            self._wait()
            response = self.session.head(
                url,
                timeout=self.timeout,
                verify=self.verify_ssl,
                allow_redirects=True,
            )
            self._last_request_at = time.monotonic()
        except Exception as exc:
            return EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=None,
                endpoint=url,
                method="HEAD",
                http_status=None,
                success=False,
                error=f"réseau: {exc}",
            )
        status = int(response.status_code)
        endpoint = getattr(response, "url", url)
        response.close()
        return EndpointAttempt(
            name=name,
            base_url=base_url,
            dataset=None,
            endpoint=endpoint,
            method="HEAD",
            http_status=status,
            success=status < 400,
            error=f"HTTP {status}" if status >= 400 else None,
        )

    def _load_company_options(self) -> tuple[tuple[str, str], ...]:
        if self._company_options is not None:
            return self._company_options
        attempt, html = self._request_text(
            name="italy_company_options",
            url=self.documents_url,
        )
        self._company_attempts.append(attempt)
        if not html:
            self._company_options = ()
            return self._company_options
        parsed = parse_emarket_html(html, base_url=attempt.endpoint)
        self._company_options = parsed.companies
        return self._company_options

    @staticmethod
    def _best_company_match(
        name: str,
        symbol: str,
        options: tuple[tuple[str, str], ...],
    ) -> tuple[str, str, float] | None:
        target = _clean_company_name(name)
        symbol_norm = re.sub(r"[^a-z0-9]", "", normalize_text(symbol))
        best: tuple[str, str, float] | None = None
        for company_name, company_id in options:
            company = _clean_company_name(company_name)
            score = SequenceMatcher(None, target, company).ratio()
            if target == company:
                score = 1.0
            elif min(len(target), len(company)) >= 5 and (
                target in company or company in target
            ):
                score = max(score, 0.94)
            initials = "".join(part[0] for part in company.split() if part)
            if len(symbol_norm) >= 2 and symbol_norm == initials:
                score = max(score, 0.9)
            if best is None or score > best[2]:
                best = (company_name, company_id, score)
        if best and best[2] >= 0.84:
            return best
        return None

    def resolve_issuer(
        self,
        *,
        symbol: str,
        name: str,
        isin: str | None = None,
    ) -> ItalyIssuerResolution:
        options = self._load_company_options()
        match = self._best_company_match(name, symbol, options)
        if not match:
            return ItalyIssuerResolution(
                found=False,
                requested_name=name,
                matched_name=None,
                symbol=symbol,
                emarket_issuer_id=None,
                storage_provider="emarketstorage",
                emarket_url=None,
                oneinfo_url=self.oneinfo_url,
                borsa_italiana_company_url=(
                    f"{self.borsa_company_base_url}/{isin}.html?lang=it"
                    if isin
                    else None
                ),
                match_score=None,
                attempts=tuple(self._company_attempts),
                error="émetteur absent de la liste EMARKET STORAGE",
            )
        matched_name, company_id, score = match
        emarket_url = f"{self.documents_url}?{urlencode({'azienda': company_id})}"
        return ItalyIssuerResolution(
            found=True,
            requested_name=name,
            matched_name=matched_name,
            symbol=symbol,
            emarket_issuer_id=company_id,
            storage_provider="emarketstorage",
            emarket_url=emarket_url,
            oneinfo_url=self.oneinfo_url,
            borsa_italiana_company_url=(
                f"{self.borsa_company_base_url}/{isin}.html?lang=it"
                if isin
                else None
            ),
            match_score=score,
            attempts=tuple(self._company_attempts),
        )

    def _load_market_notices(self) -> tuple[ItalyNotice, ...]:
        if self._market_notices is not None:
            return self._market_notices
        notices_by_url: dict[str, ItalyNotice] = {}
        any_html = False
        lookback_limit = date.today() - timedelta(days=self.lookback_days)

        for category_value, category_label in FINANCIAL_CATEGORY_QUERIES:
            for page in range(self.max_pages):
                params: dict[str, Any] = {
                    "categoria": category_value,
                    "mercato": self.market,
                }
                if page:
                    params["page"] = page
                attempt, html = self._request_text(
                    name=f"italy_documents_{category_value}_page_{page}",
                    url=self.documents_url,
                    params=params,
                )
                self._market_attempts.append(attempt)
                if not html:
                    break
                any_html = True
                parsed = parse_emarket_html(
                    html,
                    base_url=attempt.endpoint,
                    category=category_label,
                )
                for notice in parsed.notices:
                    if (
                        notice.published_date
                        and notice.published_date < lookback_limit
                    ):
                        continue
                    if classify_italy_document(
                        notice.title,
                        notice.category,
                        notice.document_url,
                    ):
                        notices_by_url.setdefault(
                            notice.document_url,
                            notice,
                        )
                if not parsed.next_url:
                    break

        self._market_notices = tuple(notices_by_url.values())
        if self._market_notices:
            self.state = ConnectorState.READY
            self.last_error = None
        elif any_html:
            self.mark_degraded(
                "HTML EMARKET STORAGE accessible, aucune notice financière exploitable"
            )
        else:
            self.state = ConnectorState.UNAVAILABLE
            self.last_error = "EMARKET STORAGE inaccessible"
        return self._market_notices

    def _issuer_specific_notices(self, issuer: Issuer) -> tuple[ItalyNotice, ...]:
        resolution = self.resolve_issuer(
            symbol=issuer.symbol,
            name=issuer.name,
            isin=issuer.isin,
        )
        if not resolution.found or not resolution.emarket_issuer_id:
            return ()
        found: dict[str, ItalyNotice] = {}
        for page in range(self.max_pages):
            params: dict[str, Any] = {
                "azienda": resolution.emarket_issuer_id,
            }
            if page:
                params["page"] = page
            attempt, html = self._request_text(
                name=f"italy_issuer_{resolution.emarket_issuer_id}_page_{page}",
                url=self.documents_url,
                params=params,
            )
            self._market_attempts.append(attempt)
            if not html:
                break
            parsed = parse_emarket_html(html, base_url=attempt.endpoint)
            for notice in parsed.notices:
                if classify_italy_document(
                    notice.title,
                    notice.category,
                    notice.document_url,
                ):
                    found.setdefault(notice.document_url, notice)
            if not parsed.next_url:
                break
        return tuple(found.values())

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        if issuer.market.casefold() not in ITALIAN_MARKETS:
            return []
        market_notices = self._load_market_notices()
        matched = [
            notice
            for notice in market_notices
            if match_issuer_notice(issuer, notice)
        ]
        if not matched and self.state != ConnectorState.UNAVAILABLE:
            matched = [
                notice
                for notice in self._issuer_specific_notices(issuer)
                if match_issuer_notice(issuer, notice)
            ]
        if matched:
            self.state = ConnectorState.READY
            self.last_error = None

        candidates: list[DocumentCandidate] = []
        seen_urls: set[str] = set()
        for notice in matched:
            if notice.document_url in seen_urls:
                continue
            document_type = classify_italy_document(
                notice.title,
                notice.category,
                notice.document_url,
            )
            if not document_type:
                continue
            seen_urls.add(notice.document_url)
            source_document_id = notice.protocol
            if not source_document_id:
                source_document_id = hashlib.sha256(
                    (
                        f"{notice.company}|{notice.published_date}|"
                        f"{notice.document_url}"
                    ).encode("utf-8")
                ).hexdigest()[:20]
            candidates.append(
                DocumentCandidate(
                    title=notice.title,
                    url=notice.document_url,
                    published_date=notice.published_date,
                    document_type=document_type,
                    source=self.source_name,
                    source_document_id=source_document_id,
                    metadata={
                        "company": notice.company,
                        "issuer_name": notice.company,
                        "issuer_isins": [],
                        "issuer_symbol": None,
                        "category": notice.category,
                        "detail_url": notice.detail_url,
                        "protocol": notice.protocol,
                        "provider": "emarketstorage",
                        "file_format": _file_format(notice.document_url),
                    },
                )
            )
        return candidates

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() not in ITALIAN_MARKETS:
            return []
        cutoff = since or (date.today() - timedelta(days=7))
        candidate_limit = max(1, limit or 1000)
        notices = [
            notice
            for notice in self._load_market_notices()
            if (
                notice.published_date is None
                or notice.published_date >= cutoff
            )
        ]
        self._scanned_notices = len(notices)
        candidates: list[DocumentCandidate] = []
        for notice in notices:
            document_type = classify_italy_document(
                notice.title,
                notice.category,
                notice.document_url,
            )
            if not document_type:
                continue
            source_document_id = notice.protocol or hashlib.sha256(
                (
                    f"{notice.company}|{notice.published_date}|"
                    f"{notice.document_url}"
                ).encode("utf-8")
            ).hexdigest()[:20]
            candidates.append(
                DocumentCandidate(
                    title=notice.title,
                    url=notice.document_url,
                    published_date=notice.published_date,
                    document_type=document_type,
                    source=self.source_name,
                    source_document_id=source_document_id,
                    metadata={
                        "company": notice.company,
                        "issuer_name": notice.company,
                        "issuer_isins": [],
                        "issuer_symbol": None,
                        "category": notice.category,
                        "protocol": notice.protocol,
                        "detail_url": notice.detail_url,
                        "file_format": _file_format(notice.document_url),
                    },
                )
            )
            if len(candidates) >= candidate_limit:
                break
        return candidates

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        return 1 + self.max_pages * len(FINANCIAL_CATEGORY_QUERIES)

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 3 + self.max_pages * (
            len(FINANCIAL_CATEGORY_QUERIES) + 1
        )

    @staticmethod
    def _notice_output(notice: ItalyNotice) -> dict[str, Any]:
        return {
            "date": (
                notice.published_date.isoformat()
                if notice.published_date
                else None
            ),
            "company": notice.company,
            "title": notice.title,
            "document_url": notice.document_url,
            "detail_url": notice.detail_url,
            "protocol": notice.protocol,
            "category": notice.category,
        }

    def diagnose(self) -> ItalySourceDiagnostic:
        attempts: list[EndpointAttempt] = []
        parsed_pages: dict[str, ParsedItalyPage] = {}
        html_by_name: dict[str, str] = {}
        for name, url in (
            ("italy_home", self.home_url),
            ("italy_documents", self.documents_url),
            ("italy_press_releases", self.press_releases_url),
        ):
            attempt, html = self._request_text(name=name, url=url)
            attempts.append(attempt)
            if html:
                html_by_name[name] = html
                parsed_pages[name] = parse_emarket_html(
                    html,
                    base_url=attempt.endpoint,
                )

        listing_page = parsed_pages.get("italy_documents")
        if listing_page is None:
            listing_page = parsed_pages.get("italy_press_releases")
        pagination_success = False
        if listing_page and listing_page.next_url:
            attempt, page_html = self._request_text(
                name="italy_pagination",
                url=listing_page.next_url,
            )
            attempts.append(attempt)
            pagination_success = bool(page_html and attempt.success)

        all_notices = [
            notice
            for page_name, page in parsed_pages.items()
            if page_name in {"italy_documents", "italy_press_releases"}
            for notice in page.notices
        ]
        direct_notice = next(
            (
                notice
                for notice in all_notices
                if _is_document_url(notice.document_url)
            ),
            None,
        )
        direct_success = False
        if direct_notice:
            direct_attempt = self._request_head(
                name="italy_direct_document",
                url=direct_notice.document_url,
            )
            attempts.append(direct_attempt)
            direct_success = direct_attempt.success

        oneinfo_attempt, oneinfo_html = self._request_text(
            name="italy_1info_fallback",
            url=self.oneinfo_url,
            check_robots=False,
        )
        attempts.append(oneinfo_attempt)
        oneinfo_state = ConnectorState.STUB
        if oneinfo_html:
            oneinfo_parsed = parse_emarket_html(
                oneinfo_html,
                base_url=oneinfo_attempt.endpoint,
            )
            if oneinfo_parsed.notices:
                oneinfo_state = ConnectorState.DEGRADED

        categories = tuple(
            dict.fromkeys(
                category
                for page in parsed_pages.values()
                for category in page.categories
            )
        )
        category_text = normalize_text(" ".join(categories))
        categories_visible = (
            "1.1" in category_text
            and "1.2" in category_text
            and "doag" in category_text
        )
        checks = {
            "home_accessible": "italy_home" in html_by_name,
            "documents_accessible": "italy_documents" in html_by_name,
            "press_releases_accessible": (
                "italy_press_releases" in html_by_name
            ),
            "pagination": pagination_success,
            "direct_document_link": direct_notice is not None,
            "direct_document_reachable": direct_success,
            "categories_visible": categories_visible,
            "real_notices": bool(all_notices),
        }

        listing_accessible = (
            checks["documents_accessible"]
            or checks["press_releases_accessible"]
        )
        if not listing_accessible:
            state = ConnectorState.UNAVAILABLE
        elif (
            checks["real_notices"]
            and checks["direct_document_link"]
            and checks["direct_document_reachable"]
            and checks["categories_visible"]
            and checks["pagination"]
        ):
            state = ConnectorState.READY
        else:
            state = ConnectorState.DEGRADED
        self.state = state
        missing = [name for name, success in checks.items() if not success]
        self.last_error = (
            "Contrôles incomplets: " + ", ".join(missing)
            if state == ConnectorState.DEGRADED
            else (
                "EMARKET STORAGE inaccessible"
                if state == ConnectorState.UNAVAILABLE
                else None
            )
        )

        example_notice = next(
            (
                notice
                for notice in all_notices
                if classify_italy_document(
                    notice.title,
                    notice.category,
                    notice.document_url,
                )
            ),
            direct_notice,
        )
        documents_attempt = next(
            (
                attempt
                for attempt in attempts
                if attempt.name == "italy_documents"
            ),
            attempts[0],
        )
        return ItalySourceDiagnostic(
            source=self.source_name,
            state=state,
            called_url=documents_attempt.endpoint,
            http_status=documents_attempt.http_status,
            total_count=None,
            detected_count=len(all_notices),
            categories=categories,
            example_document=(
                self._notice_output(example_notice)
                if example_notice
                else None
            ),
            checks=checks,
            fallback_sources={"1info": oneinfo_state.value},
            attempts=tuple(attempts),
            error=self.last_error,
        )

    def discover(self, query: str) -> ItalySourceDiscovery:
        attempts: list[EndpointAttempt] = []
        endpoint_candidates: list[ItalyEndpointCandidate] = []
        notices_by_url: dict[str, ItalyNotice] = {}
        params = {"cerca": query} if query.strip() else None

        for name, url, role in (
            (
                "italy_discover_documents",
                self.documents_url,
                "primary regulated documents search",
            ),
            (
                "italy_discover_press",
                self.press_releases_url,
                "regulated press releases search",
            ),
        ):
            attempt, html = self._request_text(
                name=name,
                url=url,
                params=params,
            )
            attempts.append(attempt)
            if not html:
                continue
            parsed = parse_emarket_html(
                html,
                base_url=attempt.endpoint,
            )
            query_norm = normalize_text(query)
            for notice in parsed.notices:
                haystack = normalize_text(
                    f"{notice.company} {notice.title}"
                )
                if not query_norm or query_norm in haystack:
                    notices_by_url.setdefault(notice.document_url, notice)
            endpoint_candidates.append(
                ItalyEndpointCandidate(
                    url=attempt.endpoint,
                    role=role,
                    format="HTML",
                    pagination="query parameter page (zero-based)",
                    fields=(
                        "published_date",
                        "company",
                        "title",
                        "document_url",
                        "protocol",
                    ),
                    verified=bool(parsed.notices),
                    state=(
                        ConnectorState.READY
                        if parsed.notices
                        else ConnectorState.DEGRADED
                    ),
                    http_status=attempt.http_status,
                )
            )

        oneinfo_attempt, oneinfo_html = self._request_text(
            name="italy_discover_1info",
            url=self.oneinfo_url,
            check_robots=False,
        )
        attempts.append(oneinfo_attempt)
        endpoint_candidates.append(
            ItalyEndpointCandidate(
                url=oneinfo_attempt.endpoint,
                role="secondary authorized storage fallback",
                format="JavaScript application",
                pagination=None,
                fields=(),
                verified=False,
                state=ConnectorState.STUB,
                http_status=oneinfo_attempt.http_status,
            )
        )
        borsa_url = (
            "https://www.borsaitaliana.it/borsa/azioni/"
            "listino-a-z.html?initial=A&lang=it"
        )
        borsa_attempt, borsa_html = self._request_text(
            name="italy_discover_borsa",
            url=borsa_url,
        )
        attempts.append(borsa_attempt)
        endpoint_candidates.append(
            ItalyEndpointCandidate(
                url=borsa_attempt.endpoint,
                role="Euronext Growth Milan issuer/company discovery",
                format="HTML",
                pagination="alphabetical initial query parameter",
                fields=("issuer", "company page"),
                verified=bool(borsa_html),
                state=(
                    ConnectorState.DEGRADED
                    if borsa_html
                    else ConnectorState.UNAVAILABLE
                ),
                http_status=borsa_attempt.http_status,
            )
        )
        return ItalySourceDiscovery(
            source=self.source_name,
            query=query,
            candidates=tuple(endpoint_candidates),
            notices=tuple(notices_by_url.values()),
            attempts=tuple(attempts),
        )
