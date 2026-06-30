from __future__ import annotations

import csv
import json
import os
import pytest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import Settings
from db import Database
from models import Issuer
from screener.config import ScreenerConfig
from screener.eodhd import EodHdClient, load_eodhd_token
from screener.higgons import HiggonsScreener
from screener.cli import run_screen_higgons

# ----------------------------------------------------
# 1. Test lecture token depuis fichier & 2. absence token
# ----------------------------------------------------

def test_load_token_success():
    with patch("screener.eodhd.Path") as mock_path:
        instance = mock_path.return_value
        instance.exists.return_value = True
        instance.read_text.return_value = "my_token \n"
        
        token = load_eodhd_token()
        assert token == "my_token"

def test_load_token_absent():
    with patch("screener.eodhd.Path") as mock_path:
        instance = mock_path.return_value
        instance.exists.return_value = False
        
        with pytest.raises(FileNotFoundError):
            load_eodhd_token()

def test_load_token_empty():
    with patch("screener.eodhd.Path") as mock_path:
        instance = mock_path.return_value
        instance.exists.return_value = True
        instance.read_text.return_value = "   "
        
        with pytest.raises(ValueError):
            load_eodhd_token()

# Helper to build mock fundamentals
def make_mock_fundamentals(
    currency="EUR",
    asset_type="Common Stock",
    market_cap=1_000_000_000,
    shares=10_000_000,
    net_income_latest=150_000_000,
    net_income_prev=100_000_000,
    revenue_latest=1_000_000_000,
    revenue_prev=800_000_000,
    ebit=150_000_000,
    ebitda=200_000_000,
    equity=600_000_000,
    short_debt=50_000_000,
    long_debt=150_000_000,
    cash=100_000_000,
    depr_amort=50_000_000,
    op_cash_flow=None,
    total_assets=None,
    current_liab=None,
    date_latest="2025-12-31",
    date_prev="2024-12-31",
):
    res = {
        "General": {
            "Type": asset_type,
            "CurrencyCode": currency,
            "Exchange": "XPAR",
            "Country": "France",
        },
        "Highlights": {
            "MarketCapitalization": market_cap,
            "SharesOutstanding": shares,
            "PERatio": 10.0,
        },
        "Valuation": {
            "TrailingPE": 10.0,
        },
        "SharesStats": {
            "SharesOutstanding": shares,
        },
        "Financials": {
            "Income_Statement": {
                "yearly": {
                    date_latest: {
                        "netIncome": net_income_latest,
                        "totalRevenue": revenue_latest,
                        "ebit": ebit,
                        "ebitda": ebitda,
                    },
                }
            },
            "Balance_Sheet": {
                "yearly": {
                    date_latest: {
                        "totalStockholderEquity": equity,
                        "shortTermDebt": short_debt,
                        "longTermDebt": long_debt,
                        "cashAndEquivalents": cash,
                    }
                }
            },
            "Cash_Flow": {
                "yearly": {
                    date_latest: {
                        "depreciationAndAmortization": depr_amort,
                        "totalCashFromOperatingActivities": op_cash_flow,
                    }
                }
            }
        }
    }

    if date_prev:
        res["Financials"]["Income_Statement"]["yearly"][date_prev] = {
            "netIncome": net_income_prev,
            "totalRevenue": revenue_prev,
        }

    # Add optional fallbacks
    if total_assets is not None:
        res["Financials"]["Balance_Sheet"]["yearly"][date_latest]["totalAssets"] = total_assets
    if current_liab is not None:
        res["Financials"]["Balance_Sheet"]["yearly"][date_latest]["totalCurrentLiabilities"] = current_liab
    if op_cash_flow is not None:
        res["Financials"]["Cash_Flow"]["yearly"][date_latest]["totalCashFromOperatingActivities"] = op_cash_flow
    if depr_amort is None:
        res["Financials"]["Cash_Flow"]["yearly"][date_latest].pop("depreciationAndAmortization", None)

    return res

# Helper to build mock price history
def make_mock_prices(length_days=400, price_start=100.0, price_end=150.0):
    prices = []
    start_date = date(2025, 1, 1)
    for i in range(length_days):
        dt = start_date + timedelta(days=i)
        # Interpolate price
        factor = i / (length_days - 1)
        close = price_start + (price_end - price_start) * factor
        prices.append({
            "date": dt.isoformat(),
            "close": close,
            "adjusted_close": close,
            "volume": 1000,
        })
    return prices

@pytest.fixture
def mock_client():
    client = MagicMock(spec=EodHdClient)
    client.get_forex_rate.return_value = (1.0, False)
    return client

# ----------------------------------------------------
# 3. Test calcul performance 12 mois
# ----------------------------------------------------

def test_calcul_performance_12m(mock_client):
    prices = make_mock_prices(length_days=370, price_start=100.0, price_end=150.0)
    mock_client.get_eod_historical_data.return_value = prices
    mock_client.get_fundamentals.return_value = make_mock_fundamentals()

    screener = HiggonsScreener(mock_client)
    issuer = Issuer("Test", "FR0000000001", "TEST", "Euronext Paris")
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    
    assert res["passed"] is True
    # Performance is (150 - 100) / 100 = 50%
    assert abs(res["result"]["stock_perf_12m"] - 0.50) < 0.05

# ----------------------------------------------------
# 4. Test calcul performance relative
# ----------------------------------------------------

def test_calcul_performance_relative(mock_client):
    prices = make_mock_prices(length_days=370, price_start=100.0, price_end=150.0) # stock = +50%
    index_prices = make_mock_prices(length_days=370, price_start=100.0, price_end=120.0) # index = +20%
    mock_client.get_eod_historical_data.return_value = prices
    mock_client.get_fundamentals.return_value = make_mock_fundamentals()

    screener = HiggonsScreener(mock_client)
    issuer = Issuer("Test", "FR0000000001", "TEST", "Euronext Paris")
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5), index_perf_history=index_prices)
    
    assert res["passed"] is True
    assert abs(res["result"]["stock_perf_12m"] - 0.50) < 0.05
    assert abs(res["result"]["index_perf_12m"] - 0.20) < 0.05
    # Relative = 50% - 20% = 30%
    assert abs(res["result"]["relative_perf_12m"] - 0.30) < 0.05

# ----------------------------------------------------
# 5. Test calcul PER
# ----------------------------------------------------

def test_calcul_pe_ratio(mock_client):
    prices = make_mock_prices(length_days=370)
    mock_client.get_eod_historical_data.return_value = prices
    
    # We want TrailngPE to be invalid/missing so screener calculates it:
    # PER = MarketCapitalization / NetIncome
    # MarketCap = 1,000,000,000, NetIncome = 125,000,000 => PER = 8
    fund = make_mock_fundamentals(market_cap=1_000_000_000, net_income_latest=125_000_000)
    fund["Highlights"]["PERatio"] = None
    fund["Valuation"]["TrailingPE"] = None
    mock_client.get_fundamentals.return_value = fund

    screener = HiggonsScreener(mock_client)
    issuer = Issuer("Test", "FR0000000001", "TEST", "Euronext Paris")
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    
    assert res["passed"] is True
    assert res["result"]["pe_ratio"] == 8.0

# ----------------------------------------------------
# 6. Test calcul P/CF avec amortissements
# ----------------------------------------------------

def test_calcul_p_cf_with_dna(mock_client):
    prices = make_mock_prices(length_days=370)
    mock_client.get_eod_historical_data.return_value = prices
    
    # CF = NetIncome (100M) + DNA (50M) = 150M. P/CF = MarketCap (900M) / 150M = 6.0
    fund = make_mock_fundamentals(market_cap=900_000_000, net_income_latest=100_000_000, depr_amort=50_000_000)
    mock_client.get_fundamentals.return_value = fund

    screener = HiggonsScreener(mock_client)
    issuer = Issuer("Test", "FR0000000001", "TEST", "Euronext Paris")
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    
    assert res["passed"] is True
    assert res["result"]["p_cf"] == 6.0
    assert res["result"]["p_cf_source"] == "net_income_plus_dna"

# ----------------------------------------------------
# 7. Test calcul P/CF fallback Operating Cash Flow
# ----------------------------------------------------

def test_calcul_p_cf_fallback_operating_cash_flow(mock_client):
    prices = make_mock_prices(length_days=370)
    mock_client.get_eod_historical_data.return_value = prices
    
    # depr_amort = None, op_cash_flow = 120M. P/CF = MarketCap (600M) / 120M = 5.0
    fund = make_mock_fundamentals(market_cap=600_000_000, net_income_latest=100_000_000, depr_amort=None, op_cash_flow=120_000_000)
    mock_client.get_fundamentals.return_value = fund

    screener = HiggonsScreener(mock_client)
    issuer = Issuer("Test", "FR0000000001", "TEST", "Euronext Paris")
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    
    assert res["passed"] is True
    assert res["result"]["p_cf"] == 5.0
    assert res["result"]["p_cf_source"] == "operating_cash_flow_fallback"
    assert res["result"]["data_quality_score"] == 60  # -10 for fallback, -30 for no index

# ----------------------------------------------------
# 8. Test calcul ROCE principal
# ----------------------------------------------------

def test_calcul_roce_principal(mock_client):
    prices = make_mock_prices(length_days=370)
    mock_client.get_eod_historical_data.return_value = prices
    
    # EBIT = 100M
    # Equity = 400M
    # ShortDebt = 50M, LongDebt = 150M, Cash = 100M => NetDebt = 100M
    # ROCE = 100M / (400M + 100M) = 20%
    fund = make_mock_fundamentals(ebit=100_000_000, equity=400_000_000, short_debt=50_000_000, long_debt=150_000_000, cash=100_000_000)
    mock_client.get_fundamentals.return_value = fund

    screener = HiggonsScreener(mock_client)
    issuer = Issuer("Test", "FR0000000001", "TEST", "Euronext Paris")
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    
    assert res["passed"] is True
    assert res["result"]["roce"] == 0.20
    assert res["result"]["roce_source"] == "equity_plus_net_debt"

# ----------------------------------------------------
# 9. Test calcul ROCE fallback
# ----------------------------------------------------

def test_calcul_roce_fallback(mock_client):
    prices = make_mock_prices(length_days=370)
    mock_client.get_eod_historical_data.return_value = prices
    
    # equity = 10M, net_income = 5M => ROE = 50% (passes)
    # short_debt = 0, long_debt = 0, cash = 20M => NetDebt = -20M
    # denom = equity + NetDebt = 10M - 20M = -10M <= 0 => triggers fallback
    # EBIT = 100M, Assets = 800M, CurrentLiab = 300M => Denom = 500M => ROCE = 20%
    fund = make_mock_fundamentals(
        market_cap=1_000_000,
        ebit=100_000_000,
        equity=10_000_000,
        net_income_latest=5_000_000,
        short_debt=0,
        long_debt=0,
        cash=20_000_000,
        total_assets=800_000_000,
        current_liab=300_000_000,
    )
    mock_client.get_fundamentals.return_value = fund

    screener = HiggonsScreener(mock_client)
    issuer = Issuer("Test", "FR0000000001", "TEST", "Euronext Paris")
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    
    assert res["passed"] is True
    assert res["result"]["roce"] == 0.20
    assert res["result"]["roce_source"] == "assets_minus_current_liabilities"
    assert res["result"]["data_quality_score"] == 60  # -10 for fallback, -30 for no index

# ----------------------------------------------------
# 10. Test dette nette négative
# ----------------------------------------------------

def test_dette_nette_negative(mock_client):
    prices = make_mock_prices(length_days=370)
    mock_client.get_eod_historical_data.return_value = prices
    
    # ShortDebt = 50M, LongDebt = 50M, Cash = 200M => NetDebt = -100M
    # Denominator in ROCE will be Equity (400M) + NetDebt (-100M) = 300M
    # EBIT = 60M => ROCE = 60M / 300M = 20%
    # EBITDA = -10M (negative, but passed automatically because NetDebt < 0)
    fund = make_mock_fundamentals(ebit=60_000_000, ebitda=-10_000_000, equity=400_000_000, short_debt=50_000_000, long_debt=50_000_000, cash=200_000_000)
    mock_client.get_fundamentals.return_value = fund

    screener = HiggonsScreener(mock_client)
    issuer = Issuer("Test", "FR0000000001", "TEST", "Euronext Paris")
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    
    assert res["passed"] is True
    assert res["result"]["roce"] == 0.20
    assert res["result"]["net_cash_position"] is True
    assert res["result"]["net_debt_to_ebitda"] is None

# ----------------------------------------------------
# 11. Test rejet à chaque filtre
# ----------------------------------------------------

def test_rejets_filtres(mock_client):
    screener = HiggonsScreener(mock_client)
    issuer = Issuer("Test", "FR0000000001", "TEST", "Euronext Paris")

    # 1. NOT_COMMON_STOCK
    prices = make_mock_prices(length_days=370)
    mock_client.get_eod_historical_data.return_value = prices
    fund = make_mock_fundamentals(asset_type="ETF")
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "NOT_COMMON_STOCK"

    # 2. INSUFFICIENT_PRICE_HISTORY
    prices_short = make_mock_prices(length_days=10)
    mock_client.get_eod_historical_data.return_value = prices_short
    fund = make_mock_fundamentals()
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "INSUFFICIENT_PRICE_HISTORY"

    # Reset prices
    mock_client.get_eod_historical_data.return_value = prices

    # 3. MARKET_CAP_TOO_HIGH
    fund = make_mock_fundamentals(market_cap=13_000_000_000) # > 12B
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "MARKET_CAP_TOO_HIGH"

    # 4. INSUFFICIENT_LIQUIDITY
    # Zero volume to cause traded value = 0 < 50k
    prices_no_vol = make_mock_prices(length_days=370)
    for p in prices_no_vol:
        p["volume"] = 0
    mock_client.get_eod_historical_data.return_value = prices_no_vol
    fund = make_mock_fundamentals()
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "INSUFFICIENT_LIQUIDITY"

    # Reset prices
    mock_client.get_eod_historical_data.return_value = prices

    # 5. RELATIVE_MOMENTUM_TOO_WEAK
    # Stock drop -50% (100 -> 50), Index stable
    prices_drop = make_mock_prices(length_days=370, price_start=100.0, price_end=50.0)
    index_stable = make_mock_prices(length_days=370, price_start=100.0, price_end=100.0)
    mock_client.get_eod_historical_data.return_value = prices_drop
    fund = make_mock_fundamentals()
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5), index_perf_history=index_stable)
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "RELATIVE_MOMENTUM_TOO_WEAK"

    # Reset prices
    mock_client.get_eod_historical_data.return_value = prices

    # 6. NEGATIVE_OR_INVALID_PE
    # Net income <= 0
    fund = make_mock_fundamentals(net_income_latest=-10_000_000)
    fund["Highlights"]["PERatio"] = -5.0
    fund["Valuation"]["TrailingPE"] = -5.0
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "NEGATIVE_OR_INVALID_PE"

    # 7. PE_TOO_HIGH
    # MarketCap 1.5B / NetIncome 10M = 150 PER
    fund = make_mock_fundamentals(market_cap=1_500_000_000, net_income_latest=10_000_000)
    fund["Highlights"]["PERatio"] = 150.0
    fund["Valuation"]["TrailingPE"] = 150.0
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "PE_TOO_HIGH"

    # 8. PCF_TOO_HIGH
    # MarketCap 1.2B / CF 100M = 12 P/CF
    fund = make_mock_fundamentals(market_cap=1_200_000_000, net_income_latest=50_000_000, depr_amort=50_000_000)
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "PCF_TOO_HIGH"

    # 9. NEGATIVE_NET_INCOME
    # Note: checked before in PE, but let's test specifically
    fund = make_mock_fundamentals(market_cap=1_000_000, net_income_latest=-1_000_000, depr_amort=150_000_000)
    # Set positive highlights PE to bypass PE check but trigger net income quality check
    fund["Highlights"]["PERatio"] = 8.0
    fund["Valuation"]["TrailingPE"] = 8.0
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "NEGATIVE_NET_INCOME"

    # 10. NEGATIVE_REVENUE_GROWTH
    # Rev latest 900M < Rev prev 1B
    fund = make_mock_fundamentals(revenue_latest=900_000_000, revenue_prev=1_000_000_000)
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "NEGATIVE_REVENUE_GROWTH"

    # 11. EBIT_MARGIN_TOO_LOW
    # EBIT 40M / Rev 1B = 4% < 5%
    fund = make_mock_fundamentals(ebit=40_000_000, revenue_latest=1_000_000_000)
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "EBIT_MARGIN_TOO_LOW"

    # 12. ROE_TOO_LOW
    # NetIncome 50M / Equity 600M = 8.3% < 9%
    fund = make_mock_fundamentals(net_income_latest=50_000_000, equity=600_000_000, depr_amort=60_000_000)
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "ROE_TOO_LOW"

    # 13. ROCE_TOO_LOW
    # EBIT 50M / (Equity 500M + NetDebt 100M) = 50M / 600M = 8.3% < 10%
    fund = make_mock_fundamentals(ebit=50_000_000, equity=500_000_000, short_debt=100_000_000, long_debt=100_000_000, cash=100_000_000)
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "ROCE_TOO_LOW"

    # 14. NET_DEBT_EBITDA_TOO_HIGH
    # NetDebt = 200M (Short 100 + Long 200 - Cash 100), EBITDA = 50M => ratio = 4.0 >= 3.0
    fund = make_mock_fundamentals(short_debt=100_000_000, long_debt=200_000_000, cash=100_000_000, ebitda=50_000_000)
    mock_client.get_fundamentals.return_value = fund
    res = screener.screen_issuer(issuer, as_of_date=date(2026, 1, 5))
    assert res["passed"] is False
    assert res["rejection"]["rejection_code"] == "NET_DEBT_EBITDA_TOO_HIGH"

# ----------------------------------------------------
# 12. Test génération CSV candidats & 13. rejets
# ----------------------------------------------------

def test_generations_csv_cli(tmp_path):
    # Setup files and folders
    db_file = tmp_path / "infofin.sqlite3"
    database = Database(db_file)
    database.initialize()
    database.upsert_issuers([
        Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris"),
        Issuer("Sanofi", "FR0000120578", "SAN", "Euronext Paris"),
    ])

    settings = Settings(
        db_path=db_file,
        data_dir=tmp_path / "raw",
        http_timeout_seconds=5,
        http_retries=1,
        http_backoff_factor=0.1,
        user_agent="Test",
        max_download_bytes=1000,
        amf_base_url="",
        amf_fallback_base_urls=(),
        amf_dataset="",
        amf_rows=10,
    )

    out_candidates = tmp_path / "candidates.csv"
    out_rejections = tmp_path / "rejections.csv"
    out_json = tmp_path / "candidates.json"

    # Mock the EodHdClient
    with patch("screener.cli.EodHdClient") as mock_client_cls:
        client_instance = mock_client_cls.return_value
        # Mock Symbol List
        client_instance.get_exchange_symbol_list.return_value = [
            {"Code": "AI", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000120073", "Currency": "EUR"},
            {"Code": "SAN", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000120578", "Currency": "EUR"},
        ]
        client_instance.get_forex_rate.return_value = (1.0, False)

        # Sanofi passes, Air Liquide fails (not common stock in fundamentals or other reason)
        # Mock EOD
        client_instance.get_eod_historical_data.return_value = make_mock_prices(length_days=370)
        # Mock Fundamentals
        # AI fails: low ROE
        fund_ai = make_mock_fundamentals()
        fund_ai["Highlights"]["MarketCapitalization"] = 1_000_000
        fund_ai["Financials"]["Income_Statement"]["yearly"]["2025-12-31"]["netIncome"] = 10_000 # ROE = 10k / 600M = 0.001 (fails)
        
        # SAN passes
        fund_san = make_mock_fundamentals()

        def get_fundamentals_mock(symbol, exchange, force=False):
            if symbol == "AI":
                return fund_ai
            return fund_san

        client_instance.get_fundamentals.side_effect = get_fundamentals_mock

        # Run the command
        ret = run_screen_higgons(
            database=database,
            settings=settings,
            market_arg="paris",
            as_of_date_arg=date(2026, 1, 5),
            output_csv=str(out_candidates),
            output_json=str(out_json),
            explain_rejections=True,
        )

        assert ret == 0

        # Check candidate output
        assert out_candidates.exists()
        with out_candidates.open("r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            assert len(reader) == 1
            assert reader[0]["ticker"] == "SAN"
            assert float(reader[0]["data_quality_score"]) == 70.0

        # Check rejection output
        # Rejections output CSV path is created by replacing candidates with rejections
        expected_rejections_path = Path(str(out_candidates).replace("candidates", "rejections"))
        assert expected_rejections_path.exists()
        with expected_rejections_path.open("r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            assert len(reader) == 1
            assert reader[0]["ticker"] == "AI"
            assert reader[0]["rejection_code"] == "ROE_TOO_LOW"

# ----------------------------------------------------
# 14. Test mode --limit
# ----------------------------------------------------

def test_cli_mode_limit(tmp_path):
    db_file = tmp_path / "infofin.sqlite3"
    database = Database(db_file)
    database.initialize()
    # Add 3 issuers
    database.upsert_issuers([
        Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris"),
        Issuer("Sanofi", "FR0000120578", "SAN", "Euronext Paris"),
        Issuer("LVMH", "FR0000121014", "MC", "Euronext Paris"),
    ])

    settings = Settings(
        db_path=db_file, data_dir=tmp_path / "raw", http_timeout_seconds=5, http_retries=1, http_backoff_factor=0.1,
        user_agent="Test", max_download_bytes=1000, amf_base_url="", amf_fallback_base_urls=(), amf_dataset="", amf_rows=10,
    )

    with patch("screener.cli.EodHdClient") as mock_client_cls:
        client_instance = mock_client_cls.return_value
        client_instance.get_exchange_symbol_list.return_value = [
            {"Code": "AI", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000120073", "Currency": "EUR"},
            {"Code": "SAN", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000120578", "Currency": "EUR"},
            {"Code": "MC", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000121014", "Currency": "EUR"},
        ]
        client_instance.get_forex_rate.return_value = (1.0, False)
        client_instance.get_eod_historical_data.return_value = make_mock_prices(length_days=370)
        client_instance.get_fundamentals.return_value = make_mock_fundamentals()

        # Run with --limit 1
        out_candidates = tmp_path / "candidates_limit.csv"
        ret = run_screen_higgons(
            database=database,
            settings=settings,
            market_arg="paris",
            limit=1,
            as_of_date_arg=date(2026, 1, 5),
            output_csv=str(out_candidates),
        )

        assert ret == 0
        with out_candidates.open("r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            # Should only contain 1 row because of --limit 1
            assert len(reader) == 1

# ----------------------------------------------------
# 15. Test mode --force
# ----------------------------------------------------

def test_cli_mode_force(tmp_path):
    db_file = tmp_path / "infofin.sqlite3"
    database = Database(db_file)
    database.initialize()
    database.upsert_issuers([
        Issuer("Sanofi", "FR0000120578", "SAN", "Euronext Paris"),
    ])

    settings = Settings(
        db_path=db_file, data_dir=tmp_path / "raw", http_timeout_seconds=5, http_retries=1, http_backoff_factor=0.1,
        user_agent="Test", max_download_bytes=1000, amf_base_url="", amf_fallback_base_urls=(), amf_dataset="", amf_rows=10,
    )

    with patch("screener.cli.EodHdClient") as mock_client_cls:
        client_instance = mock_client_cls.return_value
        client_instance.get_exchange_symbol_list.return_value = [
            {"Code": "SAN", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000120578", "Currency": "EUR"},
        ]
        client_instance.get_forex_rate.return_value = (1.0, False)
        client_instance.get_eod_historical_data.return_value = make_mock_prices(length_days=370)
        client_instance.get_fundamentals.return_value = make_mock_fundamentals()

        out_candidates = tmp_path / "candidates_force.csv"
        # Run with force=True
        ret = run_screen_higgons(
            database=database,
            settings=settings,
            market_arg="paris",
            force=True,
            as_of_date_arg=date(2026, 1, 5),
            output_csv=str(out_candidates),
        )

        assert ret == 0
        # Verify force=True was passed down to the EODHD calls
        client_instance.get_exchange_symbol_list.assert_called_with("XPAR", force=True)
        client_instance.get_fundamentals.assert_called_with("SAN", "XPAR", force=True)
        client_instance.get_eod_historical_data.assert_called_with("SAN", "XPAR", date(2026, 1, 5), force=True)

# ----------------------------------------------------
# 16. Test token never logged under exceptions
# ----------------------------------------------------

def test_token_never_logged_under_exceptions(tmp_path, caplog):
    import logging
    import requests
    caplog.set_level(logging.DEBUG)
    
    settings = Settings(
        db_path=tmp_path / "infofin.sqlite3",
        data_dir=tmp_path / "data/raw",
        http_timeout_seconds=5,
        http_retries=1,
        http_backoff_factor=0.1,
        user_agent="Test",
        max_download_bytes=1000,
        amf_base_url="",
        amf_fallback_base_urls=(),
        amf_dataset="",
        amf_rows=10,
    )
    
    fake_token = "MY_SUPER_SECRET_TOKEN_4321"
    client = EodHdClient(settings, token=fake_token, backend="rest")
    
    # 1. Test basic logging output from connectionpool
    logger = logging.getLogger("urllib3.connectionpool")
    logger.warning("Retrying ... /api/exchange-symbol-list/XPAR?api_token=%s", fake_token)
    
    # Check that fake_token was redacted
    for record in caplog.records:
        assert fake_token not in record.message
        assert fake_token not in str(record.args)
    
    caplog.clear()
    
    # 2. Test request exceptions (SSLError, HTTPError, etc.)
    with patch("screener.eodhd.requests.get") as mock_requests_get, \
         patch.object(client.session, "get") as mock_get:
         
        dummy_resp = MagicMock()
        dummy_resp.text = "Hello World"
        mock_requests_get.return_value = dummy_resp
        
        url_with_token = f"https://eodhd.com/api/exchange-symbol-list/XPAR?api_token={fake_token}"
        ssl_err = requests.exceptions.SSLError(f"SSL certificate verify failed for {url_with_token}")
        mock_get.side_effect = ssl_err
        
        with pytest.raises(requests.RequestException) as excinfo:
            client.get_exchange_symbol_list("XPAR", force=True)
            
        # Verify the exception message is clean
        assert fake_token not in str(excinfo.value)
        assert "[REDACTED_TOKEN]" in str(excinfo.value)
        
        # Verify that the logged warning/error is also clean
        for record in caplog.records:
            assert fake_token not in record.message
            if record.exc_text:
                assert fake_token not in record.exc_text
            if record.exc_info:
                assert fake_token not in str(record.exc_info)


# ----------------------------------------------------
# Tests for EODHD MCP and Backend Providers
# ----------------------------------------------------

def test_opendns_detection():
    # Test that when test_resp.text contains "block.opendns.com", it returns is_blocked=True
    with patch("screener.eodhd.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = "This site is blocked. block.opendns.com"
        mock_get.return_value = mock_resp
        
        with patch("screener.eodhd.load_eodhd_token", return_value="fake_token_123"), \
             patch("screener.eodhd_mcp.requests.post") as mock_mcp_post:
             
            mock_mcp_resp = MagicMock()
            mock_mcp_resp.headers = {"Mcp-Session-Id": "session_mcp_123"}
            mock_mcp_resp.text = 'data: {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}\n'
            mock_mcp_post.return_value = mock_mcp_resp
            
            settings = Settings.from_env()
            client = EodHdClient(settings, backend="auto")
            
            assert client.backend == "mcp"
            assert mock_get.called

def test_backend_auto_rest_ok():
    # Test that when eodhd.com is not blocked, auto uses rest backend
    with patch("screener.eodhd.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = "Standard EODHD home page content"
        mock_get.return_value = mock_resp
        
        with patch("screener.eodhd.load_eodhd_token", return_value="fake_token_123"):
            settings = Settings.from_env()
            client = EodHdClient(settings, backend="auto")
            assert client.backend == "rest"

def test_backend_rest_forced_fails_if_blocked(tmp_path):
    # Test that forced rest backend fails with SSLError if REST is blocked and we try to query it
    import requests
    with patch("screener.eodhd.requests.get") as mock_get, \
         patch("screener.eodhd.load_eodhd_token", return_value="fake_token_123"):
         
        mock_resp = MagicMock()
        mock_resp.text = "block.opendns.com"
        mock_get.return_value = mock_resp
        
        settings = Settings(
            db_path=tmp_path / "infofin.sqlite3",
            data_dir=tmp_path / "data/raw",
            http_timeout_seconds=5,
            http_retries=1,
            http_backoff_factor=0.1,
            user_agent="Test",
            max_download_bytes=1000,
            amf_base_url="",
            amf_fallback_base_urls=(),
            amf_dataset="",
            amf_rows=10,
        )
        client = EodHdClient(settings, backend="rest")
        
        with patch.object(client.provider.session, "get") as mock_session_get:
            mock_session_get.side_effect = requests.exceptions.SSLError("Verification failed")
            
            with pytest.raises(requests.exceptions.SSLError) as excinfo:
                client.get_exchange_symbol_list("XPAR", force=True)
            assert "Cisco Umbrella" in str(excinfo.value)

def test_backend_mcp_forced_never_calls_rest():
    # Test that when forced to mcp, EodHdClient never calls REST endpoint or tests REST block
    with patch("screener.eodhd.requests.get") as mock_get, \
         patch("screener.eodhd.load_eodhd_token", return_value="fake_token_123"), \
         patch("screener.eodhd_mcp.requests.post") as mock_mcp_post:
         
        mock_mcp_resp = MagicMock()
        mock_mcp_resp.headers = {"Mcp-Session-Id": "mcp_session_456"}
        mock_mcp_resp.text = 'data: {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}\n'
        mock_mcp_post.return_value = mock_mcp_resp
        
        settings = Settings.from_env()
        client = EodHdClient(settings, backend="mcp")
        
        assert client.backend == "mcp"
        assert not mock_get.called

def test_mcp_token_redacted_in_logs(caplog):
    # Test that EODHD token is never logged in clear when calling MCP endpoints
    fake_token = "MY_SECRET_MCP_TOKEN_999"
    with patch("screener.eodhd.load_eodhd_token", return_value=fake_token), \
         patch("screener.eodhd_mcp.requests.post") as mock_mcp_post:
         
        mock_mcp_resp = MagicMock()
        mock_mcp_resp.headers = {"Mcp-Session-Id": "mcp_session_789"}
        mock_mcp_resp.text = 'data: {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}\n'
        mock_mcp_post.return_value = mock_mcp_resp
        
        settings = Settings.from_env()
        client = EodHdClient(settings, backend="mcp")
        
        import logging
        logger = logging.getLogger("infofin.screener.eodhd")
        
        logger.warning("Attempting to connect with token: %s", fake_token)
        logger.error("Failed to query MCP API using token=%s", fake_token)
        
        for record in caplog.records:
            assert fake_token not in record.message
            assert fake_token not in str(record.args)
            if record.message and "Attempting" in record.message:
                assert "[REDACTED_TOKEN]" in record.message

def test_mcp_historical_data_mapping():
    # Test mapping of get_historical_stock_prices output
    with patch("screener.eodhd.load_eodhd_token", return_value="token123"), \
         patch("screener.eodhd_mcp.requests.post") as mock_mcp_post:
         
        mcp_resp_init = MagicMock()
        mcp_resp_init.headers = {"Mcp-Session-Id": "session123"}
        mcp_resp_init.text = 'data: {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}\n'
        
        mcp_resp_call = MagicMock()
        mcp_resp_call.headers = {"Mcp-Session-Id": "session123"}
        
        eod_data = [
            {"date": "2026-06-01", "open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0, "adjusted_close": 102.0, "volume": 1000}
        ]
        rpc_result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(eod_data)
                }
            ]
        }
        mcp_resp_call.text = f'data: {{"jsonrpc": "2.0", "id": 3, "result": {json.dumps(rpc_result)}}}\n'
        
        mock_mcp_post.side_effect = [mcp_resp_init, mcp_resp_init, mcp_resp_call]
        
        settings = Settings.from_env()
        client = EodHdClient(settings, backend="mcp")
        
        res = client.get_eod_historical_data("AAPL", "US", date.today(), force=True)
        assert len(res) == 1
        assert res[0]["close"] == 102.0
        assert res[0]["date"] == "2026-06-01"

def test_mcp_fundamentals_mapping():
    # Test mapping of get_fundamentals_data output
    with patch("screener.eodhd.load_eodhd_token", return_value="token123"), \
         patch("screener.eodhd_mcp.requests.post") as mock_mcp_post:
         
        mcp_resp_init = MagicMock()
        mcp_resp_init.headers = {"Mcp-Session-Id": "session123"}
        mcp_resp_init.text = 'data: {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}\n'
        
        mcp_resp_call = MagicMock()
        mcp_resp_call.headers = {"Mcp-Session-Id": "session123"}
        
        fund_data = {
            "General": {"Name": "Apple Inc", "Code": "AAPL"},
            "Highlights": {"MarketCapitalization": 3000000000000}
        }
        rpc_result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(fund_data)
                }
            ]
        }
        mcp_resp_call.text = f'data: {{"jsonrpc": "2.0", "id": 3, "result": {json.dumps(rpc_result)}}}\n'
        
        mock_mcp_post.side_effect = [mcp_resp_init, mcp_resp_init, mcp_resp_call]
        
        settings = Settings.from_env()
        client = EodHdClient(settings, backend="mcp")
        
        res = client.get_fundamentals("AAPL", "US", force=True)
        assert res["General"]["Name"] == "Apple Inc"
        assert res["Highlights"]["MarketCapitalization"] == 3000000000000

def test_diagnose_eodhd_ok(capsys):
    with patch("screener.cli.load_eodhd_token", return_value="token123"), \
         patch("screener.cli.requests.get") as mock_get, \
         patch("screener.eodhd_mcp.requests.post") as mock_mcp_post:
         
        mock_rest_resp = MagicMock()
        mock_rest_resp.status_code = 200
        mock_rest_resp.text = "standard eodhd"
        mock_get.return_value = mock_rest_resp
        
        mcp_resp_init = MagicMock()
        mcp_resp_init.headers = {"Mcp-Session-Id": "session123"}
        mcp_resp_init.text = 'data: {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}\n'
        
        mcp_resp_list = MagicMock()
        mcp_resp_list.headers = {"Mcp-Session-Id": "session123"}
        tools_list_result = {
            "tools": [
                {"name": "get_historical_stock_prices", "description": ""},
                {"name": "get_fundamentals_data", "description": ""}
            ]
        }
        mcp_resp_list.text = f'data: {{"jsonrpc": "2.0", "id": 3, "result": {json.dumps(tools_list_result)}}}\n'
        
        mcp_resp_prices = MagicMock()
        mcp_resp_prices.headers = {"Mcp-Session-Id": "session123"}
        prices_res = {"content": [{"type": "text", "text": '[{"date": "2026-06-01", "close": 100}]'}]}
        mcp_resp_prices.text = f'data: {{"jsonrpc": "2.0", "id": 4, "result": {json.dumps(prices_res)}}}\n'
        
        mcp_resp_fund = MagicMock()
        mcp_resp_fund.headers = {"Mcp-Session-Id": "session123"}
        fund_res = {"content": [{"type": "text", "text": '{"General": {"Name": "Apple Inc"}}'}]}
        mcp_resp_fund.text = f'data: {{"jsonrpc": "2.0", "id": 5, "result": {json.dumps(fund_res)}}}\n'
        
        mock_mcp_post.side_effect = [
            mcp_resp_init, mcp_resp_init,
            mcp_resp_list,
            mcp_resp_prices,
            mcp_resp_fund
        ]
        
        settings = Settings.from_env()
        from screener.cli import run_diagnose_eodhd
        exit_code = run_diagnose_eodhd(settings)
        assert exit_code == 0
        
        captured = capsys.readouterr()
        assert "REST_OK" in captured.out
        assert "MCP_OK" in captured.out
        assert "TOKEN_PROBABLY_VALID" in captured.out

def test_diagnose_eodhd_ko(capsys):
    with patch("screener.cli.load_eodhd_token", return_value="token123"), \
         patch("screener.cli.requests.get") as mock_get, \
         patch("screener.eodhd_mcp.requests.post") as mock_mcp_post:
         
        mock_dns_resp = MagicMock()
        mock_dns_resp.text = "block.opendns.com"
        mock_get.return_value = mock_dns_resp
        
        mock_mcp_post.side_effect = Exception("Connection timed out to MCP server")
        
        settings = Settings.from_env()
        from screener.cli import run_diagnose_eodhd
        exit_code = run_diagnose_eodhd(settings)
        assert exit_code == 0
        
        captured = capsys.readouterr()
        assert "REST_BLOCKED_OPENDNS" in captured.out
        assert "MCP_FAILED" in captured.out
        assert "TOKEN_NOT_TESTABLE_BECAUSE_REST_BLOCKED" in captured.out


# ----------------------------------------------------
# Tests for prefilter-higgons
# ----------------------------------------------------

def test_prefilter_higgons_pipeline(tmp_path):
    db_file = tmp_path / "infofin_prefilter.sqlite3"
    database = Database(db_file)
    database.initialize()
    
    database.upsert_issuers([
        Issuer("Company OK", "FR0000000001", "OK", "Euronext Paris"),
        Issuer("Company Short Hist", "FR0000000002", "SH", "Euronext Paris"),
        Issuer("Company Low Liq", "FR0000000003", "LL", "Euronext Paris"),
        Issuer("Company Bad Abs Mom", "FR0000000004", "AM", "Euronext Paris"),
        Issuer("Company Bad Rel Mom", "FR0000000005", "RM", "Euronext Paris"),
        Issuer("Company Big Cap", "FR0000000006", "BC", "Euronext Paris"),
        Issuer("Company ETF", "FR0000000007", "ETF", "Euronext Paris"),
        Issuer("Company Unknown Type", "FR0000000008", "UT", "Euronext Paris"),
    ])

    settings = Settings(
        db_path=db_file,
        data_dir=tmp_path / "raw",
        http_timeout_seconds=5,
        http_retries=1,
        http_backoff_factor=0.1,
        user_agent="Test",
        max_download_bytes=1000,
        amf_base_url="",
        amf_fallback_base_urls=(),
        amf_dataset="",
        amf_rows=10,
    )

    out_candidates = tmp_path / "prefilter_candidates.csv"
    out_json = tmp_path / "prefilter_results.json"

    with patch("screener.cli.EodHdClient") as mock_client_cls:
        client_instance = mock_client_cls.return_value
        
        client_instance.get_exchange_symbol_list.return_value = [
            {"Code": "OK", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000000001", "Currency": "EUR"},
            {"Code": "SH", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000000002", "Currency": "EUR"},
            {"Code": "LL", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000000003", "Currency": "EUR"},
            {"Code": "AM", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000000004", "Currency": "EUR"},
            {"Code": "RM", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000000005", "Currency": "EUR"},
            {"Code": "BC", "Exchange": "XPAR", "Type": "Common Stock", "Isin": "FR0000000006", "Currency": "EUR", "MarketCapitalization": 15000000000.0},
            {"Code": "ETF", "Exchange": "XPAR", "Type": "ETF", "Isin": "FR0000000007", "Currency": "EUR"},
            {"Code": "UT", "Exchange": "XPAR", "Type": None, "Isin": "FR0000000008", "Currency": "EUR"},
        ]
        
        client_instance.get_forex_rate.return_value = (1.0, False)

        prices_good = make_mock_prices(length_days=370, price_start=100.0, price_end=120.0)
        for p in prices_good:
            p["volume"] = 10000
            
        prices_short = make_mock_prices(length_days=100)
        
        prices_low_liq = make_mock_prices(length_days=370, price_start=100.0, price_end=120.0)
        for p in prices_low_liq:
            p["volume"] = 10
            
        prices_bad_abs = make_mock_prices(length_days=370, price_start=100.0, price_end=50.0)
        for p in prices_bad_abs:
            p["volume"] = 10000
            
        prices_bad_rel = make_mock_prices(length_days=370, price_start=100.0, price_end=100.0)
        for p in prices_bad_rel:
            p["volume"] = 10000

        prices_index = make_mock_prices(length_days=370, price_start=100.0, price_end=130.0)

        def get_eod_historical_data_mock(symbol, exchange, as_of_date, force=False):
            if symbol == "SH":
                return prices_short
            elif symbol == "LL":
                return prices_low_liq
            elif symbol == "AM":
                return prices_bad_abs
            elif symbol == "RM":
                return prices_bad_rel
            elif symbol == "FCHI":
                return prices_index
            else:
                return prices_good

        client_instance.get_eod_historical_data.side_effect = get_eod_historical_data_mock

        from screener.cli import run_prefilter_higgons
        ret = run_prefilter_higgons(
            database=database,
            settings=settings,
            market_arg="paris",
            as_of_date_arg=date(2026, 1, 5),
            output_csv=str(out_candidates),
            output_json=str(out_json),
            explain_rejections=True,
            min_daily_traded_eur=50000.0,
            max_market_cap_eur=12000000000.0,
            index_symbol="FCHI.INDX",
        )

        assert ret == 0

        assert out_candidates.exists()
        with out_candidates.open("r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            tickers = [row["ticker"] for row in reader]
            assert "OK" in tickers
            assert "UT" in tickers
            assert "SH" not in tickers
            assert len(tickers) == 2

            row_ok = [r for r in reader if r["ticker"] == "OK"][0]
            assert "MARKET_CAP_UNAVAILABLE" in row_ok["warnings"]
            assert row_ok["market_cap_status"] == "unavailable"

            row_ut = [r for r in reader if r["ticker"] == "UT"][0]
            assert "instrument_type_unknown" in row_ut["warnings"]
            assert row_ut["instrument_type_status"] == "unknown"

        expected_rejections_path = Path(str(out_candidates).replace("candidates", "rejections"))
        assert expected_rejections_path.exists()
        with expected_rejections_path.open("r", encoding="utf-8") as f:
            reader_rej = list(csv.DictReader(f))
            rej_by_ticker = {row["ticker"]: row for row in reader_rej}

            assert rej_by_ticker["SH"]["rejection_code"] == "INSUFFICIENT_PRICE_HISTORY"
            assert rej_by_ticker["LL"]["rejection_code"] == "INSUFFICIENT_LIQUIDITY"
            assert rej_by_ticker["AM"]["rejection_code"] == "ABSOLUTE_MOMENTUM_TOO_WEAK"
            assert rej_by_ticker["RM"]["rejection_code"] == "RELATIVE_MOMENTUM_TOO_WEAK"
            assert rej_by_ticker["BC"]["rejection_code"] == "MARKET_CAP_TOO_HIGH"
            assert rej_by_ticker["ETF"]["rejection_code"] == "EXCLUDED_INSTRUMENT_TYPE"

        assert out_json.exists()
        with out_json.open("r", encoding="utf-8") as f:
            json_data = json.load(f)
            counters = json_data["counters_by_filter"]
            assert counters["initial_local_count"] == 8
            assert counters["mapped_count"] == 8
            assert counters["passed_price_history_count"] == 6
            assert counters["insufficient_liquidity_count"] == 1
            assert counters["absolute_momentum_too_weak_count"] == 1
            assert counters["relative_momentum_too_weak_count"] == 1
            assert counters["market_cap_too_high_count"] == 1
            
            candidates_json = [c["ticker"] for c in json_data["candidates"]]
            assert "OK" in candidates_json
            assert "UT" in candidates_json
            assert len(candidates_json) == 2


def test_prefilter_mcp_forced_never_calls_rest():
    with patch("screener.eodhd.requests.get") as mock_rest_get, \
         patch("screener.eodhd.load_eodhd_token", return_value="fake_token_mcp"), \
         patch("screener.eodhd_mcp.requests.post") as mock_mcp_post:
         
        mock_mcp_resp = MagicMock()
        mock_mcp_resp.headers = {"Mcp-Session-Id": "mcp_session_prefilter"}
        mock_mcp_resp.text = 'data: {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}\n'
        mock_mcp_post.return_value = mock_mcp_resp
        
        settings = Settings.from_env()
        client = EodHdClient(settings, backend="mcp")
        
        assert client.backend == "mcp"
        assert not mock_rest_get.called


def test_prefilter_token_never_logged(tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    
    db_file = tmp_path / "infofin_token.sqlite3"
    database = Database(db_file)
    database.initialize()
    database.upsert_issuers([
        Issuer("Sanofi", "FR0000120578", "SAN", "Euronext Paris"),
    ])
    
    settings = Settings(
        db_path=db_file, data_dir=tmp_path / "raw", http_timeout_seconds=5, http_retries=1, http_backoff_factor=0.1,
        user_agent="Test", max_download_bytes=1000, amf_base_url="", amf_fallback_base_urls=(), amf_dataset="", amf_rows=10,
    )
    
    fake_token = "MY_SECRET_PREFILTER_TOKEN_123"
    from screener.logging_utils import setup_logging_redactor
    setup_logging_redactor(fake_token)
    
    with patch("screener.cli.load_eodhd_token", return_value=fake_token), \
         patch("screener.cli.EodHdClient") as mock_client_cls:
         
        client_instance = mock_client_cls.return_value
        client_instance.get_exchange_symbol_list.side_effect = Exception(f"EODHD connection error with token {fake_token}")
        
        try:
            from screener.cli import run_prefilter_higgons
            run_prefilter_higgons(
                database=database,
                settings=settings,
                market_arg="paris",
                as_of_date_arg=date(2026, 1, 5),
            )
        except Exception:
            pass
            
        for record in caplog.records:
            assert fake_token not in record.message
            if record.exc_text:
                assert fake_token not in record.exc_text
