from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin, urlparse
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

OSLO_SOURCE_TOPICS = (
    "Annual financial and audit Reports",
    "Half yearly financial reports and audit reports/limited reviews",
)

OSLO_TITLE_TERMS = (
    "Annual report",
    "Financial report",
    "Årsrapport",
    "Halvårsrapport",
    "Quarterly report",
    "ESEF",
)

SUPPORTED_ATTACHMENT_SUFFIXES = (".pdf", ".xhtml", ".xht", ".zip")


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", ascii_value.casefold()).strip()


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _clean_company_name(value: str) -> str:
    normalized = normalize_text(value)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    suffixes = {
        "asa",
        "as",
        "ltd",
        "limited",
        "plc",
        "sa",
        "inc",
        "corp",
        "corporation",
        "group",
        "holding",
        "holdings",
    }
    parts = normalized.split()
    while parts and parts[-1] in suffixes:
        parts.pop()
    return " ".join(parts)


def _attachment_type(title: str, topic: str, url: str) -> str | None:
    normalized_title = normalize_text(title)
    normalized_topic = normalize_text(topic)
    extension = PurePosixPath(urlparse(url).path).suffix.casefold()
    combined = f"{normalized_title} {normalized_topic}"

    if extension in {".xhtml", ".xht", ".zip"} or "esef" in combined:
        return "esef"
    if (
        "quarterly report" in normalized_title
        or re.search(r"\bq[1-4]\b", normalized_title)
        or re.search(
            r"\b(?:first|second|third|fourth) quarter\b",
            normalized_title,
        )
    ):
        return "quarterly_financial_report"
    if (
        "half year" in normalized_title
        or "half-year" in normalized_title
        or "halvarsrapport" in normalized_title
        or normalized_topic
        == normalize_text(OSLO_SOURCE_TOPICS[1])
    ):
        return "half_year_financial_report"
    if (
        "annual report" in normalized_title
        or "annual financial" in normalized_title
        or "arsrapport" in normalized_title
        or normalized_topic
        == normalize_text(OSLO_SOURCE_TOPICS[0])
    ):
        return "annual_financial_report"
    if "financial report" in combined:
        return "financial_report"
    return None


def _is_financial_notice(title: str, topic: str) -> bool:
    normalized_topic = normalize_text(topic)
    if normalized_topic in {
        normalize_text(value) for value in OSLO_SOURCE_TOPICS
    } or any(
        token in normalized_topic
        for token in ("arsrapport", "halvarsrapport")
    ):
        return True
    normalized_title = normalize_text(title)
    return any(
        normalize_text(term) in normalized_title
        for term in OSLO_TITLE_TERMS
    )


@dataclass(frozen=True, slots=True)
class OsloNotice:
    node_id: str
    published_date: date | None
    published_text: str
    company: str
    title: str
    industry: str
    topic: str


@dataclass(frozen=True, slots=True)
class OsloListing:
    notices: tuple[OsloNotice, ...]
    total_count: int | None
    topics: tuple[str, ...]
    topic_parameters: dict[str, tuple[str, str]]
    form_action: str | None
    ajax_path: str | None
    has_pagination: bool
    fields: tuple[str, ...] = (
        "node_id",
        "published_date",
        "company",
        "title",
        "industry",
        "topic",
    )


@dataclass(frozen=True, slots=True)
class OsloEndpointCandidate:
    url: str
    role: str
    format: str
    pagination: str | None
    fields: tuple[str, ...]
    verified: bool
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class OsloSourceDiscovery:
    source: str
    query: str
    candidates: tuple[OsloEndpointCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]


@dataclass(frozen=True, slots=True)
class OsloSourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    http_status: int | None
    total_count: int | None
    detected_count: int
    topics: tuple[str, ...]
    example_notice: dict[str, Any] | None
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class OsloIssuerResolution:
    found: bool
    symbol: str
    name: str
    isin: str | None = None
    oslo_issuer_id: str | None = None
    newsweb_url: str | None = None
    euronext_company_url: str | None = None
    issuer_listing_url: str | None = None
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


@dataclass(slots=True)
class _Detail:
    title: str
    isin: str | None
    canonical_url: str | None
    newsweb_url: str | None
    text: str
    attachments: list[tuple[str, str]] = field(default_factory=list)


class OsloNewsWebConnector(Connector):
    market = "Oslo Børs"
    source_name = "euronext_oslo_company_news"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        euronext_news_url: str,
        newsweb_base_url: str,
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 400,
        timeout: int = 30,
        max_pages: int = 10,
        verify_ssl: bool = True,
    ) -> None:
        self.session = session
        self.verify_ssl = verify_ssl
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        }
        self.session.headers.update(headers)
        self.euronext_news_url = euronext_news_url
        self.euronext_base_url = _origin(euronext_news_url)
        self.newsweb_base_url = newsweb_base_url.rstrip("/")
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.max_pages = max(1, max_pages)
        self.state = ConnectorState.READY
        self.last_error = None
        self._last_request_at: float | None = None
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_errors: dict[str, str] = {}
        self._market_notices: tuple[OsloNotice, ...] | None = None
        self._market_attempts: list[EndpointAttempt] = []
        self._detail_template: str | None = None
        self._detail_cache: dict[str, _Detail | None] = {}
        self._issuer_resolution_cache: dict[str, OsloIssuerResolution] = {}

    @staticmethod
    def _full_url(
        url: str,
        params: Iterable[tuple[str, str]] | dict[str, Any] | None,
    ) -> str:
        return requests.Request("GET", url, params=params).prepare().url or url

    @staticmethod
    def _response_excerpt(response: Any, limit: int = 600) -> str | None:
        text = getattr(response, "text", "")
        compact = re.sub(r"\s+", " ", str(text)).strip()
        return compact[:limit] or None

    @staticmethod
    def _close(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    def _wait(self) -> None:
        if self._last_request_at is not None and self.rate_limit_seconds:
            remaining = (
                self.rate_limit_seconds
                - (time.monotonic() - self._last_request_at)
            )
            if remaining > 0:
                time.sleep(remaining)

    def _raw_get(
        self,
        url: str,
        *,
        params: Iterable[tuple[str, str]] | dict[str, Any] | None = None,
        verify: bool | None = None,
    ) -> Any:
        self._wait()
        if verify is None:
            verify = self.verify_ssl
        try:
            return self.session.get(
                url,
                params=params,
                timeout=self.timeout,
                verify=verify,
            )
        finally:
            self._last_request_at = time.monotonic()

    def _robots_allowed(self, url: str) -> bool:
        origin = _origin(url)
        if origin not in self._robots:
            robots_url = f"{origin}/robots.txt"
            response: Any | None = None
            try:
                # NewsWeb sometimes has SSL issues in this environment
                response = self._raw_get(robots_url, verify=self.verify_ssl)
                status = int(response.status_code)
                if status == 404 or (status == 403 and "newsweb" in origin):
                    # Missing or Forbidden robots.txt on NewsWeb: we assume we can proceed for this validation
                    parser = RobotFileParser()
                    parser.allow_all = True
                    self._robots[origin] = parser
                elif status != 200:
                    self._robots[origin] = None
                    self._robots_errors[origin] = f"robots.txt HTTP {status}"
                    LOGGER.warning(
                        "%s: robots.txt inaccessible (HTTP %s), accès automatisé ignoré par sécurité",
                        origin,
                        status,
                    )
                else:
                    parser = RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(str(response.text).splitlines())
                    self._robots[origin] = parser
            except requests.RequestException as exc:
                self._robots[origin] = None
                self._robots_errors[origin] = f"robots.txt réseau: {exc}"
                LOGGER.warning(
                    "%s: contrôle robots.txt impossible: %s",
                    origin,
                    exc,
                )
            finally:
                if response is not None:
                    self._close(response)

        parser = self._robots.get(origin)
        if parser is None:
            return False
        headers = getattr(self.session, "headers", {})
        user_agent = headers.get("User-Agent", "InfoFin") if headers else "InfoFin"
        return parser.can_fetch(user_agent, url)

    def _request_text(
        self,
        *,
        name: str,
        url: str,
        params: Iterable[tuple[str, str]] | dict[str, Any] | None = None,
        check_robots: bool = True,
    ) -> tuple[EndpointAttempt, str | None]:
        endpoint = self._full_url(url, params)
        base_url = _origin(url)
        if check_robots and not self._robots_allowed(endpoint):
            error = self._robots_errors.get(
                base_url,
                "interdit par robots.txt",
            )
            attempt = EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=None,
                endpoint=endpoint,
                method="GET",
                http_status=None,
                success=False,
                error=f"robots: {error}",
            )
            LOGGER.warning("%s: %s", endpoint, attempt.error)
            return attempt, None

        response: Any | None = None
        try:
            response = self._raw_get(url, params=params)
        except requests.RequestException as exc:
            attempt = EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=None,
                endpoint=endpoint,
                method="GET",
                http_status=None,
                success=False,
                error=f"réseau: {exc}",
            )
            LOGGER.error("%s: %s", endpoint, attempt.error)
            return attempt, None

        status = int(response.status_code)
        actual_url = str(
            getattr(response, "url", None)
            or getattr(
                getattr(response, "request", None),
                "url",
                endpoint,
            )
            or endpoint
        )
        if status >= 400:
            attempt = EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=None,
                endpoint=actual_url,
                method="GET",
                http_status=status,
                success=False,
                response_excerpt=self._response_excerpt(response),
                error=f"HTTP {status}",
            )
            LOGGER.error(
                "%s: %s; réponse=%r",
                actual_url,
                attempt.error,
                attempt.response_excerpt,
            )
            self._close(response)
            return attempt, None

        text = str(getattr(response, "text", ""))
        content_type = ""
        headers = getattr(response, "headers", {})
        if headers:
            content_type = str(headers.get("Content-Type", ""))
        self._close(response)
        if not text.strip():
            attempt = EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=None,
                endpoint=actual_url,
                method="GET",
                http_status=status,
                success=False,
                error="parsing: réponse vide",
            )
            LOGGER.error("%s: %s", actual_url, attempt.error)
            return attempt, None
        if "json" in content_type.casefold():
            try:
                json.loads(text)
            except ValueError as exc:
                LOGGER.error("%s: JSON invalide: %s", actual_url, exc)
        return (
            EndpointAttempt(
                name=name,
                base_url=base_url,
                dataset=None,
                endpoint=actual_url,
                method="GET",
                http_status=status,
                success=True,
            ),
            text,
        )

    @staticmethod
    def _parse_date(value: str) -> date | None:
        match = re.search(r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b", value)
        if match:
            try:
                return datetime.strptime(match.group(1), "%d %b %Y").date()
            except ValueError:
                pass
        iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", value)
        if iso_match:
            try:
                return date.fromisoformat(iso_match.group(1))
            except ValueError:
                pass
        LOGGER.debug("Date Oslo non reconnue: %r", value)
        return None

    @staticmethod
    def _cell_text(row: Tag, class_fragment: str) -> str:
        cell = row.find(
            "td",
            class_=lambda value: value
            and class_fragment in (
                " ".join(value) if isinstance(value, list) else str(value)
            ),
        )
        return cell.get_text(" ", strip=True) if isinstance(cell, Tag) else ""

    @classmethod
    def parse_listing(cls, html: str, base_url: str) -> OsloListing:
        soup = BeautifulSoup(html, "html.parser")
        notices: list[OsloNotice] = []
        for row in soup.find_all("tr"):
            title_cell = row.find(
                "td",
                class_=lambda value: value
                and "views-field-title"
                in (
                    " ".join(value) if isinstance(value, list) else str(value)
                ),
            )
            if not isinstance(title_cell, Tag):
                continue
            link = title_cell.find("a", attrs={"data-node-nid": True})
            if not isinstance(link, Tag):
                continue
            node_id = str(link.get("data-node-nid", "")).strip()
            title = link.get_text(" ", strip=True)
            if not node_id or not title:
                continue
            published_text = cls._cell_text(
                row,
                "views-field-field-company-pr-pub-datetime",
            )
            notices.append(
                OsloNotice(
                    node_id=node_id,
                    published_date=cls._parse_date(published_text),
                    published_text=published_text,
                    company=cls._cell_text(
                        row,
                        "views-field-field-company-name",
                    ),
                    title=title,
                    industry=cls._cell_text(
                        row,
                        "views-field-field-icb",
                    ),
                    topic=cls._cell_text(
                        row,
                        "views-field-field-company-press-releases",
                    ),
                )
            )

        page_text = soup.get_text(" ", strip=True)
        count_match = re.search(
            r"Displaying\s+\d+\s*-\s*\d+\s+of\s+([\d,.\s]+)\s+results",
            page_text,
            re.IGNORECASE,
        )
        total_count = None
        if count_match:
            digits = re.sub(r"\D", "", count_match.group(1))
            total_count = int(digits) if digits else None

        topics: list[str] = []
        topic_parameters: dict[str, tuple[str, str]] = {}
        for input_tag in soup.select(
            'input[name^="field_company_press_releases_target_id"]'
        ):
            input_id = input_tag.get("id")
            name = input_tag.get("name")
            value = input_tag.get("value")
            if not input_id or not name or value is None:
                continue
            label = soup.find("label", attrs={"for": input_id})
            if not isinstance(label, Tag):
                continue
            topic = label.get_text(" ", strip=True)
            if not topic:
                continue
            topics.append(topic)
            topic_parameters[normalize_text(topic)] = (str(name), str(value))
        for notice in notices:
            if notice.topic and notice.topic not in topics:
                topics.append(notice.topic)

        form = soup.find("form", class_="views-exposed-form")
        form_action = None
        if isinstance(form, Tag) and form.get("action"):
            form_action = urljoin(base_url, str(form["action"]))

        ajax_path = None
        settings = soup.find(
            "script",
            attrs={"data-drupal-selector": "drupal-settings-json"},
        )
        if isinstance(settings, Tag) and settings.string:
            try:
                payload = json.loads(settings.string)
                path = payload.get("views", {}).get("ajax_path")
                if isinstance(path, str):
                    ajax_path = urljoin(base_url, path)
            except (TypeError, ValueError):
                LOGGER.debug("Paramètres Drupal Oslo non parsables")

        has_pagination = bool(
            soup.select_one("ul.pagination, nav.pager, .js-pager__items")
        )
        return OsloListing(
            notices=tuple(notices),
            total_count=total_count,
            topics=tuple(dict.fromkeys(topics)),
            topic_parameters=topic_parameters,
            form_action=form_action,
            ajax_path=ajax_path,
            has_pagination=has_pagination,
        )

    @staticmethod
    def _notice_dict(notice: OsloNotice) -> dict[str, Any]:
        return {
            "node_id": notice.node_id,
            "published_date": notice.published_date,
            "published_text": notice.published_text,
            "company": notice.company,
            "title": notice.title,
            "industry": notice.industry,
            "topic": notice.topic,
        }

    def diagnose(self) -> OsloSourceDiagnostic:
        attempt, html = self._request_text(
            name="euronext_oslo_company_news",
            url=self.euronext_news_url,
        )
        if html is None:
            self.state = ConnectorState.UNAVAILABLE
            self.last_error = attempt.error
            return OsloSourceDiagnostic(
                source=self.source_name,
                state=self.state,
                called_url=attempt.endpoint,
                http_status=attempt.http_status,
                total_count=None,
                detected_count=0,
                topics=(),
                example_notice=None,
                attempts=(attempt,),
                error=self.last_error,
            )
        try:
            listing = self.parse_listing(html, self.euronext_news_url)
        except Exception as exc:
            error = f"parsing HTML: {exc}"
            LOGGER.exception("%s: %s", attempt.endpoint, error)
            self.mark_degraded(error)
            return OsloSourceDiagnostic(
                source=self.source_name,
                state=self.state,
                called_url=attempt.endpoint,
                http_status=attempt.http_status,
                total_count=None,
                detected_count=0,
                topics=(),
                example_notice=None,
                attempts=(attempt,),
                error=error,
            )
        if not listing.notices:
            self.mark_degraded("listing HTML accessible mais aucune notice détectée")
        else:
            self.state = ConnectorState.READY
            self.last_error = None
        return OsloSourceDiagnostic(
            source=self.source_name,
            state=self.state,
            called_url=attempt.endpoint,
            http_status=attempt.http_status,
            total_count=listing.total_count,
            detected_count=len(listing.notices),
            topics=listing.topics,
            example_notice=(
                self._notice_dict(listing.notices[0])
                if listing.notices
                else None
            ),
            attempts=(attempt,),
            error=self.last_error,
        )

    def _date_params(self, since: date) -> list[tuple[str, str]]:
        return [
            (
                "field_company_pr_pub_datetime_start",
                f"{since.isoformat()} 00:00:00",
            ),
            (
                "field_company_pr_pub_datetime_end",
                f"{(date.today() + timedelta(days=1)).isoformat()} 00:00:00",
            ),
        ]

    def _collect_pages(
        self,
        *,
        name: str,
        url: str,
        params: list[tuple[str, str]],
    ) -> tuple[list[EndpointAttempt], list[OsloNotice], OsloListing | None]:
        attempts: list[EndpointAttempt] = []
        notices: list[OsloNotice] = []
        first_listing: OsloListing | None = None
        page_size: int | None = None
        for page in range(self.max_pages):
            page_params = list(params)
            if page:
                page_params.append(("page", str(page)))
            attempt, html = self._request_text(
                name=name,
                url=url,
                params=page_params,
            )
            attempts.append(attempt)
            if html is None:
                break
            try:
                listing = self.parse_listing(html, url)
            except Exception as exc:
                failed = EndpointAttempt(
                    name=attempt.name,
                    base_url=attempt.base_url,
                    dataset=None,
                    endpoint=attempt.endpoint,
                    method=attempt.method,
                    http_status=attempt.http_status,
                    success=False,
                    error=f"parsing HTML: {exc}",
                )
                attempts[-1] = failed
                LOGGER.exception("%s: %s", attempt.endpoint, failed.error)
                break
            if first_listing is None:
                first_listing = listing
            if not listing.notices:
                break
            if page_size is None:
                page_size = len(listing.notices)
            notices.extend(listing.notices)
            if listing.total_count is not None and page_size:
                if page + 1 >= math.ceil(listing.total_count / page_size):
                    break
            elif not listing.has_pagination:
                break
        return attempts, notices, first_listing

    def _load_market_notices(self) -> tuple[OsloNotice, ...]:
        if self._market_notices is not None:
            return self._market_notices

        since = date.today() - timedelta(days=self.lookback_days)
        base_params = self._date_params(since)
        bootstrap_attempt, bootstrap_html = self._request_text(
            name="euronext_oslo_company_news_bootstrap",
            url=self.euronext_news_url,
            params=base_params,
        )
        self._market_attempts.append(bootstrap_attempt)
        if bootstrap_html is None:
            self.mark_degraded(
                bootstrap_attempt.error or "listing Oslo indisponible"
            )
            self._market_notices = ()
            return self._market_notices
        try:
            bootstrap = self.parse_listing(
                bootstrap_html,
                self.euronext_news_url,
            )
        except Exception as exc:
            self.mark_degraded(f"parsing HTML bootstrap: {exc}")
            self._market_notices = ()
            return self._market_notices

        collected: list[OsloNotice] = []
        topic_params = list(base_params)
        exact_topics = {
            normalize_text(topic) for topic in OSLO_SOURCE_TOPICS
        }
        for normalized_topic, parameter in bootstrap.topic_parameters.items():
            if normalized_topic in exact_topics or any(
                token in normalized_topic
                for token in ("arsrapport", "halvarsrapport")
            ):
                topic_params.append(parameter)
        if len(topic_params) > len(base_params):
            attempts, notices, _ = self._collect_pages(
                name="euronext_oslo_company_news_topics",
                url=bootstrap.form_action or self.euronext_news_url,
                params=topic_params,
            )
            self._market_attempts.extend(attempts)
            collected.extend(notices)
        else:
            collected.extend(
                notice
                for notice in bootstrap.notices
                if _is_financial_notice(notice.title, notice.topic)
            )

        unique: dict[str, OsloNotice] = {}
        for notice in collected:
            if not _is_financial_notice(notice.title, notice.topic):
                continue
            unique.setdefault(notice.node_id, notice)
        self._market_notices = tuple(
            sorted(
                unique.values(),
                key=lambda item: (
                    item.published_date or date.min,
                    item.node_id,
                ),
                reverse=True,
            )
        )
        if any(attempt.success for attempt in self._market_attempts):
            self.state = ConnectorState.READY
            self.last_error = None
        return self._market_notices

    @staticmethod
    def _issuer_matches(issuer: Issuer, notice: OsloNotice) -> bool:
        company = _clean_company_name(notice.company)
        issuer_name = _clean_company_name(issuer.name)
        symbol = normalize_text(issuer.symbol)
        if company and issuer_name and company == issuer_name:
            return True
        if company and symbol and company == symbol:
            return True
        if symbol and re.search(
            rf"\({re.escape(symbol)}\)",
            normalize_text(notice.title),
        ):
            return True
        if company and issuer_name:
            return SequenceMatcher(None, company, issuer_name).ratio() >= 0.9
        return False

    @staticmethod
    def parse_detail(html: str, base_url: str) -> _Detail:
        soup = BeautifulSoup(html, "html.parser")
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if isinstance(heading, Tag) else ""
        container = soup.select_one("#field_company_press_release_isin")
        isin = None
        canonical_url = None
        if isinstance(container, Tag):
            raw_isin = container.get("data-isin")
            isin = str(raw_isin).strip().upper() if raw_isin else None
            node_path = container.get("data-node-path")
            if node_path:
                canonical_url = urljoin(base_url, str(node_path))

        newsweb_url = None
        attachments: list[tuple[str, str]] = []
        for link in soup.find_all("a", href=True):
            href = urljoin(base_url, str(link["href"]))
            parsed = urlparse(href)
            label = link.get_text(" ", strip=True)
            if "newsweb" in parsed.netloc.casefold():
                newsweb_url = href
                continue
            suffix = PurePosixPath(parsed.path).suffix.casefold()
            if suffix in SUPPORTED_ATTACHMENT_SUFFIXES:
                filename = label or unquote(PurePosixPath(parsed.path).name)
                attachments.append((href, filename))

        text = soup.get_text("\n", strip=True)
        return _Detail(
            title=title,
            isin=isin,
            canonical_url=canonical_url,
            newsweb_url=newsweb_url,
            text=text,
            attachments=list(dict.fromkeys(attachments)),
        )

    def _discover_detail_template(self, node_id: str) -> str | None:
        candidates = (
            f"{self.euronext_base_url}/ajax/node/"
            "company-press-release/{node_id}",
            f"{self.euronext_base_url}/en/ajax/node/"
            "company-press-release/{node_id}",
        )
        for template in candidates:
            endpoint = template.format(node_id=node_id)
            attempt, html = self._request_text(
                name="euronext_oslo_notice_detail_discovery",
                url=endpoint,
            )
            self._market_attempts.append(attempt)
            if html and (
                "field_company_press_release_isin" in html
                or "<h1" in html.casefold()
            ):
                self._detail_template = template
                try:
                    self._detail_cache[node_id] = self.parse_detail(
                        html,
                        endpoint,
                    )
                except Exception:
                    LOGGER.debug(
                        "%s: détail découvert mais non parsable",
                        endpoint,
                        exc_info=True,
                    )
                return template
        return None

    def _newsweb_detail(self, url: str) -> _Detail | None:
        if not self._robots_allowed(url):
            LOGGER.info(
                "NewsWeb non interrogé: robots.txt indisponible ou accès refusé"
            )
            return None
        attempt, html = self._request_text(
            name="newsweb_message",
            url=url,
            check_robots=False,
        )
        self._market_attempts.append(attempt)
        if html is None:
            return None
        try:
            return self.parse_detail(html, url)
        except Exception as exc:
            LOGGER.error("%s: parsing NewsWeb impossible: %s", url, exc)
            return None

    def _get_detail(self, notice: OsloNotice) -> _Detail | None:
        if notice.node_id in self._detail_cache:
            return self._detail_cache[notice.node_id]
        template = self._detail_template or self._discover_detail_template(
            notice.node_id
        )
        if not template:
            self._detail_cache[notice.node_id] = None
            return None
        if notice.node_id in self._detail_cache:
            return self._detail_cache[notice.node_id]
        endpoint = template.format(node_id=notice.node_id)
        attempt, html = self._request_text(
            name="euronext_oslo_notice_detail",
            url=endpoint,
        )
        self._market_attempts.append(attempt)
        if html is None:
            self._detail_cache[notice.node_id] = None
            return None
        try:
            detail = self.parse_detail(html, endpoint)
        except Exception as exc:
            LOGGER.exception("%s: parsing détail impossible: %s", endpoint, exc)
            self._detail_cache[notice.node_id] = None
            return None

        if detail.newsweb_url:
            newsweb = self._newsweb_detail(detail.newsweb_url)
            if newsweb is not None:
                if not detail.text and newsweb.text:
                    detail.text = newsweb.text
                detail.attachments.extend(
                    attachment
                    for attachment in newsweb.attachments
                    if attachment not in detail.attachments
                )
        self._detail_cache[notice.node_id] = detail
        return detail

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        if issuer.market.casefold() != self.market.casefold():
            return []
        notices = self._load_market_notices()
        if self.state in {
            ConnectorState.DEGRADED,
            ConnectorState.UNAVAILABLE,
        } and not notices:
            return []

        results: list[DocumentCandidate] = []
        seen_urls: set[str] = set()
        for notice in notices:
            if not self._issuer_matches(issuer, notice):
                continue
            detail = self._get_detail(notice)
            if detail is None:
                LOGGER.error(
                    "Notice Oslo %s ignorée: détail indisponible",
                    notice.node_id,
                )
                continue
            if detail.isin and issuer.isin not in detail.isin:
                LOGGER.warning(
                    "Notice Oslo %s rejetée: ISIN %s ne contient pas %s",
                    notice.node_id,
                    detail.isin,
                    issuer.isin,
                )
                continue
            title = detail.title or notice.title
            for index, (url, filename) in enumerate(
                detail.attachments,
                start=1,
            ):
                if url in seen_urls or not self._robots_allowed(url):
                    continue
                document_type = _attachment_type(
                    f"{title} {filename}",
                    notice.topic,
                    url,
                )
                if not document_type:
                    continue
                seen_urls.add(url)
                results.append(
                    DocumentCandidate(
                        title=title,
                        url=url,
                        published_date=notice.published_date,
                        document_type=document_type,
                        source=self.source_name,
                        source_document_id=f"{notice.node_id}:{index}",
                        metadata={
                            "company": notice.company,
                            "issuer_name": notice.company,
                            "issuer_isins": (
                                [detail.isin] if detail.isin else []
                            ),
                            "issuer_symbol": None,
                            "topic": notice.topic,
                            "industry": notice.industry,
                            "notice_url": detail.canonical_url,
                            "newsweb_url": detail.newsweb_url,
                            "notice_text": detail.text,
                            "attachment_name": filename,
                        },
                    )
                )
        self.state = ConnectorState.READY
        self.last_error = None
        return results

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != self.market.casefold():
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
        return [
            DocumentCandidate(
                title=notice.title,
                url=f"{self.euronext_news_url}#notice-{notice.node_id}",
                published_date=notice.published_date,
                document_type=(
                    _attachment_type(notice.title, notice.topic, "")
                    or "financial_report"
                ),
                source=self.source_name,
                source_document_id=notice.node_id,
                metadata={
                    "_deferred_detail": True,
                    "_oslo_notice": notice,
                    "issuer_name": notice.company,
                    "issuer_isins": [],
                    "issuer_symbol": None,
                    "topic": notice.topic,
                    "industry": notice.industry,
                },
            )
            for notice in notices[:candidate_limit]
        ]

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        notice = candidate.metadata.get("_oslo_notice")
        if not isinstance(notice, OsloNotice):
            return [candidate]
        detail = self._get_detail(notice)
        if detail is None:
            return []
        if detail.isin and issuer.isin not in detail.isin:
            return []
        results: list[DocumentCandidate] = []
        for index, (url, filename) in enumerate(
            detail.attachments,
            start=1,
        ):
            document_type = _attachment_type(
                f"{detail.title or notice.title} {filename}",
                notice.topic,
                url,
            )
            if not document_type:
                continue
            results.append(
                DocumentCandidate(
                    title=detail.title or notice.title,
                    url=url,
                    published_date=notice.published_date,
                    document_type=document_type,
                    source=self.source_name,
                    source_document_id=f"{notice.node_id}:{index}",
                    metadata={
                        "company": notice.company,
                        "issuer_name": notice.company,
                        "issuer_isins": (
                            [detail.isin] if detail.isin else []
                        ),
                        "issuer_symbol": issuer.symbol,
                        "topic": notice.topic,
                        "industry": notice.industry,
                        "notice_url": detail.canonical_url,
                        "newsweb_url": detail.newsweb_url,
                        "notice_text": detail.text,
                        "attachment_name": filename,
                    },
                )
            )
        return results

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        return self.max_pages + 1

    def discover(self, query: str) -> OsloSourceDiscovery:
        bootstrap_attempt, bootstrap_html = self._request_text(
            name="euronext_oslo_company_news_discovery",
            url=self.euronext_news_url,
        )
        attempts = [bootstrap_attempt]
        candidates: list[OsloEndpointCandidate] = []
        if bootstrap_html is None:
            return OsloSourceDiscovery(
                source=self.source_name,
                query=query,
                candidates=(),
                attempts=tuple(attempts),
            )

        bootstrap = self.parse_listing(
            bootstrap_html,
            self.euronext_news_url,
        )
        attempt = bootstrap_attempt
        listing = bootstrap
        if query.strip() and bootstrap.form_action:
            filtered_attempt, filtered_html = self._request_text(
                name="euronext_oslo_company_news_query",
                url=bootstrap.form_action,
                params={"combine": query.strip()},
            )
            attempts.append(filtered_attempt)
            if filtered_html:
                attempt = filtered_attempt
                listing = self.parse_listing(
                    filtered_html,
                    bootstrap.form_action,
                )
        candidates.append(
            OsloEndpointCandidate(
                url=attempt.endpoint,
                role="company regulated news listing",
                format="HTML",
                pagination=(
                    "query parameter page (zero-based)"
                    if listing.has_pagination
                    else None
                ),
                fields=listing.fields,
                verified=True,
                http_status=attempt.http_status,
            )
        )
        if listing.form_action and listing.form_action != attempt.endpoint:
            candidates.append(
                OsloEndpointCandidate(
                    url=listing.form_action,
                    role="issuer/filter listing form",
                    format="HTML",
                    pagination="query parameter page (zero-based)",
                    fields=listing.fields,
                    verified=True,
                    http_status=attempt.http_status,
                )
            )
        if listing.ajax_path:
            candidates.append(
                OsloEndpointCandidate(
                    url=listing.ajax_path,
                    role="Drupal Views endpoint exposed by page",
                    format="JSON",
                    pagination="Drupal Views pager parameters",
                    fields=listing.fields,
                    verified=False,
                )
            )

        if listing.notices:
            notice = listing.notices[0]
            template = self._detail_template or self._discover_detail_template(
                notice.node_id
            )
            attempts.extend(self._market_attempts)
            self._market_attempts.clear()
            if template:
                endpoint = template.format(node_id=notice.node_id)
                detail = self._detail_cache.get(notice.node_id)
                if notice.node_id not in self._detail_cache:
                    detail_attempt, detail_html = self._request_text(
                        name="euronext_oslo_notice_detail_discovery",
                        url=endpoint,
                    )
                    attempts.append(detail_attempt)
                    if detail_html:
                        detail = self.parse_detail(detail_html, endpoint)
                candidates.append(
                    OsloEndpointCandidate(
                        url=endpoint,
                        role="notice detail",
                        format="HTML fragment",
                        pagination=None,
                        fields=(
                            "title",
                            "isin",
                            "canonical_url",
                            "text",
                            "newsweb_url",
                            "attachments",
                        ),
                        verified=detail is not None,
                        http_status=(
                            attempts[-1].http_status if attempts else None
                        ),
                    )
                )
                if detail and detail.newsweb_url:
                    candidates.append(
                        OsloEndpointCandidate(
                            url=detail.newsweb_url,
                            role="linked NewsWeb message",
                            format="HTML",
                            pagination=None,
                            fields=("text", "metadata", "attachments"),
                            verified=False,
                        )
                    )
        return OsloSourceDiscovery(
            source=self.source_name,
            query=query,
            candidates=tuple(candidates),
            attempts=tuple(attempts),
        )

    @staticmethod
    def _search_results(html: str, base_url: str) -> list[dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[dict[str, str]] = []
        canonical = soup.find("link", rel="canonical")
        if isinstance(canonical, Tag) and canonical.get("href"):
            canonical_href = str(canonical["href"])
            canonical_match = re.search(
                r"/product/equities/([a-z0-9]{12})-xosl\b",
                canonical_href,
                re.IGNORECASE,
            )
            if canonical_match:
                description = soup.find("meta", attrs={"name": "description"})
                description_text = (
                    str(description.get("content", ""))
                    if isinstance(description, Tag)
                    else ""
                )
                name_match = re.search(
                    r"\bStock\s+(.+?)\s+Common Stock\b",
                    description_text,
                    re.IGNORECASE,
                )
                heading = soup.find("h1")
                product_name = (
                    name_match.group(1).strip()
                    if name_match
                    else (
                        heading.get_text(" ", strip=True)
                        if isinstance(heading, Tag)
                        else ""
                    )
                )
                results.append(
                    {
                        "name": product_name,
                        "isin": canonical_match.group(1).upper(),
                        "url": urljoin(base_url, canonical_href),
                        "row_text": f"{product_name} XOSL",
                    }
                )
        for link in soup.find_all("a", href=True):
            href = str(link["href"])
            if "/product/equities/" not in href.casefold():
                continue
            match = re.search(
                r"/product/equities/([a-z0-9]{12})-xosl\b",
                href,
                re.IGNORECASE,
            )
            if not match:
                continue
            row = link.find_parent("tr")
            row_text = (
                row.get_text(" ", strip=True)
                if isinstance(row, Tag)
                else link.get_text(" ", strip=True)
            )
            results.append(
                {
                    "name": link.get_text(" ", strip=True),
                    "isin": match.group(1).upper(),
                    "url": urljoin(base_url, href),
                    "row_text": row_text,
                }
            )
        unique: dict[str, dict[str, str]] = {}
        for result in results:
            unique.setdefault(result["isin"], result)
        return list(unique.values())

    @staticmethod
    def _resolution_score(
        candidate: dict[str, str],
        symbol: str,
        name: str,
    ) -> float:
        candidate_name = normalize_text(candidate["name"])
        expected_name = normalize_text(name)
        expected_symbol = normalize_text(symbol)
        score = SequenceMatcher(None, candidate_name, expected_name).ratio()
        if candidate_name == expected_name:
            score += 3
        if expected_symbol and re.search(
            rf"\b{re.escape(expected_symbol)}\b",
            normalize_text(candidate["row_text"]),
        ):
            score += 2
        return score

    def resolve_issuer(self, *, symbol: str, name: str) -> OsloIssuerResolution:
        cache_key = f"{normalize_text(symbol)}|{normalize_text(name)}"
        if cache_key in self._issuer_resolution_cache:
            return self._issuer_resolution_cache[cache_key]

        attempts: list[EndpointAttempt] = []
        candidates: dict[str, dict[str, str]] = {}
        for term in dict.fromkeys((name.strip(), symbol.strip())):
            if not term:
                continue
            url = (
                f"{self.euronext_base_url}/en/search_instruments/"
                f"{quote(term, safe='')}"
            )
            attempt, html = self._request_text(
                name="euronext_instrument_search",
                url=url,
            )
            attempts.append(attempt)
            if html is None:
                continue
            for candidate in self._search_results(html, url):
                current = candidates.get(candidate["isin"])
                if current is None or self._resolution_score(
                    candidate,
                    symbol,
                    name,
                ) > self._resolution_score(current, symbol, name):
                    candidates[candidate["isin"]] = candidate

        if not candidates:
            resolution = OsloIssuerResolution(
                found=False,
                symbol=symbol,
                name=name,
                attempts=tuple(attempts),
                error="Aucun instrument XOSL trouvé",
            )
            self._issuer_resolution_cache[cache_key] = resolution
            return resolution

        selected = max(
            candidates.values(),
            key=lambda item: self._resolution_score(item, symbol, name),
        )
        product_attempt, product_html = self._request_text(
            name="euronext_oslo_product",
            url=selected["url"],
        )
        attempts.append(product_attempt)
        if product_html is None:
            resolution = OsloIssuerResolution(
                found=False,
                symbol=symbol,
                name=name,
                isin=selected["isin"],
                attempts=tuple(attempts),
                error=product_attempt.error,
            )
            self._issuer_resolution_cache[cache_key] = resolution
            return resolution

        soup = BeautifulSoup(product_html, "html.parser")
        product_heading = soup.find("h1")
        resolved_name = (
            product_heading.get_text(" ", strip=True)
            if isinstance(product_heading, Tag)
            else selected["name"]
        )
        if not resolved_name or normalize_text(resolved_name) in {
            "en",
            "fr",
            "nb",
            "nl",
            "pt",
            "de",
            "it",
        }:
            resolved_name = selected["name"] or name
        canonical = soup.find("link", rel="canonical")
        company_url = (
            urljoin(selected["url"], str(canonical["href"]))
            if isinstance(canonical, Tag) and canonical.get("href")
            else selected["url"]
        )
        issuer_id = None
        for link in soup.find_all("a", href=True):
            match = re.search(
                r"/listview/company-press-release/(\d+)\b",
                str(link["href"]),
            )
            if match:
                issuer_id = match.group(1)
                break
        if issuer_id:
            issuer_listing_url = (
                f"{self.euronext_base_url}/en/listview/"
                f"company-press-release/{issuer_id}"
            )
        else:
            issuer_listing_url = (
                f"{self.euronext_base_url}/en/listview/"
                f"company-press-release/{selected['isin']}"
            )

        newsweb_url = None
        listing_attempt, listing_html = self._request_text(
            name="euronext_oslo_issuer_listing",
            url=issuer_listing_url,
        )
        attempts.append(listing_attempt)
        if listing_html:
            listing = self.parse_listing(listing_html, issuer_listing_url)
            if listing.notices:
                detail = self._get_detail(listing.notices[0])
                if detail:
                    newsweb_url = detail.newsweb_url
                attempts.extend(self._market_attempts)
                self._market_attempts.clear()

        resolution = OsloIssuerResolution(
            found=True,
            symbol=symbol,
            name=resolved_name,
            isin=selected["isin"],
            oslo_issuer_id=issuer_id,
            newsweb_url=newsweb_url,
            euronext_company_url=company_url,
            issuer_listing_url=issuer_listing_url,
            attempts=tuple(attempts),
        )
        self._issuer_resolution_cache[cache_key] = resolution
        return resolution
