from pathlib import Path

from load_watchlist import load_watchlist, normalize_market


def test_load_watchlist_ignores_metadata_and_invalid_isin(tmp_path: Path) -> None:
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text(
        "Name;ISIN;Symbol;Market\n"
        '"European Equities"\n'
        '"15 May 2025"\n'
        "Air Liquide;FR0000120073;AI;Paris\n"
        "Bad;INVALID;BAD;XPAR\n"
        "Aker;NO0010234552;AKER;XOSL\n",
        encoding="utf-8",
    )

    result = load_watchlist(csv_path)

    assert result.rows_read == 5
    assert result.invalid_rows == 3
    assert [issuer.isin for issuer in result.issuers] == [
        "FR0000120073",
        "NO0010234552",
    ]
    assert result.issuers[0].market == "Euronext Paris"
    assert result.issuers[1].market == "Oslo Børs"


def test_market_normalization_preserves_unknown_market() -> None:
    assert normalize_market("  Euronext   Brussels ") == "Euronext Brussels"


def test_italian_market_aliases_are_normalized() -> None:
    assert normalize_market("Borsa Italiana") == "Euronext Milan"
    assert normalize_market("MTA") == "Euronext Milan"
    assert normalize_market("MTA - Star") == "Euronext Star Milan"
    assert normalize_market("AIM Italia") == "Euronext Growth Milan"
    assert (
        normalize_market("AIM -Italia/Mercato Alternativo del Capitale")
        == "Euronext Growth Milan"
    )


def test_netherlands_market_aliases_are_normalized() -> None:
    assert normalize_market("Amsterdam") == "Euronext Amsterdam"
    assert normalize_market("AMS") == "Euronext Amsterdam"
    assert normalize_market("XAMS") == "Euronext Amsterdam"
    assert (
        normalize_market("Euronext Growth Amsterdam")
        == "Euronext Amsterdam"
    )


def test_belgium_market_aliases_are_normalized() -> None:
    assert normalize_market("Brussels") == "Euronext Brussels"
    assert normalize_market("BRU") == "Euronext Brussels"
    assert normalize_market("XBRU") == "Euronext Brussels"
    assert (
        normalize_market("Alternext Brussels")
        == "Euronext Growth Brussels"
    )


def test_ireland_market_aliases_are_normalized() -> None:
    assert normalize_market("Dublin") == "Euronext Dublin"
    assert normalize_market("ISE") == "Euronext Dublin"
    assert normalize_market("Irish Stock Exchange") == "Euronext Dublin"
    assert normalize_market("Euronext Growth Dublin") == "Euronext Dublin"
    assert normalize_market("Global Exchange Market") == "Euronext Dublin"
    assert (
        normalize_market("Euronext Growth Brussels")
        == "Euronext Growth Brussels"
    )


def test_denmark_market_aliases_are_normalized() -> None:
    for alias in (
        "Nasdaq Copenhagen",
        "Copenhagen",
        "OMX Copenhagen",
        "Nasdaq OMX Copenhagen",
        "Copenhagen Stock Exchange",
        "Danish Stock Exchange",
        "First North Denmark",
        "Nasdaq First North Copenhagen",
    ):
        assert normalize_market(alias) == "Nasdaq Copenhagen"


def test_denmark_sample_watchlist_is_importable() -> None:
    sample = Path(__file__).parents[1] / "samples" / "watchlist_denmark.csv"
    result = load_watchlist(sample)

    assert result.rows_read == 6
    assert result.invalid_rows == 0
    assert result.duplicate_rows == 0
    assert {issuer.symbol for issuer in result.issuers} >= {
        "MATAS",
        "NOVO B",
        "CARL B",
        "VWS",
        "DANSKE",
    }
    assert {
        issuer.market for issuer in result.issuers
    } == {"Nasdaq Copenhagen"}


def test_austria_sample_watchlist_is_importable() -> None:
    sample = Path(__file__).parents[1] / "samples" / "watchlist_austria.csv"
    result = load_watchlist(sample)

    assert result.rows_read == 5
    assert result.invalid_rows == 0
    assert result.duplicate_rows == 0
    assert {issuer.market for issuer in result.issuers} == {
        "Vienna Stock Exchange"
    }
    assert all(
        issuer.pea_geography_status == "eu_candidate"
        for issuer in result.issuers
    )
    assert all(
        issuer.austria_home_member_state == "Austria"
        for issuer in result.issuers
    )


def test_poland_sample_watchlist_is_importable() -> None:
    sample = Path(__file__).parents[1] / "samples" / "watchlist_poland.csv"
    result = load_watchlist(sample)

    assert result.rows_read == 5
    assert result.invalid_rows == 0
    assert result.duplicate_rows == 0
    assert {issuer.market for issuer in result.issuers} == {
        "Warsaw Stock Exchange"
    }
    assert all(
        issuer.pea_geography_status == "eu_candidate"
        for issuer in result.issuers
    )
