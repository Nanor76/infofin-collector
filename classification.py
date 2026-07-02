from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import urlparse


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", ascii_text.casefold()).strip()


def _url_extension(url: str) -> str:
    return PurePosixPath(urlparse(url).path).suffix.casefold()


def _is_non_report_notice(text: str) -> bool:
    if any(
        marker in text
        for marker in (
            "aide-memoire",
            "aide memoire",
            "aide-memoire",
            "report de la publication",
            "report de publication",
            "report publication",
            "postponement of publication",
            "delay of publication",
            "delayed publication",
        )
    ):
        return True
    return False


def classify_document(
    title: str,
    url: str = "",
    content_type: str = "",
) -> str | None:
    text = _normalize(f"{title} {url}")
    extension = _url_extension(url)
    mime = (content_type or "").casefold().split(";", 1)[0].strip()

    if _is_non_report_notice(text):
        return None

    if (
        "universal registration document" in text
        or "document d'enregistrement universel" in text
        or "document d enregistrement universel" in text
        or re.search(r"\b(?:urd|deu)\b", text)
    ):
        return "universal_registration_document"

    if (
        "rapport financier semestriel" in text
        or "half-year financial report" in text
        or "half year financial report" in text
        or "half yearly financial report" in text
        or "semi-annual financial report" in text
        or "semi annual financial report" in text
        or "halfjaarlijks financieel verslag" in text
        or "halvarsrapport" in text
        or "relatorio financeiro semestral" in text
        or "relatorio semestral" in text
        or "contas semestrais" in text
        or "publicacao de contas semestrais" in text
        or "relatorio e contas do 1 semestre" in text
        or "relatorio e contas do 1 o semestre" in text
        or "informe financiero semestral" in text
        or "informe semestral" in text
        or "cuentas semestrales" in text
        or "half-yearly report" in text
        or "half yearly report" in text
        or "interim report" in text
        or re.search(r"\brfs\b", text)
    ):
        return "half_year_financial_report"

    if (
        "rapport financier annuel" in text
        or "informe financiero anual" in text
        or "informe anual" in text
        or "cuentas anuales" in text
        or "annual financial report" in text
        or "annual report" in text
        or "jaarverslag" in text
        or "arsrapport" in text
        or "relatorio financeiro anual" in text
        or "relatorio e contas anual" in text
        or "relatorio e contas" in text
        or "relatorio anual" in text
        or "contas anuais" in text
        or "publicacao de contas anuais" in text
        or "annual results" in text
        or "publication of annual report" in text
        or re.search(r"\brfa\b", text)
    ):
        return "annual_financial_report"

    if (
        "quarterly report" in text
        or "informe trimestral" in text
        or re.search(r"\bq[1-4]\b", text)
        or re.search(r"\b[1-4]q\b", text)
        or re.search(
            r"\b(?:first|second|third|fourth) quarter\b",
            text,
        )
    ):
        return "quarterly_financial_report"

    if "financial report" in text:
        return "financial_report"

    if (
        "esef" in text
        or "xhtml" in text
        or extension in {".xhtml", ".xml", ".zip", ".xbri"}
        or mime in {
            "application/xhtml+xml",
            "application/xml",
            "text/xml",
            "application/zip",
        }
    ):
        return "esef"

    return None


def supported_extension(url: str, content_type: str = "") -> str | None:
    extension = _url_extension(url)
    mime = (content_type or "").casefold().split(";", 1)[0].strip()

    if extension == ".pdf" or mime == "application/pdf":
        return "pdf"
    if extension in {".xhtml", ".xht"} or mime == "application/xhtml+xml":
        return "xhtml"
    if extension == ".xml" or ".xml." in urlparse(url).path.casefold() or mime in {
        "application/xml",
        "text/xml",
    }:
        return "xml"
    if extension in {".zip", ".xbri"} or mime in {
        "application/zip",
        "application/x-zip-compressed",
    }:
        return "zip"
    return None
