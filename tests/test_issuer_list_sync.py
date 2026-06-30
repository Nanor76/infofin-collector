from __future__ import annotations

import tempfile
from pathlib import Path

from issuer_list_sync import (
    IssuerListEntry,
    _dedupe_entries,
    _enrich_entry,
    _split_market_field,
    market_slug,
    write_market_csv,
)
from issuer_list_sync import IsinIndex, _build_isin_index


def test_split_market_field_expands_multi_listing() -> None:
    markets = _split_market_field("Euronext Paris, Amsterdam")
    assert markets == ("Euronext Paris", "Euronext Amsterdam")


def test_dedupe_entries_prefers_isin_variant() -> None:
    entries = _dedupe_entries(
        [
            IssuerListEntry(
                name="AB INBEV",
                isin="",
                symbol="ABI",
                market="Euronext Brussels",
                source="test",
            ),
            IssuerListEntry(
                name="AB INBEV",
                isin="BE0974293251",
                symbol="ABI",
                market="Euronext Brussels",
                source="test",
            ),
        ]
    )
    assert len(entries) == 1
    assert entries[0].isin == "BE0974293251"


def test_enrich_entry_uses_euronext_index() -> None:
    index = _build_isin_index(
        [
            IssuerListEntry(
                name="AB INBEV",
                isin="BE0974293251",
                symbol="ABI",
                market="Euronext Brussels",
                source="euronext",
            )
        ]
    )
    enriched = _enrich_entry(
        IssuerListEntry(
            name="AB INBEV",
            isin="",
            symbol="ABI",
            market="Euronext Brussels",
            source="fsma",
        ),
        index,
    )
    assert enriched.isin == "BE0974293251"


def test_write_market_csv() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "euronext_paris.csv"
        count = write_market_csv(
            path,
            [
                IssuerListEntry(
                    name="ACCOR",
                    isin="FR0000120404",
                    symbol="AC",
                    market="Euronext Paris",
                    source="euronext",
                )
            ],
        )
        assert count == 1
        content = path.read_text(encoding="utf-8-sig")
        assert "FR0000120404" in content
        assert "Euronext Paris" in content


def test_market_slug_normalizes_accents() -> None:
    assert market_slug("Oslo Børs") == "oslo_bors"