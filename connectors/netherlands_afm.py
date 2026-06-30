from __future__ import annotations

import csv
import io
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

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

NETHERLANDS_MARKET = "Euronext Amsterdam"
FINANCIAL_EXPORT_TYPE = "e8825b05-4004-4301-b736-651e8c61053d"
HOME_MEMBER_STATE_EXPORT_TYPE = "6b365727-6220-452f-83b1-86a179d70d12"
SUPPORTED_SUFFIXES = {".pdf", ".xhtml", ".xht", ".zip", ".xbri"}


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", ascii_value.casefold()).strip()


def clean_company_name(value: str) -> str:
    normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        normalize_text(value),
    ).strip()
    suffixes = {
        "ag",
        "as",
        "asa",
        "bv",
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
        "se",
    }
    parts = normalized.split()
    while parts and parts[-1] in suffixes:
        parts.pop()
    return " ".join(parts)


def _parse_date(value: str) -> date | None:
    compact = " ".join((value or "").split())
    if not compact:
        return None
    for fmt in (
        "%m/%d/%Y %I:%M:%S %p",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(compact, fmt).date()
        except ValueError:
            continue
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", compact)
    if iso_match:
        return date.fromisoformat(iso_match.group(1))
    month_names = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    match = re.search(
        r"\b(\d{1,2})\s+([a-z]{3})\s+(\d{4})\b",
        compact.casefold(),
    )
    if match and match.group(2) in month_names:
        return date(
            int(match.group(3)),
            month_names[match.group(2)],
            int(match.group(1)),
        )
    return None


def classify_afm_document(
    document_type: str,
    filename: str,
) -> str | None:
    combined = normalize_text(f"{document_type} {filename}")
    suffix = PurePosixPath(urlparse(filename).path).suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        return None
    if (
        suffix in {".xhtml", ".xht", ".zip", ".xbri"}
        or "esef" in combined
        or "xhtml" in combined
    ):
        return "esef"
    if any(
        term in combined
        for term in (
            "halfjaarlijkse financiele verslaggeving",
            "half yearly financial report",
            "half-yearly financial report",
            "half-year financial report",
            "semi annual financial report",
            "semi-annual financial report",
            "half year report",
            "half-year report",
        )
    ):
        return "half_year_financial_report"
    if any(
        term in combined
        for term in (
            "jaarlijkse financiele verslaggeving",
            "annual financial report",
            "annual report",
        )
    ):
        return "annual_financial_report"
    if "financial report" in combined:
        return "financial_report"
    return None


@dataclass(frozen=True, slots=True)
class AfmRecord:
    record_id: str | None
    filing_date: date | None
    issuing_institution: str
    reporting_year: str
    document_type: str
    document_type_en: str | None
    filename: str | None
    detail_url: str | None


@dataclass(frozen=True, slots=True)
class AfmDocument:
    document_type: str
    filename: str
    download_url: str


@dataclass(frozen=True, slots=True)
class HomeMemberStateRecord:
    record_id: str | None
    publication_date: date | None
    company: str
    home_member_state: str
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedAfmListing:
    records: tuple[AfmRecord, ...]
    total_count: int | None
    page_size: int
    total_pages: int
    context_item_id: str | None


@dataclass(frozen=True, slots=True)
class NetherlandsEndpointCandidate:
    url: str
    role: str
    format: str
    pagination: str | None
    fields: tuple[str, ...]
    verified: bool
    state: ConnectorState
    http_status: int | None = None
    records_count: int | None = None


@dataclass(frozen=True, slots=True)
class NetherlandsDiscoveryNotice:
    record_id: str | None
    filing_date: date | None
    issuing_institution: str
    reporting_year: str
    document_type: str
    filename: str | None
    detail_url: str | None
    download_url: str | None


@dataclass(frozen=True, slots=True)
class NetherlandsSourceDiscovery:
    source: str
    query: str
    candidates: tuple[NetherlandsEndpointCandidate, ...]
    notices: tuple[NetherlandsDiscoveryNotice, ...]
    attempts: tuple[EndpointAttempt, ...]


@dataclass(frozen=True, slots=True)
class NetherlandsSourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    http_status: int | None
    total_count: int | None
    detected_count: int
    fields: tuple[str, ...]
    example_notice: dict[str, Any] | None
    checks: dict[str, bool]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class NetherlandsIssuerResolution:
    found: bool
    requested_name: str
    matched_name: str | None
    symbol: str
    isin: str | None
    afm_record_id: str | None
    afm_issuer_url: str | None
    afm_detail_url: str | None
    home_member_state: str | None
    match_score: float | None
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


def parse_afm_xml(
    xml_text: str,
    *,
    register_url: str,
) -> list[AfmRecord]:
    root = ET.fromstring(xml_text)
    records: list[AfmRecord] = []
    for item in root.findall(".//vermelding"):
        values = {
            child.tag.rsplit("}", 1)[-1].casefold(): (
                child.text or ""
            ).strip()
            for child in item
        }
        record_id = values.get("id") or None
        institution = values.get("uitgevende-instelling", "")
        if not institution:
            continue
        detail_url = (
            f"{register_url.rstrip('/')}/details?"
            f"{urlencode({'id': record_id})}"
            if record_id
            else None
        )
        records.append(
            AfmRecord(
                record_id=record_id,
                filing_date=_parse_date(values.get("datum", "")),
                issuing_institution=institution,
                reporting_year=values.get("boekjaar", ""),
                document_type=values.get("objecttype", ""),
                document_type_en=values.get("objecttype_eng") or None,
                filename=values.get("filename") or None,
                detail_url=detail_url,
            )
        )
    return records


def parse_afm_csv(
    csv_text: str,
    *,
    register_url: str,
) -> list[AfmRecord]:
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    if not reader.fieldnames:
        return []
    fields = {
        normalize_text(name): name for name in reader.fieldnames if name
    }

    def value(row: dict[str, str], *names: str) -> str:
        for name in names:
            original = fields.get(normalize_text(name))
            if original:
                return (row.get(original) or "").strip()
        return ""

    records: list[AfmRecord] = []
    for row in reader:
        institution = value(
            row,
            "Uitgevende instelling",
            "Issuing institution",
        )
        if not institution:
            continue
        records.append(
            AfmRecord(
                record_id=None,
                filing_date=_parse_date(
                    value(row, "Datum deponering", "Filing date")
                ),
                issuing_institution=institution,
                reporting_year=value(row, "Boekjaar", "Reporting year"),
                document_type=value(row, "Soort", "Document Type"),
                document_type_en=None,
                filename=None,
                detail_url=None,
            )
        )
    return records


def parse_home_member_state_xml(
    xml_text: str,
) -> list[HomeMemberStateRecord]:
    root = ET.fromstring(xml_text)
    records: list[HomeMemberStateRecord] = []
    for item in root.findall(".//vermelding"):
        values = {
            child.tag.rsplit("}", 1)[-1].casefold(): (
                child.text or ""
            ).strip()
            for child in item
        }
        company = values.get("companyname", "")
        state = values.get("countryname", "")
        if company and state:
            records.append(
                HomeMemberStateRecord(
                    record_id=values.get("hmsid") or None,
                    publication_date=_parse_date(
                        values.get("publicationdate", "")
                    ),
                    company=company,
                    home_member_state=state,
                    filename=values.get("filename") or None,
                )
            )
    return records


def parse_afm_listing_html(
    html: str,
    *,
    register_url: str,
) -> ParsedAfmListing:
    soup = BeautifulSoup(html or "", "html.parser")
    records: list[AfmRecord] = []
    for row in soup.select(
        "tr.jq_registers_register-paged-list_results_tr"
    ):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link = cells[0].find("a", href=True)
        detail_url = (
            urljoin(register_url, str(link["href"]))
            if isinstance(link, Tag)
            else None
        )
        record_id = None
        if detail_url:
            match = re.search(r"[?&]id=([^&#]+)", detail_url)
            record_id = match.group(1) if match else None
        records.append(
            AfmRecord(
                record_id=record_id,
                filing_date=_parse_date(cells[0].get_text(" ", strip=True)),
                issuing_institution=cells[1].get_text(" ", strip=True),
                reporting_year=cells[2].get_text(" ", strip=True),
                document_type=cells[3].get_text(" ", strip=True),
                document_type_en=None,
                filename=None,
                detail_url=detail_url,
            )
        )
    count_node = soup.select_one(".cc-em--table__results strong")
    total_count = None
    if count_node:
        digits = re.sub(r"\D", "", count_node.get_text(" ", strip=True))
        total_count = int(digits) if digits else None
    container = soup.select_one("#registers_register-paged-list_div")
    page_size = 50
    context_item_id = None
    if isinstance(container, Tag):
        try:
            page_size = int(container.get("data-page-size") or 50)
        except (TypeError, ValueError):
            page_size = 50
        context_item_id = str(
            container.get("data-context-item-id") or ""
        ).strip() or None
    page_numbers = [
        int(node.get("data-page-number"))
        for node in soup.select("[data-page-number]")
        if str(node.get("data-page-number") or "").isdigit()
    ]
    total_pages = max(page_numbers, default=1)
    return ParsedAfmListing(
        records=tuple(records),
        total_count=total_count,
        page_size=page_size,
        total_pages=total_pages,
        context_item_id=context_item_id,
    )


def parse_afm_detail_html(
    html: str,
    *,
    detail_url: str,
) -> list[AfmDocument]:
    soup = BeautifulSoup(html or "", "html.parser")
    documents: list[AfmDocument] = []
    for row in soup.select(
        'table[data-register-view="register-type-index"] tbody tr'
    ):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        link = cells[-1].find("a", href=True)
        if not isinstance(link, Tag):
            continue
        href = str(link["href"]).strip()
        filename = link.get_text(" ", strip=True)
        if not href or not filename:
            continue
        for mobile_title in cells[0].select(".cc-mobile-title"):
            mobile_title.decompose()
        documents.append(
            AfmDocument(
                document_type=cells[0].get_text(" ", strip=True),
                filename=filename,
                download_url=urljoin(detail_url, href),
            )
        )
    return documents


def issuer_record_match_score(issuer: Issuer, record: AfmRecord) -> float:
    haystack = normalize_text(
        " ".join(
            (
                record.issuing_institution,
                record.filename or "",
                record.document_type,
                record.document_type_en or "",
            )
        )
    )
    if issuer.isin and issuer.isin.casefold() in haystack:
        return 1.0
    target = clean_company_name(issuer.name)
    company = clean_company_name(record.issuing_institution)
    if not target or not company:
        return 0.0
    if target == company:
        return 1.0
    score = SequenceMatcher(None, target, company).ratio()
    if min(len(target), len(company)) >= 4 and (
        target in company or company in target
    ):
        score = max(score, 0.95)
    symbol = re.sub(r"[^a-z0-9]", "", normalize_text(issuer.symbol))
    if len(symbol) >= 3 and re.search(
        rf"\b{re.escape(symbol)}\b",
        haystack,
    ):
        score = max(score, 0.88)
    return score


def match_issuer_record(
    issuer: Issuer,
    record: AfmRecord,
    *,
    threshold: float = 0.84,
) -> bool:
    return issuer_record_match_score(issuer, record) >= threshold


class NetherlandsAfmConnector(Connector):
    market = NETHERLANDS_MARKET
    source_name = "afm"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        register_url: str,
        export_type: str = FINANCIAL_EXPORT_TYPE,
        home_member_state_url: str,
        home_member_state_export_type: str = (
            HOME_MEMBER_STATE_EXPORT_TYPE
        ),
        rate_limit_seconds: float = 0.2,
        lookback_days: int = 900,
        timeout: int = 30,
    ) -> None:
        self.session = session
        self.register_url = register_url.rstrip("/")
        self.export_type = export_type
        self.home_member_state_url = home_member_state_url.rstrip("/")
        self.home_member_state_export_type = (
            home_member_state_export_type
        )
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self._last_request_at: float | None = None
        self._records: tuple[AfmRecord, ...] | None = None
        self._records_source: str | None = None
        self._record_attempts: list[EndpointAttempt] = []
        self._home_records: tuple[HomeMemberStateRecord, ...] | None = None
        self._home_attempts: list[EndpointAttempt] = []
        self._detail_cache: dict[str, tuple[AfmDocument, ...]] = {}

    @property
    def origin(self) -> str:
        parsed = urlparse(self.register_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def export_url(self, fmt: str, *, home: bool = False) -> str:
        export_type = (
            self.home_member_state_export_type
            if home
            else self.export_type
        )
        return (
            f"{self.origin}/export.aspx?"
            f"{urlencode({'format': fmt, 'type': export_type})}"
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
    ) -> Any:
        self._wait()
        try:
            return self.session.get(
                url,
                params=params,
                timeout=self.timeout,
            )
        finally:
            self._last_request_at = time.monotonic()

    @staticmethod
    def _close(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _decode_response(response: Any, *, csv_export: bool) -> str:
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            encodings = (
                ("utf-8-sig", "cp1252", "latin-1")
                if not csv_export
                else ("utf-8-sig", "cp1252", "latin-1")
            )
            for encoding in encodings:
                try:
                    return content.decode(encoding)
                except UnicodeDecodeError:
                    continue
        return str(getattr(response, "text", ""))

    def _request_text(
        self,
        *,
        name: str,
        url: str,
        params: dict[str, Any] | None = None,
        csv_export: bool = False,
    ) -> tuple[EndpointAttempt, str | None]:
        endpoint = requests.Request("GET", url, params=params).prepare().url or url
        response: Any | None = None
        try:
            response = self._raw_get(url, params=params)
        except Exception as exc:
            return (
                EndpointAttempt(
                    name=name,
                    base_url=self.origin,
                    dataset=None,
                    endpoint=endpoint,
                    method="GET",
                    http_status=None,
                    success=False,
                    error=f"réseau: {exc}",
                ),
                None,
            )
        status = int(getattr(response, "status_code", 0))
        actual_url = str(getattr(response, "url", endpoint) or endpoint)
        text = self._decode_response(response, csv_export=csv_export)
        self._close(response)
        if status >= 400:
            return (
                EndpointAttempt(
                    name=name,
                    base_url=self.origin,
                    dataset=None,
                    endpoint=actual_url,
                    method="GET",
                    http_status=status,
                    success=False,
                    error=f"HTTP {status}",
                ),
                None,
            )
        if not text.strip():
            return (
                EndpointAttempt(
                    name=name,
                    base_url=self.origin,
                    dataset=None,
                    endpoint=actual_url,
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
                base_url=self.origin,
                dataset=None,
                endpoint=actual_url,
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
        response: Any | None = None
        try:
            self._wait()
            response = self.session.head(
                url,
                timeout=self.timeout,
                allow_redirects=True,
            )
            self._last_request_at = time.monotonic()
            status = int(getattr(response, "status_code", 0))
            self._close(response)
            return EndpointAttempt(
                name=name,
                base_url=self.origin,
                dataset=None,
                endpoint=url,
                method="HEAD",
                http_status=status,
                success=status < 400,
                error=f"HTTP {status}" if status >= 400 else None,
            )
        except Exception as exc:
            if response is not None:
                self._close(response)
            return EndpointAttempt(
                name=name,
                base_url=self.origin,
                dataset=None,
                endpoint=url,
                method="HEAD",
                http_status=None,
                success=False,
                error=f"réseau: {exc}",
            )

    def _parse_attempt(
        self,
        attempt: EndpointAttempt,
        parser: Any,
        text: str,
    ) -> tuple[EndpointAttempt, list[Any]]:
        try:
            records = parser(text)
        except (ET.ParseError, csv.Error, ValueError) as exc:
            return (
                replace(
                    attempt,
                    success=False,
                    error=f"parsing: {exc}",
                ),
                [],
            )
        return replace(attempt, total_count=len(records)), records

    def _load_records(self) -> tuple[AfmRecord, ...]:
        if self._records is not None:
            return self._records
        attempts: list[EndpointAttempt] = []
        xml_attempt, xml_text = self._request_text(
            name="afm_financial_xml",
            url=self.export_url("xml"),
        )
        if xml_text:
            xml_attempt, records = self._parse_attempt(
                xml_attempt,
                lambda text: parse_afm_xml(
                    text,
                    register_url=self.register_url,
                ),
                xml_text,
            )
            attempts.append(xml_attempt)
            if records:
                self._records = tuple(records)
                self._records_source = "xml"
                self._record_attempts = attempts
                return self._records
        else:
            attempts.append(xml_attempt)

        csv_attempt, csv_text = self._request_text(
            name="afm_financial_csv",
            url=self.export_url("csv"),
            csv_export=True,
        )
        if csv_text:
            csv_attempt, records = self._parse_attempt(
                csv_attempt,
                lambda text: parse_afm_csv(
                    text,
                    register_url=self.register_url,
                ),
                csv_text,
            )
            attempts.append(csv_attempt)
            if records:
                self._records = tuple(records)
                self._records_source = "csv"
                self._record_attempts = attempts
                return self._records
        else:
            attempts.append(csv_attempt)

        listing_attempt, listing_html = self._request_text(
            name="afm_financial_html",
            url=self.register_url,
        )
        attempts.append(listing_attempt)
        records = []
        if listing_html:
            parsed = parse_afm_listing_html(
                listing_html,
                register_url=self.register_url,
            )
            records = list(parsed.records)
            attempts[-1] = replace(
                listing_attempt,
                total_count=parsed.total_count or len(records),
            )
        self._records = tuple(records)
        self._records_source = "html" if records else None
        self._record_attempts = attempts
        if not records:
            self.state = (
                ConnectorState.DEGRADED
                if any(attempt.http_status for attempt in attempts)
                else ConnectorState.UNAVAILABLE
            )
            self.last_error = "Aucun record AFM exploitable"
        return self._records

    def _load_filtered_html_records(
        self,
        issuer: Issuer,
    ) -> tuple[AfmRecord, ...]:
        attempt, html = self._request_text(
            name="afm_financial_html_issuer",
            url=self.register_url,
            params={"KeyWords": issuer.name},
        )
        self._record_attempts.append(attempt)
        if not html:
            return ()
        parsed = parse_afm_listing_html(
            html,
            register_url=self.register_url,
        )
        records = list(parsed.records)
        if (
            parsed.total_pages > 1
            and parsed.context_item_id
            and parsed.page_size
        ):
            endpoint = f"{self.origin}/api/sitecore/RegisterOverview/PagedRegisters"
            for page_number in range(2, parsed.total_pages + 1):
                page_attempt, page_html = self._request_text(
                    name=f"afm_financial_html_page_{page_number}",
                    url=endpoint,
                    params={
                        "contextItemId": parsed.context_item_id,
                        "skip": (page_number - 1) * parsed.page_size,
                        "take": parsed.page_size,
                        "currentPage": page_number,
                        "filter1": "",
                        "filter2": "",
                        "keywords": issuer.name,
                        "dateFrom": "",
                        "dateTill": "",
                    },
                )
                self._record_attempts.append(page_attempt)
                if not page_html:
                    break
                records.extend(
                    parse_afm_listing_html(
                        page_html,
                        register_url=self.register_url,
                    ).records
                )
        return tuple(records)

    def _load_home_records(self) -> tuple[HomeMemberStateRecord, ...]:
        if self._home_records is not None:
            return self._home_records
        attempt, xml_text = self._request_text(
            name="afm_home_member_state_xml",
            url=self.export_url("xml", home=True),
        )
        self._home_attempts.append(attempt)
        if not xml_text:
            self._home_records = ()
            return self._home_records
        attempt, records = self._parse_attempt(
            attempt,
            parse_home_member_state_xml,
            xml_text,
        )
        self._home_attempts[-1] = attempt
        self._home_records = tuple(records)
        return self._home_records

    def _home_member_state(self, issuer: Issuer) -> str | None:
        target = clean_company_name(issuer.name)
        best: tuple[float, HomeMemberStateRecord] | None = None
        for record in self._load_home_records():
            company = clean_company_name(record.company)
            if not target or not company:
                continue
            score = (
                1.0
                if target == company
                else SequenceMatcher(None, target, company).ratio()
            )
            if min(len(target), len(company)) >= 4 and (
                target in company or company in target
            ):
                score = max(score, 0.95)
            if best is None or score > best[0]:
                best = (score, record)
        return best[1].home_member_state if best and best[0] >= 0.84 else None

    def _detail_documents(
        self,
        record: AfmRecord,
    ) -> tuple[AfmDocument, ...]:
        if not record.detail_url:
            return ()
        if record.detail_url in self._detail_cache:
            return self._detail_cache[record.detail_url]
        attempt, html = self._request_text(
            name=f"afm_detail_{record.record_id or 'unknown'}",
            url=record.detail_url,
        )
        self._record_attempts.append(attempt)
        documents = (
            tuple(
                parse_afm_detail_html(
                    html,
                    detail_url=record.detail_url,
                )
            )
            if html
            else ()
        )
        self._detail_cache[record.detail_url] = documents
        return documents

    def _matching_records(self, issuer: Issuer) -> list[AfmRecord]:
        records = list(self._load_records())
        if self._records_source in {"csv", "html"}:
            filtered = self._load_filtered_html_records(issuer)
            if filtered:
                records = list(filtered)
        cutoff = date.today() - timedelta(days=self.lookback_days)
        return [
            record
            for record in records
            if match_issuer_record(issuer, record)
            and (
                record.filing_date is None
                or record.filing_date >= cutoff
            )
        ]

    def resolve_issuer(
        self,
        *,
        symbol: str,
        name: str,
        isin: str | None = None,
    ) -> NetherlandsIssuerResolution:
        issuer = Issuer(
            name=name,
            isin=isin or "",
            symbol=symbol,
            market=NETHERLANDS_MARKET,
        )
        records = self._matching_records(issuer)
        ranked = sorted(
            (
                (issuer_record_match_score(issuer, record), record)
                for record in records
            ),
            key=lambda item: (
                item[0],
                item[1].filing_date or date.min,
            ),
            reverse=True,
        )
        if not ranked:
            return NetherlandsIssuerResolution(
                found=False,
                requested_name=name,
                matched_name=None,
                symbol=symbol,
                isin=isin,
                afm_record_id=None,
                afm_issuer_url=None,
                afm_detail_url=None,
                home_member_state=self._home_member_state(issuer),
                match_score=None,
                attempts=tuple(
                    self._record_attempts + self._home_attempts
                ),
                error="émetteur absent du registre AFM financier",
            )
        score, record = ranked[0]
        home_state = self._home_member_state(issuer)
        return NetherlandsIssuerResolution(
            found=True,
            requested_name=name,
            matched_name=record.issuing_institution,
            symbol=symbol,
            isin=isin,
            afm_record_id=record.record_id,
            afm_issuer_url=(
                f"{self.register_url}?KeyWords={quote(record.issuing_institution)}"
            ),
            afm_detail_url=record.detail_url,
            home_member_state=home_state or "Netherlands",
            match_score=score,
            attempts=tuple(self._record_attempts + self._home_attempts),
        )

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        if issuer.market.casefold() != NETHERLANDS_MARKET.casefold():
            return []
        records = self._matching_records(issuer)
        home_state = self._home_member_state(issuer)
        if home_state and normalize_text(home_state) not in {
            "netherlands",
            "nederland",
            "the netherlands",
        }:
            LOGGER.info(
                "%s ignoré: home member state AFM=%s",
                issuer.name,
                home_state,
            )
            return []

        candidates: list[DocumentCandidate] = []
        seen_urls: set[str] = set()
        matched_without_download = False
        for record in records:
            documents = self._detail_documents(record)
            if not documents:
                matched_without_download = True
                continue
            for document in documents:
                if document.download_url in seen_urls:
                    continue
                document_type = classify_afm_document(
                    document.document_type
                    or record.document_type_en
                    or record.document_type,
                    document.filename,
                )
                if not document_type:
                    continue
                seen_urls.add(document.download_url)
                candidates.append(
                    DocumentCandidate(
                        title=(
                            f"{record.issuing_institution} - "
                            f"{document.document_type} "
                            f"{record.reporting_year}"
                        ).strip(),
                        url=document.download_url,
                        published_date=record.filing_date,
                        document_type=document_type,
                        source=self.source_name,
                        source_document_id=record.record_id,
                        metadata={
                            "issuing_institution": (
                                record.issuing_institution
                            ),
                            "reporting_year": record.reporting_year,
                            "document_type_afm": document.document_type,
                            "filename": document.filename,
                            "detail_url": record.detail_url,
                            "afm_record_id": record.record_id,
                            "afm_issuer_url": (
                                f"{self.register_url}?KeyWords="
                                f"{quote(record.issuing_institution)}"
                            ),
                            "home_member_state": (
                                home_state or "Netherlands"
                            ),
                            "issuer_isins": [],
                            "issuer_name": record.issuing_institution,
                            "issuer_symbol": None,
                        },
                    )
                )
        if candidates:
            self.state = ConnectorState.READY
            self.last_error = None
        elif matched_without_download:
            self.mark_degraded(
                "Notices AFM trouvées mais documents non téléchargeables "
                "automatiquement"
            )
        return candidates

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != NETHERLANDS_MARKET.casefold():
            return []
        cutoff = since or (date.today() - timedelta(days=7))
        candidate_limit = max(1, limit or 1000)
        records = [
            record
            for record in self._load_records()
            if record.detail_url
            and (
                record.filing_date is None
                or record.filing_date >= cutoff
            )
        ]
        records.sort(
            key=lambda record: (
                record.filing_date or date.min,
                record.record_id or "",
            ),
            reverse=True,
        )
        self._scanned_notices = len(records)
        return [
            DocumentCandidate(
                title=(
                    f"{record.issuing_institution} - "
                    f"{record.document_type_en or record.document_type} "
                    f"{record.reporting_year}"
                ).strip(),
                url=record.detail_url or self.register_url,
                published_date=record.filing_date,
                document_type=(
                    classify_afm_document(
                        record.document_type_en or record.document_type,
                        record.filename,
                    )
                    or "financial_report"
                ),
                source=self.source_name,
                source_document_id=record.record_id,
                metadata={
                    "_deferred_detail": True,
                    "_afm_record": record,
                    "issuer_isins": [],
                    "issuer_name": record.issuing_institution,
                    "issuer_symbol": None,
                    "detail_url": record.detail_url,
                    "afm_record_id": record.record_id,
                },
            )
            for record in records[:candidate_limit]
        ]

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        record = candidate.metadata.get("_afm_record")
        if not isinstance(record, AfmRecord):
            return [candidate]
        home_state = self._home_member_state(issuer)
        if home_state and normalize_text(home_state) not in {
            "netherlands",
            "nederland",
            "the netherlands",
        }:
            return []
        candidates: list[DocumentCandidate] = []
        for document in self._detail_documents(record):
            document_type = classify_afm_document(
                document.document_type
                or record.document_type_en
                or record.document_type,
                document.filename,
            )
            if not document_type:
                continue
            candidates.append(
                DocumentCandidate(
                    title=(
                        f"{record.issuing_institution} - "
                        f"{document.document_type} {record.reporting_year}"
                    ).strip(),
                    url=document.download_url,
                    published_date=record.filing_date,
                    document_type=document_type,
                    source=self.source_name,
                    source_document_id=record.record_id,
                    metadata={
                        "issuing_institution": record.issuing_institution,
                        "reporting_year": record.reporting_year,
                        "document_type_afm": document.document_type,
                        "filename": document.filename,
                        "detail_url": record.detail_url,
                        "afm_record_id": record.record_id,
                        "afm_issuer_url": (
                            f"{self.register_url}?KeyWords="
                            f"{quote(record.issuing_institution)}"
                        ),
                        "home_member_state": home_state or "Netherlands",
                        "issuer_isins": [],
                        "issuer_name": record.issuing_institution,
                        "issuer_symbol": None,
                    },
                )
            )
        return candidates

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        return 3

    @staticmethod
    def _record_output(record: AfmRecord) -> dict[str, Any]:
        return {
            "record_id": record.record_id,
            "filing_date": (
                record.filing_date.isoformat()
                if record.filing_date
                else None
            ),
            "issuing_institution": record.issuing_institution,
            "reporting_year": record.reporting_year,
            "document_type": record.document_type,
            "document_type_en": record.document_type_en,
            "filename": record.filename,
            "detail_url": record.detail_url,
        }

    def diagnose(self) -> NetherlandsSourceDiagnostic:
        attempts: list[EndpointAttempt] = []
        page_attempt, page_html = self._request_text(
            name="afm_financial_page",
            url=self.register_url,
        )
        attempts.append(page_attempt)

        csv_attempt, csv_text = self._request_text(
            name="afm_financial_csv",
            url=self.export_url("csv"),
            csv_export=True,
        )
        csv_records: list[AfmRecord] = []
        if csv_text:
            csv_attempt, parsed = self._parse_attempt(
                csv_attempt,
                lambda text: parse_afm_csv(
                    text,
                    register_url=self.register_url,
                ),
                csv_text,
            )
            csv_records = list(parsed)
        attempts.append(csv_attempt)

        xml_attempt, xml_text = self._request_text(
            name="afm_financial_xml",
            url=self.export_url("xml"),
        )
        xml_records: list[AfmRecord] = []
        if xml_text:
            xml_attempt, parsed = self._parse_attempt(
                xml_attempt,
                lambda text: parse_afm_xml(
                    text,
                    register_url=self.register_url,
                ),
                xml_text,
            )
            xml_records = list(parsed)
        attempts.append(xml_attempt)

        home_page_attempt, home_page_html = self._request_text(
            name="afm_home_member_state_page",
            url=self.home_member_state_url,
        )
        attempts.append(home_page_attempt)
        home_xml_attempt, home_xml_text = self._request_text(
            name="afm_home_member_state_xml",
            url=self.export_url("xml", home=True),
        )
        home_records: list[HomeMemberStateRecord] = []
        if home_xml_text:
            home_xml_attempt, parsed = self._parse_attempt(
                home_xml_attempt,
                parse_home_member_state_xml,
                home_xml_text,
            )
            home_records = list(parsed)
        attempts.append(home_xml_attempt)

        html_records: list[AfmRecord] = []
        if page_html:
            parsed_listing = parse_afm_listing_html(
                page_html,
                register_url=self.register_url,
            )
            html_records = list(parsed_listing.records)
            attempts[0] = replace(
                page_attempt,
                total_count=parsed_listing.total_count,
            )
        records = xml_records or csv_records or html_records
        example = records[0] if records else None
        detail_success = False
        download_success = False
        example_documents: list[AfmDocument] = []
        if example and example.detail_url:
            detail_attempt, detail_html = self._request_text(
                name="afm_example_detail",
                url=example.detail_url,
            )
            attempts.append(detail_attempt)
            detail_success = bool(detail_html and detail_attempt.success)
            if detail_html:
                example_documents = parse_afm_detail_html(
                    detail_html,
                    detail_url=example.detail_url,
                )
            if example_documents:
                head_attempt = self._request_head(
                    name="afm_example_document",
                    url=example_documents[0].download_url,
                )
                attempts.append(head_attempt)
                download_success = head_attempt.success

        checks = {
            "financial_page": page_attempt.success,
            "csv_export": bool(csv_records),
            "xml_export": bool(xml_records),
            "home_member_state_page": bool(
                home_page_html and home_page_attempt.success
            ),
            "home_member_state_export": bool(home_records),
            "real_records": bool(records),
            "detail_page": detail_success,
            "automatic_download": download_success,
        }
        if not any(
            (
                checks["financial_page"],
                checks["csv_export"],
                checks["xml_export"],
            )
        ):
            state = ConnectorState.UNAVAILABLE
        elif checks["real_records"] and checks["automatic_download"]:
            state = ConnectorState.READY
        else:
            state = ConnectorState.DEGRADED
        self.state = state
        missing = [name for name, ok in checks.items() if not ok]
        self.last_error = (
            "Contrôles incomplets: " + ", ".join(missing)
            if state == ConnectorState.DEGRADED
            else (
                "Registre AFM inaccessible"
                if state == ConnectorState.UNAVAILABLE
                else None
            )
        )
        example_output = self._record_output(example) if example else None
        if example_output is not None and example_documents:
            example_output["download_url"] = (
                example_documents[0].download_url
            )
            example_output["download_filename"] = (
                example_documents[0].filename
            )
        fields = (
            "record_id",
            "filing_date",
            "issuing_institution",
            "reporting_year",
            "document_type",
            "document_type_en",
            "filename",
            "detail_url",
            "download_url",
        )
        return NetherlandsSourceDiagnostic(
            source=self.source_name,
            state=state,
            called_url=self.register_url,
            http_status=page_attempt.http_status,
            total_count=len(records) if records else None,
            detected_count=len(records),
            fields=fields,
            example_notice=example_output,
            checks=checks,
            attempts=tuple(attempts),
            error=self.last_error,
        )

    def discover(self, query: str) -> NetherlandsSourceDiscovery:
        records = list(self._load_records())
        csv_attempt, csv_text = self._request_text(
            name="afm_financial_csv",
            url=self.export_url("csv"),
            csv_export=True,
        )
        csv_count = 0
        if csv_text:
            csv_attempt, csv_records = self._parse_attempt(
                csv_attempt,
                lambda text: parse_afm_csv(
                    text,
                    register_url=self.register_url,
                ),
                csv_text,
            )
            csv_count = len(csv_records)
        self._record_attempts.append(csv_attempt)

        page_attempt, page_html = self._request_text(
            name="afm_financial_html",
            url=self.register_url,
        )
        html_count = 0
        if page_html:
            parsed_listing = parse_afm_listing_html(
                page_html,
                register_url=self.register_url,
            )
            html_count = len(parsed_listing.records)
            page_attempt = replace(
                page_attempt,
                total_count=parsed_listing.total_count or html_count,
            )
        self._record_attempts.append(page_attempt)

        query_norm = normalize_text(query)
        matched = [
            record
            for record in records
            if not query_norm
            or query_norm
            in normalize_text(
                " ".join(
                    (
                        record.issuing_institution,
                        record.document_type,
                        record.document_type_en or "",
                        record.filename or "",
                    )
                )
            )
        ]
        notices: list[NetherlandsDiscoveryNotice] = []
        for record in matched[:10]:
            documents = self._detail_documents(record)
            if documents:
                for document in documents:
                    notices.append(
                        NetherlandsDiscoveryNotice(
                            record_id=record.record_id,
                            filing_date=record.filing_date,
                            issuing_institution=record.issuing_institution,
                            reporting_year=record.reporting_year,
                            document_type=document.document_type,
                            filename=document.filename,
                            detail_url=record.detail_url,
                            download_url=document.download_url,
                        )
                    )
            else:
                notices.append(
                    NetherlandsDiscoveryNotice(
                        record_id=record.record_id,
                        filing_date=record.filing_date,
                        issuing_institution=record.issuing_institution,
                        reporting_year=record.reporting_year,
                        document_type=(
                            record.document_type_en
                            or record.document_type
                        ),
                        filename=record.filename,
                        detail_url=record.detail_url,
                        download_url=None,
                    )
                )
        attempts = tuple(self._record_attempts)
        attempt_by_name = {attempt.name: attempt for attempt in attempts}
        candidates = (
            NetherlandsEndpointCandidate(
                url=self.export_url("xml"),
                role="primary complete financial reporting export",
                format="XML",
                pagination="complete export",
                fields=(
                    "id",
                    "datum",
                    "uitgevende-instelling",
                    "boekjaar",
                    "filename",
                    "objecttype",
                    "objecttype_eng",
                ),
                verified=self._records_source == "xml",
                state=(
                    ConnectorState.READY
                    if self._records_source == "xml"
                    else ConnectorState.DEGRADED
                ),
                http_status=getattr(
                    attempt_by_name.get("afm_financial_xml"),
                    "http_status",
                    None,
                ),
                records_count=len(records),
            ),
            NetherlandsEndpointCandidate(
                url=self.export_url("csv"),
                role="secondary financial reporting export",
                format="CSV",
                pagination="complete export",
                fields=(
                    "Datum deponering",
                    "Uitgevende instelling",
                    "Boekjaar",
                    "Soort",
                ),
                verified=any(
                    attempt.name == "afm_financial_csv"
                    and attempt.success
                    and (attempt.total_count or 0) > 0
                    for attempt in attempts
                ),
                state=(
                    ConnectorState.READY
                    if csv_count
                    else ConnectorState.DEGRADED
                ),
                http_status=getattr(
                    attempt_by_name.get("afm_financial_csv"),
                    "http_status",
                    None,
                ),
                records_count=csv_count or None,
            ),
            NetherlandsEndpointCandidate(
                url=self.register_url,
                role="HTML listing and detail fallback",
                format="HTML",
                pagination=(
                    "/api/sitecore/RegisterOverview/PagedRegisters "
                    "with skip/take/currentPage"
                ),
                fields=(
                    "filing_date",
                    "issuing_institution",
                    "reporting_year",
                    "document_type",
                    "detail_url",
                    "download_url",
                ),
                verified=bool(page_attempt.success and html_count),
                state=(
                    ConnectorState.READY
                    if page_attempt.success and html_count
                    else ConnectorState.DEGRADED
                ),
                http_status=page_attempt.http_status,
                records_count=page_attempt.total_count,
            ),
            NetherlandsEndpointCandidate(
                url=self.home_member_state_url,
                role="home member state verification",
                format="XML/HTML",
                pagination="complete XML export",
                fields=(
                    "publicationdate",
                    "companyname",
                    "countryname",
                ),
                verified=bool(self._load_home_records()),
                state=(
                    ConnectorState.READY
                    if self._home_records
                    else ConnectorState.DEGRADED
                ),
                records_count=len(self._home_records or ()),
            ),
        )
        return NetherlandsSourceDiscovery(
            source=self.source_name,
            query=query,
            candidates=candidates,
            notices=tuple(notices),
            attempts=tuple(
                self._record_attempts + self._home_attempts
            ),
        )
