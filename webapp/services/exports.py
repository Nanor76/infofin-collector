from __future__ import annotations

import csv
import json
import unicodedata
from dataclasses import asdict
from pathlib import Path

from load_watchlist import normalize_market
from webapp.services.document_search import (
    LinkSearchDocument,
    LinkSearchResultSet,
)

CSV_FIELDNAMES = (
    "market",
    "source",
    "source_document_id",
    "published_at",
    "period_end_date",
    "reporting_year",
    "document_type",
    "classification",
    "title",
    "url",
    "issuer_name",
    "issuer_isin",
    "issuer_lei",
    "category",
    "date_confidence",
    "source_publication_date_raw",
)

WEB_CSV_FIELDNAMES = (
    "market",
    "published_at",
    "period_end_date",
    "reporting_year",
    "document_type",
    "title",
    "issuer_name",
    "issuer_isin",
    "issuer_lei",
    "file_format",
    "document_url",
)


def _market_output_slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", normalize_market(value))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return "".join(
        character.lower() if character.isalnum() else "_"
        for character in ascii_value
    ).strip("_")


def documents_to_rows(
    documents: tuple[LinkSearchDocument, ...],
) -> list[dict[str, object]]:
    return [
        {
            "market": document.market,
            "source": document.source,
            "source_document_id": document.source_document_id,
            "published_at": document.published_at,
            "period_end_date": document.period_end_date,
            "reporting_year": document.reporting_year,
            "document_type": document.document_type,
            "classification": document.classification,
            "title": document.title,
            "url": document.url,
            "issuer_name": document.issuer_name,
            "issuer_isin": document.issuer_isin,
            "issuer_lei": document.issuer_lei,
            "category": document.category,
            "date_confidence": document.date_confidence,
            "source_publication_date_raw": document.source_publication_date_raw,
        }
        for document in documents
    ]


def _export_filename(
    result_set: LinkSearchResultSet,
    output_format: str,
) -> str:
    markets = result_set.request.markets
    date_from = result_set.request.date_from
    date_to = result_set.request.date_to
    scope = "all" if len(markets) != 1 else _market_output_slug(markets[0])
    return (
        f"market_documents_{scope}_{date_from:%Y%m%d}_"
        f"{date_to:%Y%m%d}.{output_format}"
    )


def write_search_export(
    result_set: LinkSearchResultSet,
    *,
    output_format: str,
    output_dir: str | Path,
) -> Path:
    if output_format not in {"csv", "json"}:
        raise ValueError("format attendu: csv ou json")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    target = output_path / _export_filename(result_set, output_format)
    rows = documents_to_rows(result_set.documents)

    if output_format == "json":
        payload = {
            "date_from": result_set.request.date_from.isoformat(),
            "date_to": result_set.request.date_to.isoformat(),
            "markets": [
                normalize_market(market) for market in result_set.request.markets
            ],
            "documents_count": len(rows),
            "errors": list(result_set.errors),
            "warnings": list(result_set.warnings),
            "market_summaries": [
                asdict(summary) for summary in result_set.market_summaries
            ],
            "documents": rows,
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    else:
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
    return target


def write_web_results_export(
    *,
    rows: list[dict[str, object]],
    output_format: str,
    target: Path,
) -> Path:
    if output_format not in {"csv", "json"}:
        raise ValueError("format attendu: csv ou json")

    target.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        target.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    else:
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=WEB_CSV_FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {field: row.get(field, "") for field in WEB_CSV_FIELDNAMES}
                )
    return target
