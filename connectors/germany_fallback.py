from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import date
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests
from typing import Any

from connectors.base import Connector, ConnectorState, DocumentCandidate
from models import Issuer

logger = logging.getLogger(__name__)

POSITIVE_TERMS = [
    "annual report", "annual financial report", "geschäftsbericht",
    "jahresfinanzbericht", "half-year report", "semi-annual report",
    "halbjahresfinanzbericht", "interim report", "zwischenbericht",
    "quarterly report", "quartalsbericht", "quarterly financial report",
    "quartalsfinanzbericht", "financial report", "finanzbericht",
    "esef", "xhtml", "xbrl", "zip esef"
]

NEGATIVE_TERMS = [
    "conference call", "call transcript", "speech", "presentation",
    "investor presentation", "analyst presentation", "slides", "webcast",
    "capital markets day", "factsheet", "quarterly statement speech",
    "ceo statement", "cfo statement", "ceo/cfo statement", "press release",
    "release", "invitation", "agm", "voting rights", "prospectus",
    "bond", "notes", "share buyback", "acquisition", "corporate action",
    "transcript", "call", "statement", "directors dealings",
    "sustainability only", "esg only"
]

def extract_year(text: str, url: str) -> int | None:
    combined = f"{text} {url}"
    years = re.findall(r'\b(202\d)\b', combined)
    if years:
        return int(years[0])
    return None

def guess_document_type(text: str, url: str) -> str:
    combined = f"{text} {url}".lower()
    if any(term in combined for term in ("annual", "geschäfts", "jahres")):
        return "annual_financial_report"
    if any(term in combined for term in ("half-year", "halbjahres", "semi-annual")):
        return "half_year_financial_report"
    if any(term in combined for term in ("quarterly", "quartal", "q1", "q2", "q3", "q4")):
        return "quarterly_financial_report"
    if any(term in combined for term in ("interim", "zwischen")):
        return "interim_report"
    if any(term in combined for term in ("esef", "xhtml", "xbrl", "zip")):
        return "esef"
    return "financial_report"

def classify_link(text: str, url: str) -> tuple[bool, str | None, str | None, list[str], list[str]]:
    """
    Classifies a link.
    Returns:
        (is_accepted, reason, doc_type, matched_pos, matched_neg)
    """
    combined = f"{text} {url}".lower()
    
    # Protect positive occurrences of "statement" when checking negative terms
    temp_combined = combined
    for protected in ["financial statements", "financial statement"]:
        temp_combined = temp_combined.replace(protected, "fin_stmt")
        
    # Check negative terms first
    matched_neg = []
    for neg in NEGATIVE_TERMS:
        if neg in temp_combined:
            matched_neg.append(neg)
            
    if matched_neg:
        return False, f"Contient le terme interdit: '{matched_neg[0]}'", None, [], matched_neg
        
    # Check positive terms
    matched_pos = []
    for pos in POSITIVE_TERMS:
        if pos in combined:
            matched_pos.append(pos)
            
    if not matched_pos:
        return False, "ambiguous_non_report", None, [], []
        
    doc_type = guess_document_type(text, url)
    return True, None, doc_type, matched_pos, []

class GermanyIssuerWebsiteFallbackConnector(Connector):
    def __init__(self, session: requests.Session, timeout: int = 10, database: Any = None) -> None:
        self.market = "Xetra"
        self.source_name = "issuer_website_fallback"
        self.state = ConnectorState.READY
        self.session = session
        self.timeout = timeout
        self.database = database
        self.global_http_calls = 0
        self._robots_cache: dict[str, RobotFileParser] = {}
        self._scanned_notices = 0
        self._details_visited = 0

    def _is_allowed_by_robots(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        if base_url not in self._robots_cache:
            rp = RobotFileParser()
            rp.set_url(base_url)
            try:
                # Use HEAD or GET with short timeout
                r = self.session.get(base_url, timeout=5, verify=True)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.allow_all = True
            except Exception:
                rp.allow_all = True
            self._robots_cache[base_url] = rp
        user_agent = self.session.headers.get("User-Agent", "Mozilla/5.0")
        return self._robots_cache[base_url].can_fetch(user_agent, url)

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        start_url = getattr(issuer, "investor_relations_url", None) or getattr(issuer, "reports_url", None)
        if not start_url:
            logger.warning("Aucune URL IR ou de rapport renseignée pour l'émetteur %s (%s)", issuer.name, issuer.isin)
            return []

        logger.info("Démarrage du crawler IR pour %s à l'adresse %s", issuer.name, start_url)
        
        parsed_start = urllib.parse.urlparse(start_url)
        start_domain = parsed_start.netloc.lower()
        
        visited_pages = set()
        queue = [start_url]
        accepted_candidates: list[DocumentCandidate] = []
        rejected_candidates: list[DocumentCandidate] = []
        pages_limit_per_issuer = 5
        
        while queue and len(visited_pages) < pages_limit_per_issuer:
            if self.global_http_calls >= 50:
                logger.warning("Limite globale de 50 requêtes HTTP atteinte pour le fallback Allemagne.")
                break
                
            current_url = queue.pop(0)
            if current_url in visited_pages:
                continue
                
            # Restreindre le domaine (même domaine ou sous-domaine)
            parsed_current = urllib.parse.urlparse(current_url)
            current_domain = parsed_current.netloc.lower()
            if not current_domain.endswith(start_domain) and not start_domain.endswith(current_domain):
                continue
                
            # Respecter robots.txt
            if not self._is_allowed_by_robots(current_url):
                logger.info("URL interdite par robots.txt: %s", current_url)
                continue
                
            logger.debug("Exploration de la page: %s", current_url)
            visited_pages.add(current_url)
            self.global_http_calls += 1
            self._details_visited += 1
            
            try:
                r = self.session.get(current_url, timeout=self.timeout, verify=True)
                if r.status_code != 200:
                    logger.warning("Échec de récupération de %s (status: %d)", current_url, r.status_code)
                    continue
            except Exception as e:
                logger.warning("Erreur lors de la récupération de %s: %s", current_url, e)
                continue
                
            soup = BeautifulSoup(r.text, 'html.parser')
            
            # Recherche de liens
            for a in soup.find_all('a', href=True):
                href = a.get('href').strip()
                if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                    continue
                    
                abs_url = urllib.parse.urljoin(current_url, href)
                # Supprimer les ancres et normaliser
                parsed_abs = urllib.parse.urlparse(abs_url)
                abs_url = urllib.parse.urlunparse((
                    parsed_abs.scheme,
                    parsed_abs.netloc,
                    parsed_abs.path,
                    parsed_abs.params,
                    parsed_abs.query,
                    ""
                ))
                
                link_text = a.get_text().strip()
                link_lower = link_text.lower()
                href_lower = abs_url.lower()
                
                # Vérifier s'il s'agit d'un fichier document
                is_doc_url = any(parsed_abs.path.lower().endswith(ext) for ext in ('.pdf', '.zip', '.xhtml', '.xbrl')) or \
                             any(f".{ext}?" in href_lower for ext in ('pdf', 'zip', 'xhtml', 'xbrl'))
                             
                if is_doc_url:
                    if any(c.url == abs_url for c in accepted_candidates) or any(c.url == abs_url for c in rejected_candidates):
                        continue
                        
                    is_accepted, reason, doc_type, matched_pos, matched_neg = classify_link(link_text, abs_url)
                    year = extract_year(link_text, abs_url)
                    
                    if is_accepted:
                        candidate = DocumentCandidate(
                            title=link_text or f"{doc_type}_{year or ''}",
                            url=abs_url,
                            published_date=None,
                            published_at=None,
                            document_type=doc_type,
                            source="issuer_website_fallback",
                            reporting_year=year,
                            period_end_date=date(year, 12, 31) if year else None,
                            date_confidence="low",
                            date_extraction_reason="Année estimée à partir du texte du site ou de l'URL",
                            matched_positive_terms=matched_pos,
                            matched_negative_terms=[],
                            metadata={
                                "official_source": False,
                                "validation_status": "needs_manual_review",
                                "confidence": "low",
                                "parent_page_url": current_url,
                            }
                        )
                        accepted_candidates.append(candidate)
                        self._scanned_notices += 1
                    else:
                        candidate = DocumentCandidate(
                            title=link_text or f"Rejected_{year or ''}",
                            url=abs_url,
                            published_date=None,
                            published_at=None,
                            document_type="candidate_rejected",
                            source="issuer_website_fallback",
                            reporting_year=year,
                            period_end_date=date(year, 12, 31) if year else None,
                            date_confidence="low",
                            date_extraction_reason="Année estimée à partir du texte du site ou de l'URL",
                            classification_reason=reason,
                            matched_positive_terms=matched_pos,
                            matched_negative_terms=matched_neg,
                            metadata={
                                "official_source": False,
                                "validation_status": "rejected_false_positive",
                                "confidence": "low",
                                "parent_page_url": current_url,
                            }
                        )
                        rejected_candidates.append(candidate)
                else:
                    # Lien HTML interne, suivre s'il contient des mots clés liés aux rapports
                    follow_terms = ['report', 'financial', 'investor', 'shareholder', 'download', 'archiv', 'berichte', 'ir', 'investoren', 'publikation']
                    if any(term in link_lower or term in href_lower for term in follow_terms):
                        if abs_url not in visited_pages and abs_url not in queue:
                            queue.append(abs_url)
                            
        # Retourner au maximum 3 candidats acceptés, et un échantillon des rejetés pour traçabilité
        return accepted_candidates[:3] + rejected_candidates[:10]
