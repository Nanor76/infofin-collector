from __future__ import annotations

import logging
import re
import time
import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from connectors.base import (
    Connector,
    ConnectorState,
    DocumentCandidate,
    EndpointAttempt,
    DatasetCandidate,
    SourceDiagnostic,
    SourceDiscovery,
)
from models import Issuer

LOGGER = logging.getLogger(__name__)

# Category mapping
OAM_CATEGORIES = {
    "270": "annual_financial_report",           # Tilinpäätös ja toimintakertomus
    "13": "annual_financial_report",            # Vuosikertomus
    "73": "year_end_report",                    # Tilinpäätöstiedote
    "78": "half_year_financial_report",         # Puolivuosikatsaus
    "153": "quarterly_financial_report",        # Osavuosikatsaus (Q1 and Q3)
    "68": "quarterly_financial_report",         # Neljännesvuosikatsaus
    "32": "interim_report",                     # Johdon osavuotinen selvitys
}

NEGATIVE_TERMS = (
    "prospectus", "esite", "listalleottoesite",
    "major holding", "flaggning", "liputus", "suurimmat osakkeenomistajat",
    "insider", "manager transaction", "johtohenkilöiden liiketoimet", "johtohenkiloiden", "johdon kaupat",
    "voting rights", "äänimäärä", "äänioikeus", "aanimaara", "aanioikeus",
    "general meeting", "yhtiökokous", "yhtiökokouskutsu", "varsinainen yhtiökokous", "yhtiökokouksen päätökset", "yhtiokokous",
    "share buyback", "omien osakkeiden", "takaisinosto",
    "bond", "notes", "debt", "laina", "jvk",
    "tender offer", "ostotarjous", "uppköpserbjudande",
    "capital increase", "capital event", "pääomatapahtuma", "paaomatapahtuma",
    "rights issue", "merkintäoikeus", "merkintaoikeus",
    "corporate action",
    "financial calendar", "tulosjulkistamisajankohdat",
    "articles of association", "yhtiöjärjestys", "yhtiojarjestys",
    "calendar", "kalenteri", "osinkojulkistus"
)

POSITIVE_RULES = (
    ("year_end_report", ("year-end report", "tilinpäätöstiedote", "tilinpaatostiedote", "year end report")),
    ("annual_financial_report", ("annual financial report", "annual report", "vuosikertomus", "tilinpäätös ja toimintakertomus", "tilinpäätös", "tilinpaatos")),
    ("half_year_financial_report", ("half-year report", "half year report", "halvårsrapport", "puolivuosikatsaus", "puolivuotinen")),
    ("quarterly_financial_report", ("quarterly report", "neljännesvuosikatsaus", "osavuosikatsaus", "q1", "q2", "q3")),
    ("interim_report", ("interim report", "johdon osavuotinen selvitys", "liiketoimintakatsaus")),
)

def _normalize(value: str) -> str:
    return " ".join((value or "").lower().split())

def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    # "2026-06-14 21:30:01 EEST"
    raw = value.strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass
    return None

def classify_finland_document(
    title: str,
    category: str = "",
    url: str = "",
) -> tuple[str, str, list[str], list[str]]:
    haystack = _normalize(" ".join((title, category, url)))
    negative = [term for term in NEGATIVE_TERMS if _normalize(term) in haystack]
    
    matched_positive: list[str] = []
    for _, terms in POSITIVE_RULES:
        matched_positive.extend(term for term in terms if _normalize(term) in haystack)

    # File extensions
    for ext in ("esef", "xhtml", "xbrl", "zip", "pdf"):
        if ext in haystack:
            matched_positive.append(ext)

    if negative:
        return (
            "other_regulatory_announcement",
            f"Explicit exclusion term: {negative[0]}",
            sorted(set(matched_positive)),
            sorted(set(negative)),
        )

    # 1. Direct Category Match
    cat_norm = _normalize(category)
    for cat_id, doc_type in OAM_CATEGORIES.items():
        if cat_norm and (cat_id in cat_norm or doc_type in cat_norm):
            return (
                doc_type,
                f"Periodic category matched: {category}",
                sorted(set(matched_positive)),
                [],
            )

    # 2. Text Pattern Match
    for classification, terms in POSITIVE_RULES:
        detected = [term for term in terms if _normalize(term) in haystack]
        if detected:
            return (
                classification,
                f"Periodic report term: {detected[0]}",
                sorted(set(matched_positive)),
                [],
            )

    return (
        "other_regulatory_announcement",
        "No periodic financial-report term or category detected",
        sorted(set(matched_positive)),
        [],
    )

def extract_finland_date_info(
    title: str,
    published_raw: str | None,
    category: str = "",
) -> dict[str, Any]:
    published_at = _parse_date(published_raw)
    period_end: date | None = None
    reporting_year: int | None = None
    reason = "No reporting-period date detected"
    confidence = "low"
    search_text = _normalize(" ".join((title, category)))

    # Try extracting year from title
    year_match = re.search(r"\b(20\d{2})\b", search_text)
    if year_match:
        reporting_year = int(year_match.group(1))
        confidence = "medium"
        reason = "Reporting year extracted from report title"
        
        # Estimate end date based on document type
        doc_type, _, _, _ = classify_finland_document(title, category)
        if doc_type in ("annual_financial_report", "year_end_report"):
            period_end = date(reporting_year, 12, 31)
        elif doc_type == "half_year_financial_report":
            period_end = date(reporting_year, 6, 30)
        elif "q1" in search_text:
            period_end = date(reporting_year, 3, 31)
        elif "q2" in search_text:
            period_end = date(reporting_year, 6, 30)
        elif "q3" in search_text:
            period_end = date(reporting_year, 9, 30)
        elif "q4" in search_text:
            period_end = date(reporting_year, 12, 31)
        else:
            # Default to end of year
            period_end = date(reporting_year, 12, 31)

    return {
        "published_at": published_at,
        "period_end_date": period_end,
        "reporting_year": reporting_year,
        "source_publication_date_raw": published_raw,
        "source_period_date_raw": str(reporting_year) if reporting_year else None,
        "date_confidence": confidence,
        "date_extraction_reason": reason,
    }

@dataclass(frozen=True)
class FinlandFile:
    filename: str
    download_url: str
    file_type: str

@dataclass(frozen=True)
class FinlandNotice:
    record_id: str
    title: str
    issuer_name: str
    published_raw: str
    detail_url: str
    category: str = ""
    files: tuple[FinlandFile, ...] = ()

@dataclass
class FinlandIssuerResolution:
    found: bool
    matched_name: str | None = None
    finland_oam_company_id: str | None = None
    finland_oam_issuer_url: str | None = None
    finland_oam_detail_url: str | None = None
    finland_home_member_state: str | None = None
    finland_nasdaq_company_url: str | None = None
    finland_pea_country_check: str | None = None
    match_score: int = 0
    attempts: list[EndpointAttempt] = field(default_factory=list)
    error: str | None = None

    # Compatibility properties for discover-issuer output mapping
    @property
    def company_id(self) -> str | None:
        return self.finland_oam_company_id

    @property
    def isin(self) -> str | None:
        return None

    @property
    def stori_url(self) -> str | None:
        return self.finland_oam_issuer_url

    @property
    def detail_url(self) -> str | None:
        return self.finland_oam_detail_url

    @property
    def home_member_state(self) -> str | None:
        return self.finland_home_member_state


@dataclass
class FinlandSourceDiscovery:
    source: str
    query: str
    candidates: tuple[DocumentCandidate, ...]
    notices: tuple[FinlandNotice, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


class FinlandOamConnector(Connector):
    market = "Nasdaq Helsinki"
    source_name = "finland_oam"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str = "https://www.oam.fi",
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 7,
        timeout: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.state = ConnectorState.READY
        self.last_error = None
        self._scanned_notices = 0
        self._details_visited = 0
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _get(self, url: str) -> requests.Response:
        self._wait()
        start = time.monotonic()
        try:
            response = self.session.get(url, timeout=self.timeout, verify=self.verify_ssl)
            self.attempts.append(EndpointAttempt(
                name="Finland OAM GET",
                base_url=self.base_url,
                dataset=None,
                endpoint=url.replace(self.base_url, ""),
                method="GET",
                http_status=response.status_code,
                success=response.status_code == 200,
            ))
            return response
        except Exception as exc:
            self.attempts.append(EndpointAttempt(
                name="Finland OAM GET",
                base_url=self.base_url,
                dataset=None,
                endpoint=url.replace(self.base_url, ""),
                method="GET",
                http_status=None,
                success=False,
                error=str(exc),
            ))
            raise exc
        finally:
            self._last_request_at = time.monotonic()

    def _post(self, url: str, data: dict[str, Any]) -> requests.Response:
        self._wait()
        try:
            response = self.session.post(url, data=data, timeout=self.timeout, verify=self.verify_ssl)
            self.attempts.append(EndpointAttempt(
                name="Finland OAM POST",
                base_url=self.base_url,
                dataset=None,
                endpoint=url.replace(self.base_url, ""),
                method="POST",
                http_status=response.status_code,
                success=response.status_code == 200,
            ))
            return response
        except Exception as exc:
            self.attempts.append(EndpointAttempt(
                name="Finland OAM POST",
                base_url=self.base_url,
                dataset=None,
                endpoint=url.replace(self.base_url, ""),
                method="POST",
                http_status=None,
                success=False,
                error=str(exc),
            ))
            raise exc
        finally:
            self._last_request_at = time.monotonic()

    def _fetch_csrf_and_options(self) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
        res = self._get(f"{self.base_url}/")
        soup = BeautifulSoup(res.text, "html.parser")
        
        csrf_tag = soup.find('meta', {'name': '_csrf'})
        csrf_token = csrf_tag.get('content') if csrf_tag else ""
        
        if not csrf_token:
            csrf_input = soup.find('input', {'name': '_csrf'})
            csrf_token = csrf_input.get('value') if csrf_input else ""
            
        companies = []
        company_select = soup.find(id='company-select')
        if company_select and company_select.get('options'):
            companies = json.loads(company_select.get('options'))
            
        categories = []
        category_select = soup.find(id='category-select')
        if category_select and category_select.get('options'):
            categories = json.loads(category_select.get('options'))
            
        return csrf_token, companies, categories

    def _search(
        self,
        *,
        company_id: str = "",
        category_id: str = "",
        start_date: date | None = None,
        end_date: date | None = None,
        free_text: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> list[FinlandNotice]:
        csrf, _, _ = self._fetch_csrf_and_options()
        if not csrf:
            LOGGER.warning("Could not extract CSRF token from OAM.fi")
            return []
            
        if start_date is None:
            if free_text:
                start_date = date.today() - timedelta(days=365 * 5)
            else:
                start_date = date.today() - timedelta(days=self.lookback_days)
        if end_date is None:
            end_date = date.today()

        payload = {
            '_csrf': csrf,
            'oam': 'fi',
            'language': 'fi',
            'pageSize': str(page_size),
            'page': str(page),
            'market': '',
            'company': company_id,
            'category': category_id,
            'startDate': start_date.isoformat(),
            'endDate': end_date.isoformat(),
            'freeText': free_text,
            'includeObsoleteCompanies': 'true',
        }
        
        res = self._post(f"{self.base_url}/", payload)
        soup = BeautifulSoup(res.text, "html.parser")
        rows = soup.find_all('nef-table-row', class_='message-row')
        
        notices = []
        for row in rows:
            cells = row.find_all('nef-table-cell')
            if len(cells) < 3:
                continue
                
            pub_date = cells[0].get_text(strip=True)
            company = cells[1].get_text(strip=True)
            
            link_el = cells[2].find('nef-link') or cells[2].find('a')
            headline = link_el.get_text(strip=True) if link_el else ""
            href = link_el.get('href') if link_el else ""
            
            category = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            
            # Extract ID from href (e.g. /view/472900?lang=fi -> 472900)
            record_id = ""
            if href:
                match = re.search(r"/view/(\d+)", href)
                if match:
                    record_id = match.group(1)
            
            notices.append(FinlandNotice(
                record_id=record_id or str(len(notices)),
                title=headline,
                issuer_name=company,
                published_raw=pub_date,
                detail_url=urllib.parse.urljoin(self.base_url, href) if href else "",
                category=category,
            ))
            
        return notices

    @property
    def scanned_notices(self) -> int:
        return self._scanned_notices

    @property
    def details_visited(self) -> int:
        return self._details_visited

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        end_dt = date.today()
        start_dt = since or (end_dt - timedelta(days=self.lookback_days))
        
        LOGGER.info("Searching Finland OAM documents from %s to %s", start_dt, end_dt)
        page_size = max(1, limit or 1000)
        notices = self._search(
            start_date=start_dt,
            end_date=end_dt,
            page_size=page_size,
        )
        self._scanned_notices = len(notices)
        
        candidates = []
        for notice in notices:
            date_info = extract_finland_date_info(notice.title, notice.published_raw, notice.category)
            pub_at = date_info["published_at"]
            if since and pub_at and pub_at < since:
                continue
                
            cls, cls_reason, pos_terms, neg_terms = classify_finland_document(
                notice.title, notice.category, notice.detail_url
            )
            
            # We wrap notice as candidate. Materialize will fetch files later
            candidates.append(DocumentCandidate(
                title=notice.title,
                url=notice.detail_url,
                published_date=pub_at,
                document_type=cls,
                source="finland_oam",
                source_document_id=notice.record_id,
                metadata={
                    "issuer_name": notice.issuer_name,
                    "category": notice.category,
                    "record_id": notice.record_id,
                    "detail_url": notice.detail_url,
                    "home_member_state": "Finland",
                    "pea_country_check": "eu_candidate",
                },
                classification=cls,
                classification_reason=cls_reason,
                matched_positive_terms=pos_terms,
                matched_negative_terms=neg_terms,
                published_at=pub_at,
                period_end_date=date_info["period_end_date"],
                reporting_year=date_info["reporting_year"],
                date_confidence=date_info["date_confidence"],
                date_extraction_reason=date_info["date_extraction_reason"],
                source_publication_date_raw=date_info["source_publication_date_raw"],
                source_period_date_raw=date_info["source_period_date_raw"],
            ))
            
        if limit is not None:
            candidates = candidates[:limit]
        return candidates

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        # Targeted search via company ID resolving
        company_id = issuer.finland_oam_company_id
        if not company_id:
            res = self.resolve_issuer(issuer)
            company_id = res.finland_oam_company_id
            
        if not company_id:
            LOGGER.warning("Could not resolve OAM company ID for %s", issuer.name)
            return []
            
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=self.lookback_days)
        notices = self._search(company_id=company_id, start_date=start_dt, end_date=end_dt)
        self._scanned_notices = len(notices)
        
        candidates = []
        for notice in notices:
            date_info = extract_finland_date_info(notice.title, notice.published_raw, notice.category)
            cls, cls_reason, pos_terms, neg_terms = classify_finland_document(
                notice.title, notice.category, notice.detail_url
            )
            
            candidates.append(DocumentCandidate(
                title=notice.title,
                url=notice.detail_url,
                published_date=date_info["published_at"],
                document_type=cls,
                source="finland_oam",
                source_document_id=notice.record_id,
                metadata={
                    "issuer_name": notice.issuer_name,
                    "category": notice.category,
                    "record_id": notice.record_id,
                    "detail_url": notice.detail_url,
                    "home_member_state": "Finland",
                    "pea_country_check": "eu_candidate",
                },
                classification=cls,
                classification_reason=cls_reason,
                matched_positive_terms=pos_terms,
                matched_negative_terms=neg_terms,
                published_at=date_info["published_at"],
                period_end_date=date_info["period_end_date"],
                reporting_year=date_info["reporting_year"],
                date_confidence=date_info["date_confidence"],
                date_extraction_reason=date_info["date_extraction_reason"],
                source_publication_date_raw=date_info["source_publication_date_raw"],
                source_period_date_raw=date_info["source_period_date_raw"],
            ))
        return candidates

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        detail_url = candidate.metadata.get("detail_url")
        if not detail_url:
            return [candidate]
            
        self._details_visited += 1
        res = self._get(detail_url)
        if res.status_code != 200:
            LOGGER.warning("Could not fetch detail page %s", detail_url)
            return [candidate]
            
        soup = BeautifulSoup(res.text, "html.parser")
        
        # Extract attachment links
        files = []
        links = soup.find_all(href=lambda h: h and 'viewAttachment' in h)
        for idx, link in enumerate(links):
            href = link.get('href')
            full_url = urllib.parse.urljoin(self.base_url, href)
            label = link.get_text(strip=True) or f"attachment_{idx}.pdf"
            
            # Determine format
            fmt = "pdf"
            if ".zip" in label.lower() or "zip" in full_url.lower():
                fmt = "zip"
            elif ".xhtml" in label.lower() or "xhtml" in full_url.lower():
                fmt = "xhtml"
            elif ".xml" in label.lower() or "xml" in full_url.lower():
                fmt = "xml"
                
            files.append(FinlandFile(
                filename=label,
                download_url=full_url,
                file_type=fmt,
            ))
            
        if not files:
            # Fallback if no attachments found, just return detail url
            return [candidate]
            
        materialized = []
        for f in files:
            meta = dict(candidate.metadata)
            meta["file_format"] = f.file_type
            meta["filename"] = f.filename
            
            # Incorporate attachment ID in the source doc ID to guarantee idempotency per file
            attachment_id = ""
            match = re.search(r"messageAttachmentId=(\d+)", f.download_url)
            if match:
                attachment_id = match.group(1)
            composed_doc_id = f"{candidate.source_document_id}:{attachment_id}" if attachment_id else candidate.source_document_id
            
            materialized.append(DocumentCandidate(
                title=candidate.title,
                url=f.download_url,
                published_date=candidate.published_date,
                document_type=candidate.document_type,
                source=candidate.source,
                source_document_id=composed_doc_id,
                metadata=meta,
                classification=candidate.classification,
                classification_reason=candidate.classification_reason,
                matched_positive_terms=candidate.matched_positive_terms,
                matched_negative_terms=candidate.matched_negative_terms,
                published_at=candidate.published_at,
                period_end_date=candidate.period_end_date,
                reporting_year=candidate.reporting_year,
                date_confidence=candidate.date_confidence,
                date_extraction_reason=candidate.date_extraction_reason,
                source_publication_date_raw=candidate.source_publication_date_raw,
                source_period_date_raw=candidate.source_period_date_raw,
            ))
            
        return materialized

    def resolve_issuer(self, issuer: Issuer) -> FinlandIssuerResolution:
        try:
            _, companies, _ = self._fetch_csrf_and_options()
            
            # Fuzzy match issuer.name or issuer.symbol
            norm_name = _normalize(issuer.name)
            norm_symbol = _normalize(issuer.symbol)
            
            best_match = None
            best_score = 0
            
            for item in companies:
                label_norm = _normalize(item["label"])
                val = item["value"]
                
                score = 0
                if norm_name and norm_name == label_norm:
                    score = 100
                elif norm_name and (norm_name in label_norm or label_norm in norm_name):
                    score = 80
                elif norm_symbol and norm_symbol in label_norm:
                    score = 60
                    
                if score > best_score:
                    best_match = item
                    best_score = score
                    
            if not best_match:
                return FinlandIssuerResolution(
                    found=False,
                    finland_pea_country_check="eu_candidate",
                    attempts=list(self.attempts),
                    error="No matching OAM company found",
                )
                
            return FinlandIssuerResolution(
                found=True,
                matched_name=best_match["label"],
                finland_oam_company_id=best_match["value"],
                finland_oam_issuer_url=f"{self.base_url}/?company={best_match['value']}",
                finland_oam_detail_url=f"{self.base_url}/?company={best_match['value']}",
                finland_home_member_state="Finland",
                finland_nasdaq_company_url="https://www.nasdaqomxnordic.com",
                finland_pea_country_check="eu_candidate",
                match_score=best_score,
                attempts=list(self.attempts),
            )
            
        except Exception as exc:
            return FinlandIssuerResolution(
                found=False,
                finland_pea_country_check="eu_candidate",
                attempts=list(self.attempts),
                error=str(exc),
            )

    def diagnose(self, limit: int = 10) -> SourceDiagnostic:
        try:
            csrf, companies, categories = self._fetch_csrf_and_options()
            
            # Query recent notices
            notices = self._search(page_size=limit)
            
            formats = set()
            example = None
            if notices:
                example = {
                    "record_id": notices[0].record_id,
                    "title": notices[0].title,
                    "issuer": notices[0].issuer_name,
                    "published_at": notices[0].published_raw,
                    "category": notices[0].category,
                    "detail_url": notices[0].detail_url,
                }
                
                # Check formatting of first few notices
                for notice in notices[:3]:
                    if notice.detail_url:
                        # Dummy mock candidate
                        cand = DocumentCandidate(
                            title=notice.title,
                            url=notice.detail_url,
                            published_date=date.today(),
                            document_type="financial_report",
                            source="finland_oam",
                            metadata={"detail_url": notice.detail_url},
                        )
                        mats = self.materialize_candidate(cand, None)
                        for m in mats:
                            fmt = m.metadata.get("file_format")
                            if fmt:
                                formats.add(fmt.upper())
            
            return SourceDiagnostic(
                source="finland_oam",
                state=ConnectorState.READY if notices else ConnectorState.DEGRADED,
                base_url=self.base_url,
                dataset="oam-fi-disclosures",
                selected_endpoint="POST /",
                total_count=len(notices),
                fields=(
                    "published_at",
                    "issuer_name",
                    "title",
                    "category",
                    "detail_url",
                    "attachment_url",
                ),
                example_record=example,
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return SourceDiagnostic(
                source="finland_oam",
                state=ConnectorState.UNAVAILABLE,
                base_url=self.base_url,
                dataset="oam-fi-disclosures",
                selected_endpoint="POST /",
                total_count=0,
                fields=(),
                example_record=None,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def discover(self, query: str, limit: int = 25) -> FinlandSourceDiscovery:
        try:
            notices = self._search(free_text=query, page_size=limit)
            
            candidates = []
            for notice in notices:
                date_info = extract_finland_date_info(notice.title, notice.published_raw, notice.category)
                cls, cls_reason, pos_terms, neg_terms = classify_finland_document(
                    notice.title, notice.category, notice.detail_url
                )
                
                candidates.append(DocumentCandidate(
                    title=notice.title,
                    url=notice.detail_url,
                    published_date=date_info["published_at"],
                    document_type=cls,
                    source="finland_oam",
                    source_document_id=notice.record_id,
                    metadata={
                        "issuer_name": notice.issuer_name,
                        "category": notice.category,
                        "record_id": notice.record_id,
                        "detail_url": notice.detail_url,
                    },
                    classification=cls,
                    classification_reason=cls_reason,
                    matched_positive_terms=pos_terms,
                    matched_negative_terms=neg_terms,
                    published_at=date_info["published_at"],
                    period_end_date=date_info["period_end_date"],
                    reporting_year=date_info["reporting_year"],
                ))
                
            return FinlandSourceDiscovery(
                source="finland_oam",
                query=query,
                candidates=tuple(candidates),
                notices=tuple(notices),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return FinlandSourceDiscovery(
                source="finland_oam",
                query=query,
                candidates=(),
                notices=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None = None,
    ) -> int:
        # 1 GET for homepage/CSRF + 1 POST search
        return 2

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        # 1 GET CSRF + 1 POST search
        return 2
