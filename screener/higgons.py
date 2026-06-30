from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from config import Settings
from models import Issuer
from screener.config import ScreenerConfig
from screener.eodhd import EodHdClient

LOGGER = logging.getLogger("infofin.screener.higgons")

def safe_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None

def get_statement_for_date(statement_dict: dict[str, Any], target_date_str: str) -> dict[str, Any] | None:
    if not statement_dict:
        return None
    if target_date_str in statement_dict:
        return statement_dict[target_date_str]
    
    # Try to parse target date
    try:
        target_dt = date.fromisoformat(target_date_str)
    except ValueError:
        return None

    best_entry = None
    min_diff = None
    for date_str, entry in statement_dict.items():
        try:
            dt = date.fromisoformat(date_str)
            diff = abs((dt - target_dt).days)
            if diff <= 15:  # Allow 15 days misalignment
                if min_diff is None or diff < min_diff:
                    min_diff = diff
                    best_entry = entry
        except ValueError:
            continue
    return best_entry

class HiggonsScreener:
    def __init__(self, client: EodHdClient, config: ScreenerConfig | None = None) -> None:
        self.client = client
        self.config = config or ScreenerConfig()

    def screen_issuer(
        self,
        issuer: Issuer,
        as_of_date: date,
        index_perf_history: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Runs the full William Higgons screening pipeline for a single issuer.
        Returns a dict containing:
        - 'passed': bool
        - 'result': candidate data dict (if passed)
        - 'rejection': rejection details dict (if rejected)
        - 'warnings': list of warning strings
        - 'data_quality_report': dict
        """
        # State tracking
        passed_filters_count = 0
        warning_count = 0
        data_quality_score = 100
        warnings: list[str] = []
        missing_fields: list[str] = []
        raw_values_snapshot: dict[str, Any] = {}

        # Default outputs
        rejection_code = ""
        rejection_reason = ""
        rejected_at_filter = ""

        # Fetch EODHD fundamentals
        try:
            fundamentals = self.client.get_fundamentals(issuer.symbol, issuer.market, force=force)
        except Exception as exc:
            LOGGER.warning("Impossible de charger les fondamentaux pour %s: %s", issuer.symbol, exc)
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 0 - Données minimales",
                    "rejection_code": "MISSING_FUNDAMENTALS",
                    "rejection_reason": f"Erreur ou fondamentaux absents de l'API: {exc}",
                    "missing_fields": ["fundamentals_json"],
                    "raw_values_snapshot": {},
                },
                "warnings": ["Erreur lors du chargement des fondamentaux"],
                "data_quality_report": {"score": 0, "details": ["Missing fundamentals"]},
            }

        general = fundamentals.get("General", {})
        highlights = fundamentals.get("Highlights", {})
        valuation = fundamentals.get("Valuation", {})
        shares_stats = fundamentals.get("SharesStats", {})
        financials = fundamentals.get("Financials", {})

        # Check if financials exist
        if not financials or not isinstance(financials, dict):
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 0 - Données minimales",
                    "rejection_code": "MISSING_FUNDAMENTALS",
                    "rejection_reason": "Pas de section 'Financials' dans les fondamentaux",
                    "missing_fields": ["Financials"],
                    "raw_values_snapshot": {},
                },
                "warnings": ["Section Financials manquante"],
                "data_quality_report": {"score": 0, "details": ["Financials section is missing"]},
            }

        # -----------------
        # Level 0 Filters
        # -----------------
        # 0.1 Common Stock Check
        raw_type = general.get("Type", "Unknown")
        raw_values_snapshot["asset_type"] = raw_type
        if raw_type.lower() not in {"common stock", "stock", "shares", "equity"}:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 0 - Actions ordinaires",
                    "rejection_code": "NOT_COMMON_STOCK",
                    "rejection_reason": f"Type d'actif non pris en charge: {raw_type}",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": [],
                "data_quality_report": {"score": 100, "details": []},
            }
        passed_filters_count += 1

        # Get currency
        currency = general.get("CurrencyCode") or highlights.get("CurrencyCode") or "EUR"
        raw_values_snapshot["currency"] = currency

        # 0.2 Price History Check
        try:
            eod_data = self.client.get_eod_historical_data(issuer.symbol, issuer.market, as_of_date, force=force)
        except Exception as exc:
            LOGGER.warning("Impossible de charger les cours pour %s: %s", issuer.symbol, exc)
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 0 - Historique de cours",
                    "rejection_code": "INSUFFICIENT_PRICE_HISTORY",
                    "rejection_reason": f"Erreur lors de la récupération des prix: {exc}",
                    "missing_fields": ["price_history"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": ["Prix absents"],
                "data_quality_report": {"score": 0, "details": ["No price history"]},
            }

        valid_bars = sorted(
            [bar for bar in eod_data if bar.get("date") and bar["date"] <= as_of_date.isoformat()],
            key=lambda x: x["date"],
            reverse=True,
        )

        if len(valid_bars) < 30:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 0 - Historique de cours",
                    "rejection_code": "INSUFFICIENT_PRICE_HISTORY",
                    "rejection_reason": f"Historique de cours trop court ({len(valid_bars)} jours disponibles)",
                    "missing_fields": ["price_history"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": [],
                "data_quality_report": {"score": 50, "details": ["Insufficient price history count"]},
            }

        bar_end = valid_bars[0]
        last_price_date_str = bar_end["date"]
        last_price_date = date.fromisoformat(last_price_date_str)
        raw_values_snapshot["last_price_date"] = last_price_date_str

        # Find bar around 12 months before last_price_date
        target_start_dt = last_price_date - timedelta(days=365)
        best_start_bar = None
        min_diff_days = 9999
        for bar in valid_bars:
            try:
                dt = date.fromisoformat(bar["date"])
                diff = abs((dt - target_start_dt).days)
                if diff < min_diff_days:
                    min_diff_days = diff
                    best_start_bar = bar
            except ValueError:
                continue

        if best_start_bar is None or min_diff_days > 15:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 0 - Historique de cours",
                    "rejection_code": "INSUFFICIENT_PRICE_HISTORY",
                    "rejection_reason": "Pas de cours disponible il y a 12 mois (+/- 15 jours)",
                    "missing_fields": ["price_history_12m"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": [],
                "data_quality_report": {"score": 50, "details": ["No price 12m ago"]},
            }
        passed_filters_count += 1

        # 0.3 Fundamentals Check (Income, Balance, Cash Flow yearly dicts)
        income_stmt_dict = financials.get("Income_Statement", {}).get("yearly", {})
        balance_sheet_dict = financials.get("Balance_Sheet", {}).get("yearly", {})
        cash_flow_dict = financials.get("Cash_Flow", {}).get("yearly", {})

        available_dates = sorted(
            [d for d in income_stmt_dict.keys() if d <= as_of_date.isoformat()],
            reverse=True,
        )

        if len(available_dates) < 1:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 0 - Données fondamentales",
                    "rejection_code": "MISSING_FUNDAMENTALS",
                    "rejection_reason": "Aucun exercice annuel disponible dans l'historique des fondamentaux",
                    "missing_fields": ["Income_Statement.yearly"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": [],
                "data_quality_report": {"score": 0, "details": ["No yearly financials"]},
            }

        date_latest = available_dates[0]
        date_previous = available_dates[1] if len(available_dates) >= 2 else None
        raw_values_snapshot["last_fundamental_period"] = date_latest

        # Check if date_latest is older than 18 months
        latest_fund_dt = date.fromisoformat(date_latest)
        if (as_of_date - latest_fund_dt).days > 548:
            data_quality_score -= 20
            warnings.append("Données fondamentales de plus de 18 mois")
            warning_count += 1

        # Extract statement records
        inc_latest = income_stmt_dict[date_latest]
        inc_prev = income_stmt_dict[date_previous] if date_previous else None

        bal_latest = get_statement_for_date(balance_sheet_dict, date_latest)
        bal_prev = get_statement_for_date(balance_sheet_dict, date_previous) if date_previous else None

        cf_latest = get_statement_for_date(cash_flow_dict, date_latest)
        cf_prev = get_statement_for_date(cash_flow_dict, date_previous) if date_previous else None

        if not bal_latest or not cf_latest:
            missing = []
            if not bal_latest:
                missing.append("Balance_Sheet.latest")
            if not cf_latest:
                missing.append("Cash_Flow.latest")
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 0 - Données fondamentales",
                    "rejection_code": "MISSING_FUNDAMENTALS",
                    "rejection_reason": f"Fiches bilans ou flux de trésorerie manquantes pour le {date_latest}",
                    "missing_fields": missing,
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": 30, "details": ["Missing latest Balance Sheet or Cash Flow"]},
            }
        passed_filters_count += 1

        # 0.4 Market/Exchange / Currency check (Optional soft mapping filter)
        # We perform check in the main loop or here. Let's make it pass unless there's a serious mismatch.
        passed_filters_count += 1

        # Fetch forex rate to EUR
        try:
            forex_rate, is_fallback_used = self.client.get_forex_rate(currency, as_of_date, force=force)
            if is_fallback_used:
                data_quality_score -= 20
                warnings.append("Taux de change forex de secours utilisé")
                warning_count += 1
        except Exception as exc:
            LOGGER.warning("Impossible d'obtenir le taux de change pour %s: %s", currency, exc)
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 0 - Conversion devise",
                    "rejection_code": "MISSING_REQUIRED_FIELDS",
                    "rejection_reason": f"Erreur de résolution forex pour {currency}: {exc}",
                    "missing_fields": ["forex_rate"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": 20, "details": ["Forex resolution error"]},
            }

        raw_values_snapshot["forex_rate"] = forex_rate

        # -----------------
        # Level 1 Filters
        # -----------------
        # 1.1 Market Cap Check
        market_cap_local = safe_float(highlights.get("MarketCapitalization"))
        shares_outstanding = safe_float(shares_stats.get("SharesOutstanding")) or safe_float(highlights.get("SharesOutstanding"))
        
        if market_cap_local is None or market_cap_local <= 0:
            if shares_outstanding and shares_outstanding > 0:
                close_price = safe_float(bar_end.get("close"))
                if close_price:
                    market_cap_local = close_price * shares_outstanding
            
        if market_cap_local is None:
            missing_fields.append("MarketCapitalization")
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 1 - Capitalisation",
                    "rejection_code": "MISSING_REQUIRED_FIELDS",
                    "rejection_reason": "Capitalisation boursière impossible à calculer (cours ou actions manquantes)",
                    "missing_fields": ["MarketCapitalization"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": ["Missing Market Cap"]},
            }

        market_cap_eur = market_cap_local * forex_rate
        raw_values_snapshot["market_cap_eur"] = market_cap_eur

        if market_cap_eur >= self.config.max_market_cap_eur:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 1 - Capitalisation boursière",
                    "rejection_code": "MARKET_CAP_TOO_HIGH",
                    "rejection_reason": f"Capitalisation boursière de {market_cap_eur:,.0f} € supérieure au seuil de {self.config.max_market_cap_eur:,.0f} €",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }
        passed_filters_count += 1

        # 1.2 Liquidity Check
        three_months_bars = [
            bar for bar in valid_bars
            if (last_price_date - date.fromisoformat(bar["date"])).days <= 90
        ]
        
        if not three_months_bars:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 1 - Liquidité",
                    "rejection_code": "INSUFFICIENT_LIQUIDITY",
                    "rejection_reason": "Aucun cours de bourse sur les 3 derniers mois pour calculer la liquidité",
                    "missing_fields": ["price_history_3m"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": ["No price history in last 3 months"]},
            }

        sum_traded_value_local = 0.0
        for bar in three_months_bars:
            close_val = safe_float(bar.get("close")) or 0.0
            vol_val = safe_float(bar.get("volume")) or 0.0
            sum_traded_value_local += close_val * vol_val

        avg_daily_traded_value_local = sum_traded_value_local / len(three_months_bars)
        avg_daily_traded_value_eur = avg_daily_traded_value_local * forex_rate
        raw_values_snapshot["avg_daily_traded_value_eur_3m"] = avg_daily_traded_value_eur

        if avg_daily_traded_value_eur < self.config.min_daily_traded_value_eur:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 1 - Liquidité minimale",
                    "rejection_code": "INSUFFICIENT_LIQUIDITY",
                    "rejection_reason": f"Montant moyen échangé quotidien sur 3 mois de {avg_daily_traded_value_eur:,.0f} € inférieur au seuil de {self.config.min_daily_traded_value_eur:,.0f} €",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }
        passed_filters_count += 1

        # 1.3 Anti-falling knife Check
        close_end = safe_float(bar_end.get("adjusted_close")) or safe_float(bar_end.get("close")) or 0.0
        close_start = safe_float(best_start_bar.get("adjusted_close")) or safe_float(best_start_bar.get("close")) or 0.0

        if close_start <= 0 or close_end <= 0:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 1 - Momentum",
                    "rejection_code": "INSUFFICIENT_PRICE_HISTORY",
                    "rejection_reason": "Prix de départ ou d'arrivée invalide (<= 0)",
                    "missing_fields": ["price_history_values"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": ["Invalid stock price value"]},
            }

        stock_perf_12m = (close_end / close_start) - 1.0
        raw_values_snapshot["stock_perf_12m"] = stock_perf_12m

        # Compute index performance
        index_perf_12m = None
        relative_perf_12m = None
        relative_momentum_unavailable = False

        if index_perf_history:
            # Match dates
            # Get index price closest to last_price_date
            idx_end_bar = None
            idx_min_diff_end = 9999
            # Get index price closest to best_start_bar's date
            idx_start_bar = None
            idx_min_diff_start = 9999

            for bar in index_perf_history:
                try:
                    dt = date.fromisoformat(bar["date"])
                    diff_end = abs((dt - last_price_date).days)
                    if diff_end < idx_min_diff_end:
                        idx_min_diff_end = diff_end
                        idx_end_bar = bar

                    diff_start = abs((dt - date.fromisoformat(best_start_bar["date"])).days)
                    if diff_start < idx_min_diff_start:
                        idx_min_diff_start = diff_start
                        idx_start_bar = bar
                except ValueError:
                    continue

            if idx_end_bar and idx_start_bar and idx_min_diff_end <= 15 and idx_min_diff_start <= 15:
                idx_close_end = safe_float(idx_end_bar.get("adjusted_close")) or safe_float(idx_end_bar.get("close")) or 0.0
                idx_close_start = safe_float(idx_start_bar.get("adjusted_close")) or safe_float(idx_start_bar.get("close")) or 0.0
                if idx_close_start > 0:
                    index_perf_12m = (idx_close_end / idx_close_start) - 1.0
                    relative_perf_12m = stock_perf_12m - index_perf_12m
                else:
                    relative_momentum_unavailable = True
            else:
                relative_momentum_unavailable = True
        else:
            relative_momentum_unavailable = True

        raw_values_snapshot["index_perf_12m"] = index_perf_12m
        raw_values_snapshot["relative_perf_12m"] = relative_perf_12m

        if relative_momentum_unavailable:
            data_quality_score -= 30
            warnings.append("Performance relative indisponible")
            warning_count += 1
            # Produce warning, but do NOT reject
        else:
            if relative_perf_12m is not None and relative_perf_12m < self.config.min_relative_perf_12m:
                return {
                    "passed": False,
                    "rejection": {
                        "ticker": issuer.symbol,
                        "isin": issuer.isin,
                        "company_name": issuer.name,
                        "rejected_at_filter": "Niveau 1 - Anti-couteau qui tombe",
                        "rejection_code": "RELATIVE_MOMENTUM_TOO_WEAK",
                        "rejection_reason": f"Performance relative sur 12 mois de {relative_perf_12m * 100:.1f}% inférieure au seuil de {self.config.min_relative_perf_12m * 100:.1f}%",
                        "missing_fields": [],
                        "raw_values_snapshot": raw_values_snapshot,
                    },
                    "warnings": warnings,
                    "data_quality_report": {"score": data_quality_score, "details": []},
                }
        passed_filters_count += 1

        # -----------------
        # Level 2 Filters
        # -----------------
        # 2.1 PER Check
        net_income_latest = safe_float(inc_latest.get("netIncome"))
        raw_values_snapshot["net_income_latest"] = net_income_latest

        pe_ratio = safe_float(highlights.get("PERatio")) or safe_float(valuation.get("TrailingPE"))
        
        if pe_ratio is None or pe_ratio <= 0:
            if net_income_latest and net_income_latest > 0:
                pe_ratio = market_cap_local / net_income_latest
            else:
                pe_ratio = None

        raw_values_snapshot["pe_ratio"] = pe_ratio

        if pe_ratio is None:
            missing_fields.append("PERatio")
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 2 - PER",
                    "rejection_code": "NEGATIVE_OR_INVALID_PE",
                    "rejection_reason": "PER impossible à calculer ou invalide (Net Income manquant ou <= 0)",
                    "missing_fields": ["netIncome"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": ["Missing PER values"]},
            }

        if pe_ratio <= 0:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 2 - PER",
                    "rejection_code": "NEGATIVE_OR_INVALID_PE",
                    "rejection_reason": f"PER strictement négatif ou nul: {pe_ratio:.2f}",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }

        if pe_ratio >= self.config.max_pe_ratio:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 2 - PERRatio",
                    "rejection_code": "PE_TOO_HIGH",
                    "rejection_reason": f"PER de {pe_ratio:.2f} supérieur au seuil de {self.config.max_pe_ratio:.1f}",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }
        passed_filters_count += 1

        # 2.2 P/CF Check
        depr_amort = safe_float(cf_latest.get("depreciationAndAmortization")) or safe_float(cf_latest.get("depreciation")) or safe_float(cf_latest.get("amortization"))
        op_cash_flow = safe_float(cf_latest.get("totalCashFromOperatingActivities"))

        raw_values_snapshot["depreciation_and_amortization"] = depr_amort
        raw_values_snapshot["operating_cash_flow"] = op_cash_flow

        p_cf_source = ""
        cash_flow_higgons = None

        if depr_amort is not None:
            cash_flow_higgons = (net_income_latest or 0.0) + depr_amort
            p_cf_source = "net_income_plus_dna"
        elif op_cash_flow is not None:
            cash_flow_higgons = op_cash_flow
            p_cf_source = "operating_cash_flow_fallback"
            data_quality_score -= 10
            warnings.append("Amortissements absents, Operating Cash Flow utilisé en fallback pour P/CF")
            warning_count += 1
        
        raw_values_snapshot["p_cf_source"] = p_cf_source

        if cash_flow_higgons is None or cash_flow_higgons <= 0:
            missing = []
            if depr_amort is None:
                missing.append("depreciationAndAmortization")
            if op_cash_flow is None:
                missing.append("totalCashFromOperatingActivities")
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 2 - P/CF",
                    "rejection_code": "PCF_TOO_HIGH",
                    "rejection_reason": "P/CF impossible à calculer (Cash Flow Higgons manquant ou <= 0)",
                    "missing_fields": missing,
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": ["Missing P/CF inputs"]},
            }

        p_cf = market_cap_local / cash_flow_higgons
        raw_values_snapshot["p_cf"] = p_cf

        if p_cf >= self.config.max_p_cf_ratio or p_cf <= 0:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 2 - P/CF",
                    "rejection_code": "PCF_TOO_HIGH",
                    "rejection_reason": f"P/CF de {p_cf:.2f} supérieur ou égal au seuil de {self.config.max_p_cf_ratio:.1f} (ou négatif)",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }
        passed_filters_count += 1

        # -----------------
        # Level 3 Filters
        # -----------------
        # 3.1 Strict Positive Net Income
        if net_income_latest is None or net_income_latest <= 0:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 3 - Résultat Net",
                    "rejection_code": "NEGATIVE_NET_INCOME",
                    "rejection_reason": f"Résultat net de {net_income_latest} inférieur ou égal à 0",
                    "missing_fields": ["netIncome"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }
        passed_filters_count += 1

        # 3.2 Revenue Growth Check
        rev_latest = safe_float(inc_latest.get("totalRevenue")) or safe_float(inc_latest.get("revenue"))
        rev_prev = safe_float(inc_prev.get("totalRevenue")) if inc_prev else None
        if rev_prev is None and inc_prev:
            rev_prev = safe_float(inc_prev.get("revenue"))

        raw_values_snapshot["revenue_latest"] = rev_latest
        raw_values_snapshot["revenue_previous"] = rev_prev

        if rev_latest is None or rev_prev is None or rev_prev <= 0:
            missing = []
            if rev_latest is None:
                missing.append("totalRevenue.latest")
            if rev_prev is None:
                missing.append("totalRevenue.previous")
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 3 - Croissance CA",
                    "rejection_code": "NEGATIVE_REVENUE_GROWTH",
                    "rejection_reason": "Chiffre d'affaires de l'exercice actuel ou précédent manquant ou nul",
                    "missing_fields": missing,
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": ["Missing Revenue values"]},
            }

        revenue_growth_yoy = (rev_latest / rev_prev) - 1.0
        raw_values_snapshot["revenue_growth_yoy"] = revenue_growth_yoy

        if revenue_growth_yoy <= 0:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 3 - Croissance du Chiffre d'Affaires",
                    "rejection_code": "NEGATIVE_REVENUE_GROWTH",
                    "rejection_reason": f"Croissance du chiffre d'affaires négative ou nulle ({revenue_growth_yoy * 100:.2f}%)",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }
        passed_filters_count += 1

        # 3.3 EBIT Margin Check
        ebit_latest = safe_float(inc_latest.get("ebit")) or safe_float(inc_latest.get("operatingIncome"))
        raw_values_snapshot["ebit"] = ebit_latest

        if ebit_latest is None or rev_latest <= 0:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 3 - Marge EBIT",
                    "rejection_code": "EBIT_MARGIN_TOO_LOW",
                    "rejection_reason": "EBIT de l'exercice actuel manquant",
                    "missing_fields": ["ebit"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": ["Missing EBIT value"]},
            }

        ebit_margin = ebit_latest / rev_latest
        raw_values_snapshot["ebit_margin"] = ebit_margin

        if ebit_margin < self.config.min_ebit_margin:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 3 - Marge EBIT",
                    "rejection_code": "EBIT_MARGIN_TOO_LOW",
                    "rejection_reason": f"Marge EBIT de {ebit_margin * 100:.2f}% inférieure au seuil de {self.config.min_ebit_margin * 100:.1f}%",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }
        passed_filters_count += 1

        # 3.4 ROE Check
        total_equity_latest = safe_float(bal_latest.get("totalStockholderEquity")) or safe_float(bal_latest.get("totalEquity"))
        raw_values_snapshot["total_equity"] = total_equity_latest

        if total_equity_latest is None or total_equity_latest <= 0:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 3 - ROE",
                    "rejection_code": "ROE_TOO_LOW",
                    "rejection_reason": "Capitaux propres manquants ou négatifs",
                    "missing_fields": ["totalStockholderEquity"],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": ["Missing Equity value"]},
            }

        roe = net_income_latest / total_equity_latest
        raw_values_snapshot["roe"] = roe

        if roe < self.config.min_roe:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 3 - ROE",
                    "rejection_code": "ROE_TOO_LOW",
                    "rejection_reason": f"ROE de {roe * 100:.2f}% inférieur au seuil de {self.config.min_roe * 100:.1f}%",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }
        passed_filters_count += 1

        # 3.5 ROCE Check
        short_debt = safe_float(bal_latest.get("shortTermDebt")) or 0.0
        long_debt = safe_float(bal_latest.get("longTermDebt")) or 0.0
        cash = safe_float(bal_latest.get("cashAndEquivalents")) or safe_float(bal_latest.get("cash")) or 0.0

        raw_values_snapshot["short_term_debt"] = short_debt
        raw_values_snapshot["long_term_debt"] = long_debt
        raw_values_snapshot["cash_and_equivalents"] = cash

        net_debt = short_debt + long_debt - cash
        raw_values_snapshot["net_debt"] = net_debt
        net_cash_position = net_debt < 0

        roce_source = ""
        roce = None

        # Check if we can compute principal
        if total_equity_latest is not None:
            denom = total_equity_latest + net_debt
            if denom > 0:
                roce = ebit_latest / denom
                roce_source = "equity_plus_net_debt"

        if roce is None:
            # Try fallback
            assets = safe_float(bal_latest.get("totalAssets"))
            current_liab = safe_float(bal_latest.get("totalCurrentLiabilities")) or safe_float(bal_latest.get("currentLiabilities"))
            raw_values_snapshot["total_assets"] = assets
            raw_values_snapshot["total_current_liabilities"] = current_liab

            if assets is not None and current_liab is not None:
                denom = assets - current_liab
                if denom > 0:
                    roce = ebit_latest / denom
                    roce_source = "assets_minus_current_liabilities"
                    data_quality_score -= 10
                    warnings.append("Capitaux propres ou dette absents, ROCE calculé via Actif Total - Passif Court Terme")
                    warning_count += 1

        raw_values_snapshot["roce_source"] = roce_source

        if roce is None:
            missing = ["totalStockholderEquity", "shortTermDebt", "longTermDebt", "cashAndEquivalents"]
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 3 - ROCE",
                    "rejection_code": "ROCE_TOO_LOW",
                    "rejection_reason": "ROCE impossible à calculer (champs manquants ou dénominateur <= 0)",
                    "missing_fields": missing,
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": ["Missing ROCE inputs"]},
            }

        raw_values_snapshot["roce"] = roce

        if roce < self.config.min_roce:
            return {
                "passed": False,
                "rejection": {
                    "ticker": issuer.symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Niveau 3 - ROCE",
                    "rejection_code": "ROCE_TOO_LOW",
                    "rejection_reason": f"ROCE de {roce * 100:.2f}% inférieur au seuil de {self.config.min_roce * 100:.1f}%",
                    "missing_fields": [],
                    "raw_values_snapshot": raw_values_snapshot,
                },
                "warnings": warnings,
                "data_quality_report": {"score": data_quality_score, "details": []},
            }
        passed_filters_count += 1

        # -----------------
        # Level 4 Filters (Debt)
        # -----------------
        # 4.1 Net Debt / EBITDA Check
        ebitda = safe_float(inc_latest.get("ebitda"))
        if ebitda is None:
            # fallback
            if ebit_latest is not None and depr_amort is not None:
                ebitda = ebit_latest + depr_amort
        
        raw_values_snapshot["ebitda"] = ebitda

        net_debt_to_ebitda = None
        if net_cash_position:
            # Automatically passed
            pass
        else:
            if ebitda is None or ebitda <= 0:
                return {
                    "passed": False,
                    "rejection": {
                        "ticker": issuer.symbol,
                        "isin": issuer.isin,
                        "company_name": issuer.name,
                        "rejected_at_filter": "Niveau 4 - Dette",
                        "rejection_code": "EBITDA_INVALID",
                        "rejection_reason": "EBITDA invalide (<= 0 ou manquant) alors que la dette nette est positive",
                        "missing_fields": ["ebitda"],
                        "raw_values_snapshot": raw_values_snapshot,
                    },
                    "warnings": warnings,
                    "data_quality_report": {"score": data_quality_score, "details": ["Missing or invalid EBITDA"]},
                }
            
            net_debt_to_ebitda = net_debt / ebitda
            raw_values_snapshot["net_debt_to_ebitda"] = net_debt_to_ebitda

            if net_debt_to_ebitda >= self.config.max_net_debt_to_ebitda:
                return {
                    "passed": False,
                    "rejection": {
                        "ticker": issuer.symbol,
                        "isin": issuer.isin,
                        "company_name": issuer.name,
                        "rejected_at_filter": "Niveau 4 - Dette Nette / EBITDA",
                        "rejection_code": "NET_DEBT_EBITDA_TOO_HIGH",
                        "rejection_reason": f"Ratio Dette Nette / EBITDA de {net_debt_to_ebitda:.2f} supérieur ou égal au seuil de {self.config.max_net_debt_to_ebitda:.1f}",
                        "missing_fields": [],
                        "raw_values_snapshot": raw_values_snapshot,
                    },
                    "warnings": warnings,
                    "data_quality_report": {"score": data_quality_score, "details": []},
                }
        passed_filters_count += 1

        # Cap data quality score at 0
        data_quality_score = max(0, data_quality_score)

        # Build Candidate Result
        cand_data = {
            "ticker": issuer.symbol,
            "isin": issuer.isin,
            "company_name": issuer.name,
            "exchange": general.get("Exchange", issuer.market),
            "country": general.get("Country", "Unknown"),
            "currency": currency,
            "market_cap_eur": market_cap_eur,
            "avg_daily_traded_value_eur_3m": avg_daily_traded_value_eur,
            "stock_perf_12m": stock_perf_12m,
            "index_perf_12m": index_perf_12m,
            "relative_perf_12m": relative_perf_12m,
            "pe_ratio": pe_ratio,
            "p_cf": p_cf,
            "p_cf_source": p_cf_source,
            "revenue_growth_yoy": revenue_growth_yoy,
            "net_income": net_income_latest,
            "ebit_margin": ebit_margin,
            "roe": roe,
            "roce": roce,
            "roce_source": roce_source,
            "net_debt": net_debt,
            "ebitda": ebitda,
            "net_debt_to_ebitda": net_debt_to_ebitda,
            "passed_filters_count": passed_filters_count,
            "warning_count": warning_count,
            "data_quality_score": data_quality_score,
            "last_price_date": last_price_date_str,
            "last_fundamental_period": date_latest,
            "source_summary": f"EODHD fundamentals & EOD prices. Forex: {currency}->EUR.",
            "net_cash_position": net_cash_position,
            # AI placeholders
            "is_diversified_holding": None,
            "recent_major_acquisition": None,
            "family_ownership": None,
            "qualitative_price_drop_explanation": None,
            "exceptional_events_in_accounts": None,
            "business_readability": None,
        }

        return {
            "passed": True,
            "result": cand_data,
            "warnings": warnings,
            "data_quality_report": {
                "score": data_quality_score,
                "details": warnings,
            },
        }
