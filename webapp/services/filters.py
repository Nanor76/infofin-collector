from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from webapp.services.document_search import LinkSearchDocument


def normalize_search_text(value: object) -> str:
    text = "" if value is None else str(value)
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", ascii_text.casefold()).strip()


def document_matches_query(document: LinkSearchDocument, query: str | None) -> bool:
    if not query or not query.strip():
        return True
    needle = normalize_search_text(query)
    if not needle:
        return True
    haystack = normalize_search_text(
        " ".join(
            (
                document.title,
                document.issuer_name,
                document.issuer_isin,
                document.issuer_lei,
            )
        )
    )
    return needle in haystack


def filter_documents(
    documents: tuple[LinkSearchDocument, ...],
    *,
    document_types: tuple[str, ...] = (),
    query: str | None = None,
    issuer_isin: str | None = None,
    sources: tuple[str, ...] = (),
    formats: tuple[str, ...] = (),
    date_confidences: tuple[str, ...] = (),
) -> tuple[LinkSearchDocument, ...]:
    result: list[LinkSearchDocument] = []
    type_filter = set(document_types) if document_types else None
    source_filter = set(sources) if sources else None
    format_filter = set(formats) if formats else None
    confidence_filter = set(date_confidences) if date_confidences else None
    isin_filter = issuer_isin.strip().casefold() if issuer_isin else None

    for document in documents:
        if type_filter is not None and document.document_type not in type_filter:
            continue
        if not document_matches_query(document, query):
            continue
        if isin_filter is not None:
            document_isins = {
                part.strip().casefold()
                for part in document.issuer_isin.split(",")
                if part.strip()
            }
            if isin_filter not in document_isins:
                continue
        if source_filter is not None and document.source not in source_filter:
            continue
        if format_filter is not None and document.file_format not in format_filter:
            continue
        if (
            confidence_filter is not None
            and document.date_confidence not in confidence_filter
        ):
            continue
        result.append(document)
    return tuple(result)
