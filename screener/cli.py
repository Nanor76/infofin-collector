from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from config import Settings
from db import Database
from load_watchlist import normalize_market
from models import Issuer
from screener.config import ScreenerConfig
from screener.eodhd import EodHdClient, load_eodhd_token, scrub_token
from screener.higgons import HiggonsScreener

LOGGER = logging.getLogger("infofin.screener.cli")

MARKET_TO_EODHD_EXCHANGE = {
    "Euronext Paris": "XPAR",
    "Euronext Amsterdam": "XAMS",
    "Euronext Brussels": "XBRU",
    "Euronext Milan": "XMIL",
    "Euronext Star Milan": "XMIL",
    "Euronext Growth Milan": "XMIL",
    "Euronext MIV Milan": "XMIL",
    "Oslo Børs": "XOSL",
    "Euronext Lisbon": "XLIS",
    "Euronext Dublin": "XDUB",
}

def run_screen_higgons(
    database: Database,
    settings: Settings,
    market_arg: str,
    exchange_code_arg: str | None = None,
    as_of_date_arg: date | None = None,
    force: bool = False,
    limit: int | None = None,
    output_csv: str | None = None,
    output_json: str | None = None,
    explain_rejections: bool = False,
    min_daily_traded_eur: float | None = None,
    index_symbol: str | None = None,
    eodhd_backend: str = "auto",
) -> int:
    # 1. Normalize market
    normalized_market = normalize_market(market_arg)
    LOGGER.info("Market normalized to: %s", normalized_market)

    # 2. Map exchange code
    exchange_code = exchange_code_arg
    if not exchange_code:
        exchange_code = MARKET_TO_EODHD_EXCHANGE.get(normalized_market)
    if not exchange_code:
        LOGGER.error("Impossible de déterminer le code d'échange EODHD pour le marché: %s. Veuillez spécifier --exchange-code.", normalized_market)
        return 1

    # 3. Setup dates
    as_of_date = as_of_date_arg or date.today()
    LOGGER.info("Running screener as of date: %s", as_of_date)

    # 4. Initialize clients
    try:
        client = EodHdClient(settings, backend=eodhd_backend)
    except Exception as exc:
        LOGGER.error("Erreur lors de l'initialisation du client EODHD: %s", exc)
        return 1

    config = ScreenerConfig()
    if min_daily_traded_eur is not None:
        config = ScreenerConfig(
            min_daily_traded_value_eur=min_daily_traded_eur
        )
    screener = HiggonsScreener(client, config)

    # 5. Load issuers from DB
    all_issuers = database.list_issuers()
    # Filter by market (case-insensitive substring match)
    market_issuers = [
        iss for iss in all_issuers
        if normalized_market.lower() in iss.market.lower()
    ]
    initial_tickers_count = len(market_issuers)
    LOGGER.info("Found %d issuers in local DB matching market '%s'", initial_tickers_count, normalized_market)

    if initial_tickers_count == 0:
        LOGGER.warning("Aucun émetteur trouvé pour le marché %s", normalized_market)
        return 0

    # Apply limit if test mode
    if limit is not None:
        LOGGER.info("Limiting analysis to first %d issuers", limit)
        market_issuers = market_issuers[:limit]

    # 6. Fetch Exchange Ticker List from EODHD
    try:
        exchange_symbol_list = client.get_exchange_symbol_list(exchange_code, force=force)
        LOGGER.info("Fetched %d tickers from EODHD for exchange %s", len(exchange_symbol_list), exchange_code)
    except Exception as exc:
        LOGGER.error("Impossible de charger la liste des symboles EODHD pour l'échange %s: %s", exchange_code, exc)
        return 1

    eodhd_by_isin = {
        item["Isin"].upper().strip(): item
        for item in exchange_symbol_list
        if item.get("Isin")
    }
    eodhd_by_symbol = {
        item["Code"].upper().strip(): item
        for item in exchange_symbol_list
    }

    # 7. Fetch Index History if index_symbol is provided
    index_perf_history = None
    if index_symbol:
        LOGGER.info("Fetching EOD history for index: %s", index_symbol)
        parts = index_symbol.split(".")
        idx_sym = parts[0]
        idx_ex = parts[1] if len(parts) > 1 else "INDX"
        try:
            index_perf_history = client.get_eod_historical_data(idx_sym, idx_ex, as_of_date, force=force)
        except Exception as exc:
            LOGGER.warning("Impossible de charger l'historique de l'indice %s: %s. La performance relative sera indisponible.", index_symbol, exc)

    # 8. Main screen loop
    candidates = []
    rejections = []
    warnings_log = []

    # Diagnostic counters
    cnt_level0_pass = 0
    cnt_level1_pass = 0
    cnt_level2_pass = 0
    cnt_level3_pass = 0
    cnt_level4_pass = 0

    rejection_reasons = []

    for issuer in market_issuers:
        eodhd_item = eodhd_by_isin.get(issuer.isin.upper().strip())
        if not eodhd_item:
            eodhd_item = eodhd_by_symbol.get(issuer.symbol.upper().strip())

        if not eodhd_item:
            reason = "Non trouvé dans la liste des tickers EODHD"
            rejections.append({
                "ticker": issuer.symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Niveau 0 - Données minimales",
                "rejection_code": "MISSING_REQUIRED_FIELDS",
                "rejection_reason": reason,
                "missing_fields": ["eodhd_ticker_mapping"],
                "raw_values_snapshot": {},
            })
            rejection_reasons.append("MISSING_REQUIRED_FIELDS")
            continue

        eod_symbol = eodhd_item["Code"]
        eod_exchange = eodhd_item["Exchange"]
        
        screen_target = Issuer(
            name=issuer.name,
            isin=issuer.isin,
            symbol=eod_symbol,
            market=eod_exchange,
            id=issuer.id,
        )

        try:
            screen_res = screener.screen_issuer(
                screen_target,
                as_of_date,
                index_perf_history=index_perf_history,
                force=force,
            )
        except Exception as exc:
            LOGGER.error("Erreur inattendue lors du screening de %s: %s", issuer.symbol, exc)
            rejections.append({
                "ticker": issuer.symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Niveau 0 - Données minimales",
                "rejection_code": "MISSING_REQUIRED_FIELDS",
                "rejection_reason": f"Erreur de calcul screener: {exc}",
                "missing_fields": ["screener_execution"],
                "raw_values_snapshot": {},
            })
            rejection_reasons.append("MISSING_REQUIRED_FIELDS")
            continue

        for w in screen_res.get("warnings", []):
            warnings_log.append(f"{issuer.symbol}: {w}")

        if screen_res["passed"]:
            candidates.append(screen_res["result"])
            cnt_level0_pass += 1
            cnt_level1_pass += 1
            cnt_level2_pass += 1
            cnt_level3_pass += 1
            cnt_level4_pass += 1
        else:
            rej = screen_res["rejection"]
            rejections.append(rej)
            rejection_reasons.append(rej["rejection_code"])
            
            filter_level = rej.get("rejected_at_filter", "")
            if "Niveau 0" not in filter_level:
                cnt_level0_pass += 1
                if "Niveau 1" not in filter_level:
                    cnt_level1_pass += 1
                    if "Niveau 2" not in filter_level:
                        cnt_level2_pass += 1
                        if "Niveau 3" not in filter_level:
                            cnt_level3_pass += 1

    # 9. Output generation
    candidates_count = len(candidates)
    rejections_count = len(rejections)

    out_csv_path = output_csv or f"data/screeners/higgons_candidates_{market_arg}_{as_of_date}.csv"
    
    if candidates:
        csv_file_path = Path(out_csv_path)
        csv_file_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_file_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(candidates[0].keys()))
            writer.writeheader()
            writer.writerows(candidates)
        LOGGER.info("Saved candidates CSV to %s", csv_file_path)
    else:
        dummy = {
            "ticker": "", "isin": "", "company_name": "", "exchange": "", "country": "", "currency": "",
            "market_cap_eur": 0, "avg_daily_traded_value_eur_3m": 0, "stock_perf_12m": 0, "index_perf_12m": 0,
            "relative_perf_12m": 0, "pe_ratio": 0, "p_cf": 0, "p_cf_source": "", "revenue_growth_yoy": 0,
            "net_income": 0, "ebit_margin": 0, "roe": 0, "roce": 0, "roce_source": "", "net_debt": 0,
            "ebitda": 0, "net_debt_to_ebitda": 0, "passed_filters_count": 0, "warning_count": 0,
            "data_quality_score": 0, "last_price_date": "", "last_fundamental_period": "", "source_summary": "",
            "net_cash_position": False, "is_diversified_holding": None, "recent_major_acquisition": None,
            "family_ownership": None, "qualitative_price_drop_explanation": None, "exceptional_events_in_accounts": None,
            "business_readability": None
        }
        csv_file_path = Path(out_csv_path)
        csv_file_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_file_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(dummy.keys()))
            writer.writeheader()
        LOGGER.info("Saved empty candidates CSV to %s", csv_file_path)

    rej_csv_path = None
    if explain_rejections:
        rej_csv_path = out_csv_path.replace("candidates", "rejections")
        if rej_csv_path == out_csv_path:
            rej_csv_path = out_csv_path + ".rejections.csv"
        
        csv_rej_file = Path(rej_csv_path)
        csv_rej_file.parent.mkdir(parents=True, exist_ok=True)
        if rejections:
            csv_rejections = []
            for r in rejections:
                csv_rejections.append({
                    "ticker": r["ticker"],
                    "isin": r["isin"],
                    "company_name": r["company_name"],
                    "rejected_at_filter": r["rejected_at_filter"],
                    "rejection_code": r["rejection_code"],
                    "rejection_reason": r["rejection_reason"],
                    "missing_fields": ",".join(r["missing_fields"]),
                    "raw_values_snapshot": json.dumps(r["raw_values_snapshot"], ensure_ascii=False),
                })

            with csv_rej_file.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["ticker", "isin", "company_name", "rejected_at_filter", "rejection_code", "rejection_reason", "missing_fields", "raw_values_snapshot"])
                writer.writeheader()
                writer.writerows(csv_rejections)
        else:
            with csv_rej_file.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["ticker", "isin", "company_name", "rejected_at_filter", "rejection_code", "rejection_reason", "missing_fields", "raw_values_snapshot"])
                writer.writeheader()
        LOGGER.info("Saved rejections CSV to %s", csv_rej_file)

    if output_json:
        json_file_path = Path(output_json)
        json_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        json_data = {
            "metadata": {
                "market": market_arg,
                "normalized_market": normalized_market,
                "exchange_code": exchange_code,
                "as_of_date": as_of_date.isoformat(),
                "execution_time": datetime.now().isoformat(),
                "force": force,
                "limit": limit,
                "index_symbol": index_symbol,
                "eodhd_backend": eodhd_backend,
            },
            "thresholds": {
                "max_market_cap_eur": config.max_market_cap_eur,
                "min_daily_traded_value_eur": config.min_daily_traded_value_eur,
                "min_relative_perf_12m": config.min_relative_perf_12m,
                "max_pe_ratio": config.max_pe_ratio,
                "max_p_cf_ratio": config.max_p_cf_ratio,
                "min_ebit_margin": config.min_ebit_margin,
                "min_roe": config.min_roe,
                "min_roce": config.min_roce,
                "max_net_debt_to_ebitda": config.max_net_debt_to_ebitda,
            },
            "candidates": candidates,
            "rejections": rejections,
            "warnings": warnings_log,
            "data_quality_report": [
                {
                    "ticker": c["ticker"],
                    "isin": c["isin"],
                    "score": c["data_quality_score"],
                    "details": [w for w in warnings_log if w.startswith(c["ticker"] + ":")]
                }
                for c in candidates
            ]
        }
        json_file_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Saved structured JSON output to %s", json_file_path)

    # 10. Display Diagnostics
    print("--------------------------------------------------")
    print("DIAGNOSTIC DU SCREENER (WILLIAM HIGGONS)")
    print("--------------------------------------------------")
    print(f"Nombre de tickers initiaux            : {initial_tickers_count}")
    print(f"Nombre après nettoyage de l'univers : {cnt_level0_pass}")
    print(f"Nombre après cotations                : {cnt_level1_pass}")
    print(f"Nombre après valorisation             : {cnt_level2_pass}")
    print(f"Nombre après qualité / rentabilité    : {cnt_level3_pass}")
    print(f"Nombre final de candidats             : {candidates_count}")
    print("--------------------------------------------------")
    
    rejection_counter = Counter(rejection_reasons)
    print("Top 10 raisons de rejet :")
    for code, count in rejection_counter.most_common(10):
        print(f"  - {code}: {count}")
    print("--------------------------------------------------")
    print("Chemins des fichiers générés :")
    print(f"  - Candidats CSV : {out_csv_path}")
    if explain_rejections and rej_csv_path:
        print(f"  - Rejets CSV    : {rej_csv_path}")
    if output_json:
        print(f"  - Sortie JSON   : {output_json}")
    print("--------------------------------------------------")

    return 0

def run_diagnose_eodhd(settings: Settings) -> int:
    print("==================================================")
    print("DIAGNOSTIC DE CONNEXION EODHD (REST, OpenDNS, MCP)")
    print("==================================================")

    # 1. Lecture du token
    print("1. Lecture du token...")
    try:
        token = load_eodhd_token()
        print(f"   [OK] Token lu avec succès (longueur: {len(token)} caractères).")
        from screener.logging_utils import setup_logging_redactor
        setup_logging_redactor(token)
    except Exception as exc:
        print(f"   [ERROR] Échec de la lecture du token: {exc}")
        return 1

    # 2. Détection OpenDNS / Cisco Umbrella
    print("\n2. Détection du filtrage OpenDNS / Cisco Umbrella...")
    is_blocked = False
    try:
        import urllib3
        with urllib3.warnings.catch_warnings():
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            test_resp = requests.get("https://eodhd.com", verify=False, timeout=3.0)
            if "block.opendns.com" in test_resp.text or "opendns" in test_resp.text:
                is_blocked = True
    except Exception:
        pass

    if is_blocked:
        print("   [BLOCKED] La connexion REST vers eodhd.com est interceptée par OpenDNS/Cisco Umbrella.")
    else:
        print("   [OK] Pas de blocage OpenDNS détecté sur eodhd.com.")

    # 3. Test REST avec vrai token
    print("\n3. Test API REST avec le token réel...")
    rest_ok = False
    token_valid = False
    if not is_blocked:
        try:
            url = "https://eodhd.com/api/exchange-symbol-list/PA"
            params = {"api_token": token, "fmt": "json"}
            resp = requests.get(url, params=params, timeout=5.0, verify=settings.http_verify_ssl)
            if resp.status_code == 200:
                rest_ok = True
                token_valid = True
                print("   [OK] Connexion REST réussie, token valide.")
            elif resp.status_code in (401, 403):
                print(f"   [ERROR] Erreur d'authentification REST (status {resp.status_code}). Le token est probablement invalide ou l'abonnement est expiré.")
            else:
                print(f"   [ERROR] Erreur REST: Code statut {resp.status_code}")
        except Exception as exc:
            print(f"   [ERROR] Échec de la requête REST: {scrub_token(str(exc), token)}")
    else:
        print("   [SKIP] Ignoré car REST est bloqué par OpenDNS.")

    # 4. Test REST avec faux token (redaction check)
    print("\n4. Test API REST avec un token fictif (validation de la redaction)...")
    if not is_blocked:
        try:
            fake_token = "FAKETOKEN12345"
            url = "https://eodhd.com/api/exchange-symbol-list/PA"
            params = {"api_token": fake_token, "fmt": "json"}
            from screener.logging_utils import setup_logging_redactor
            setup_logging_redactor(fake_token)
            resp = requests.get(url, params=params, timeout=5.0, verify=settings.http_verify_ssl)
            print(f"   [OK] Réponse reçue du serveur pour le faux token (status: {resp.status_code}).")
        except Exception as exc:
            err_str = scrub_token(str(exc), token)
            err_str = scrub_token(err_str, "FAKETOKEN12345")
            print(f"   [OK] Exception capturée (redactée): {err_str}")
    else:
        print("   [SKIP] Ignoré car REST est bloqué par OpenDNS.")

    # 5. Connexion MCP
    print("\n5. Connexion au serveur MCP EODHD...")
    mcp_ok = False
    mcp_tools_list = []
    try:
        from screener.eodhd_mcp import EodhdMcpProvider
        mcp_provider = EodhdMcpProvider(settings, token)
        mcp_provider._ensure_session()
        mcp_ok = True
        print(f"   [OK] Session MCP initialisée avec succès. Session ID: {mcp_provider.session_id}")
    except Exception as exc:
        print(f"   [ERROR] Échec de la connexion MCP: {scrub_token(str(exc), token)}")

    # 6. Liste des outils MCP
    if mcp_ok:
        print("\n6. Récupération des outils MCP disponibles...")
        try:
            res_tools = mcp_provider._mcp_request("tools/list", {})
            mcp_tools_list = [t["name"] for t in res_tools.get("tools", [])]
            print(f"   [OK] {len(mcp_tools_list)} outils MCP trouvés.")
            print(f"   Outils: {', '.join(mcp_tools_list[:10])}...")
        except Exception as exc:
            print(f"   [ERROR] Échec de la récupération des outils MCP: {scrub_token(str(exc), token)}")

    # 7. Test de prix historique via MCP
    mcp_price_ok = False
    if mcp_ok and "get_historical_stock_prices" in mcp_tools_list:
        print("\n7. Test de récupération des prix historiques via MCP (AAPL.US)...")
        try:
            res = mcp_provider.get_eod_historical_data("AAPL", "US", date.today() - timedelta(days=5), force=True)
            if res and isinstance(res, list):
                mcp_price_ok = True
                print(f"   [OK] {len(res)} barres de prix historiques récupérées.")
                print(f"   Dernière clôture: {res[-1] if res else 'Aucune'}")
            else:
                print(f"   [ERROR] Format de réponse invalide: {type(res)}")
        except Exception as exc:
            print(f"   [ERROR] Échec du test de prix historique: {scrub_token(str(exc), token)}")

    # 8. Test fondamentaux via MCP
    mcp_fund_ok = False
    if mcp_ok and "get_fundamentals_data" in mcp_tools_list:
        print("\n8. Test de récupération des fondamentaux via MCP (AAPL.US)...")
        try:
            res = mcp_provider.get_fundamentals("AAPL", "US", force=True)
            if res and isinstance(res, dict) and "General" in res:
                mcp_fund_ok = True
                print(f"   [OK] Données fondamentales récupérées avec succès.")
                print(f"   Nom société: {res['General'].get('Name')}")
            else:
                print(f"   [ERROR] Format de réponse invalide ou données manquantes.")
        except Exception as exc:
            print(f"   [ERROR] Échec du test des fondamentaux: {scrub_token(str(exc), token)}")

    # Conclusion
    print("\n==================================================")
    print("RÉSUMÉ DU DIAGNOSTIC / CONCLUSION")
    print("==================================================")
    
    if rest_ok:
        print("- REST Status            : REST_OK")
    elif is_blocked:
        print("- REST Status            : REST_BLOCKED_OPENDNS")
    else:
        print("- REST Status            : REST_FAILED")
        
    if mcp_ok and mcp_price_ok and mcp_fund_ok:
        print("- MCP Status             : MCP_OK")
    else:
        print("- MCP Status             : MCP_FAILED")

    if token_valid:
        print("- Token Validity         : TOKEN_PROBABLY_VALID")
    elif is_blocked:
        print("- Token Validity         : TOKEN_NOT_TESTABLE_BECAUSE_REST_BLOCKED")
    else:
        print("- Token Validity         : TOKEN_INVALID_OR_NO_ABONNEMENT")
        
    print("==================================================")
    return 0


def calculate_perf_12m(
    valid_bars: list[dict[str, Any]],
    as_of_date: date,
) -> tuple[float | None, str | None, str | None, list[str]]:
    if not valid_bars:
        return None, None, None, ["No price history"]

    bar_end = valid_bars[0]
    last_price_date_str = bar_end["date"]
    try:
        last_price_date = date.fromisoformat(last_price_date_str)
    except ValueError:
        return None, None, None, ["Invalid date format in history"]

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
        return None, last_price_date_str, None, ["No start price within 15 days of 12 months ago"]

    close_end = float(bar_end.get("adjusted_close") or bar_end.get("close") or 0.0)
    close_start = float(best_start_bar.get("adjusted_close") or best_start_bar.get("close") or 0.0)

    if close_start <= 0 or close_end <= 0:
        return None, last_price_date_str, best_start_bar["date"], ["Invalid price values <= 0"]

    perf = (close_end / close_start) - 1.0
    return perf, last_price_date_str, best_start_bar["date"], []


def run_prefilter_higgons(
    database: Database,
    settings: Settings,
    market_arg: str,
    exchange_code_arg: str | None = None,
    as_of_date_arg: date | None = None,
    force: bool = False,
    limit: int | None = None,
    output_csv: str | None = None,
    output_json: str | None = None,
    explain_rejections: bool = False,
    min_daily_traded_eur: float = 50000.0,
    max_market_cap_eur: float = 12000000000.0,
    index_symbol: str | None = None,
    eodhd_backend: str = "auto",
) -> int:
    # 1. Normalize market
    normalized_market = normalize_market(market_arg)
    LOGGER.info("Prefilter: Market normalized to: %s", normalized_market)

    # 2. Map exchange code
    exchange_code = exchange_code_arg
    if not exchange_code:
        exchange_code = MARKET_TO_EODHD_EXCHANGE.get(normalized_market)
    if not exchange_code:
        LOGGER.error("Prefilter: Impossible de déterminer le code d'échange EODHD pour: %s", normalized_market)
        return 1

    # 3. Setup date
    as_of_date = as_of_date_arg or date.today()
    LOGGER.info("Prefilter: Running as of date: %s", as_of_date)

    # 4. Initialize client
    try:
        client = EodHdClient(settings, backend=eodhd_backend)
    except Exception as exc:
        LOGGER.error("Prefilter: Erreur lors de l'initialisation du client EODHD: %s", exc)
        return 1

    # 5. Load issuers from DB
    all_issuers = database.list_issuers()
    market_issuers = [
        iss for iss in all_issuers
        if normalized_market.lower() in iss.market.lower()
    ]
    initial_local_count = len(market_issuers)
    LOGGER.info("Prefilter: Found %d issuers in local DB matching market '%s'", initial_local_count, normalized_market)

    if initial_local_count == 0:
        LOGGER.warning("Prefilter: Aucun émetteur trouvé pour le marché %s", normalized_market)
        return 0

    if limit is not None:
        LOGGER.info("Prefilter: Limiting analysis to first %d issuers", limit)
        market_issuers = market_issuers[:limit]

    # 6. Fetch Exchange Ticker List from EODHD
    try:
        exchange_symbol_list = client.get_exchange_symbol_list(exchange_code, force=force)
        LOGGER.info("Prefilter: Fetched %d tickers from EODHD for exchange %s", len(exchange_symbol_list), exchange_code)
    except Exception as exc:
        LOGGER.error("Prefilter: Impossible de charger la liste des symboles EODHD pour %s: %s", exchange_code, exc)
        return 1

    eodhd_by_isin = {
        item["Isin"].upper().strip(): item
        for item in exchange_symbol_list
        if item.get("Isin")
    }
    eodhd_by_symbol = {
        item["Code"].upper().strip(): item
        for item in exchange_symbol_list
    }

    # 7. Fetch Index History if index_symbol is provided
    index_perf_history = None
    index_perf_12m = None
    if index_symbol:
        LOGGER.info("Prefilter: Fetching EOD history for index: %s", index_symbol)
        parts = index_symbol.split(".")
        idx_sym = parts[0]
        idx_ex = parts[1] if len(parts) > 1 else "INDX"
        try:
            index_perf_history = client.get_eod_historical_data(idx_sym, idx_ex, as_of_date, force=force)
            if index_perf_history:
                valid_idx_bars = sorted(
                    [b for b in index_perf_history if b.get("date") and b["date"] <= as_of_date.isoformat()],
                    key=lambda x: x["date"],
                    reverse=True
                )
                if valid_idx_bars:
                    perf, last_date, start_date, idx_warns = calculate_perf_12m(valid_idx_bars, as_of_date)
                    if perf is not None:
                        index_perf_12m = perf
                        LOGGER.info("Prefilter: Index 12m perf: %.2f%% (from %s to %s)", perf * 100.0, start_date, last_date)
                    else:
                        LOGGER.warning("Prefilter: Index 12m perf not computed: %s", idx_warns)
        except Exception as exc:
            LOGGER.warning("Prefilter: Impossible de charger l'historique de l'indice %s: %s", index_symbol, exc)

    candidates = []
    rejections = []
    warnings_log = []

    # Counters
    mapped_count = 0
    passed_price_history_count = 0
    insufficient_liquidity_count = 0
    absolute_momentum_too_weak_count = 0
    relative_momentum_too_weak_count = 0
    market_cap_too_high_count = 0
    rejection_reasons = []

    for issuer in market_issuers:
        eodhd_item = eodhd_by_isin.get(issuer.isin.upper().strip())
        if not eodhd_item:
            eodhd_item = eodhd_by_symbol.get(issuer.symbol.upper().strip())

        if not eodhd_item:
            reason = "Non trouvé dans la liste des tickers EODHD"
            rejections.append({
                "ticker": issuer.symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Filtre 1 - Univers Euronext",
                "rejection_code": "NOT_FOUND_IN_EODHD",
                "rejection_reason": reason,
                "raw_values_snapshot": {},
                "warnings": "NOT_FOUND_IN_EODHD",
            })
            rejection_reasons.append("NOT_FOUND_IN_EODHD")
            continue

        mapped_count += 1
        eod_symbol = eodhd_item["Code"]
        eod_exchange = eodhd_item["Exchange"]
        eod_currency = eodhd_item.get("Currency", "EUR")
        instrument_type = eodhd_item.get("Type") or "Unknown"
        instrument_type_status = "known" if eodhd_item.get("Type") else "unknown"
        
        raw_values_snapshot = {
            "symbol": eod_symbol,
            "exchange": eod_exchange,
            "currency": eod_currency,
            "instrument_type": instrument_type,
            "instrument_type_status": instrument_type_status,
        }

        warnings = []
        if instrument_type_status == "unknown":
            warnings.append("instrument_type_unknown")
            warnings_log.append(f"{eod_symbol}: instrument_type_unknown")

        passed_prefilters_count = 0

        # Filtre 1 - Exclude if ETF, fund, warrant, bond, certificate, etc.
        eodhd_type_upper = instrument_type.upper().strip()
        excluded_keywords = ("ETF", "FUND", "WARRANT", "BOND", "DEBT", "NOTE", "CERTIFICATE", "RIGHT", "OPTION", "FUTURE", "OBLIGATION", "CERTIFICAT", "PRODUIT STRUCTURE")
        is_excluded = False
        if eodhd_type_upper:
            for kw in excluded_keywords:
                if kw in eodhd_type_upper:
                    is_excluded = True
                    break

        if is_excluded:
            reason = f"Type d'instrument '{instrument_type}' exclu (non ordinaire)"
            rejections.append({
                "ticker": eod_symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Filtre 1 - Univers Euronext",
                "rejection_code": "EXCLUDED_INSTRUMENT_TYPE",
                "rejection_reason": reason,
                "raw_values_snapshot": raw_values_snapshot,
                "warnings": ",".join(warnings),
            })
            rejection_reasons.append("EXCLUDED_INSTRUMENT_TYPE")
            continue

        passed_prefilters_count += 1

        # Fetch price history (12 months)
        try:
            price_history = client.get_eod_historical_data(eod_symbol, eod_exchange, as_of_date, force=force)
        except Exception as exc:
            reason = f"Impossible de charger l'historique de prix: {exc}"
            rejections.append({
                "ticker": eod_symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Filtre 2 - Données de prix",
                "rejection_code": "INSUFFICIENT_PRICE_HISTORY",
                "rejection_reason": reason,
                "raw_values_snapshot": raw_values_snapshot,
                "warnings": ",".join(warnings),
            })
            rejection_reasons.append("INSUFFICIENT_PRICE_HISTORY")
            continue

        valid_bars = sorted(
            [bar for bar in price_history if bar.get("date") and bar["date"] <= as_of_date.isoformat()],
            key=lambda x: x["date"],
            reverse=True,
        )

        # Filtre 2 - 180 trading days
        if len(valid_bars) < 180:
            reason = f"Nombre de barres de cotation insuffisant: {len(valid_bars)} < 180"
            rejections.append({
                "ticker": eod_symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Filtre 2 - Données de prix",
                "rejection_code": "INSUFFICIENT_PRICE_HISTORY",
                "rejection_reason": reason,
                "raw_values_snapshot": raw_values_snapshot,
                "warnings": ",".join(warnings),
            })
            rejection_reasons.append("INSUFFICIENT_PRICE_HISTORY")
            continue

        passed_price_history_count += 1
        passed_prefilters_count += 1

        # Fetch forex rate if currency != EUR
        try:
            forex_rate, is_fallback_forex = client.get_forex_rate(eod_currency, as_of_date, force=force)
            if is_fallback_forex:
                warnings.append("forex_fallback_used")
                warnings_log.append(f"{eod_symbol}: forex_fallback_used")
        except Exception as exc:
            reason = f"Impossible de récupérer le taux de change pour {eod_currency}: {exc}"
            rejections.append({
                "ticker": eod_symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Filtre 3 - Liquidité",
                "rejection_code": "INSUFFICIENT_LIQUIDITY",
                "rejection_reason": reason,
                "raw_values_snapshot": raw_values_snapshot,
                "warnings": ",".join(warnings),
            })
            rejection_reasons.append("INSUFFICIENT_LIQUIDITY")
            continue

        # Filtre 3 - Average daily traded value 3m (90 calendar days)
        last_price_date = date.fromisoformat(valid_bars[0]["date"])
        three_months_bars = [
            bar for bar in valid_bars
            if (last_price_date - date.fromisoformat(bar["date"])).days <= 90
        ]
        
        if not three_months_bars:
            reason = "Aucune barre de prix dans les 3 derniers mois"
            rejections.append({
                "ticker": eod_symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Filtre 3 - Liquidité",
                "rejection_code": "INSUFFICIENT_LIQUIDITY",
                "rejection_reason": reason,
                "raw_values_snapshot": raw_values_snapshot,
                "warnings": ",".join(warnings),
            })
            rejection_reasons.append("INSUFFICIENT_LIQUIDITY")
            continue

        sum_traded_value_3m = 0.0
        for bar in three_months_bars:
            c = float(bar.get("close") or 0.0)
            v = float(bar.get("volume") or 0.0)
            sum_traded_value_3m += c * v

        avg_daily_traded_value_3m = (sum_traded_value_3m / len(three_months_bars)) * forex_rate
        raw_values_snapshot["avg_daily_traded_value_3m"] = avg_daily_traded_value_3m

        # Also calculate 12m volume
        twelve_months_bars = [
            bar for bar in valid_bars
            if (last_price_date - date.fromisoformat(bar["date"])).days <= 365
        ]
        avg_daily_traded_value_12m = 0.0
        if twelve_months_bars:
            sum_traded_value_12m = 0.0
            for bar in twelve_months_bars:
                c = float(bar.get("close") or 0.0)
                v = float(bar.get("volume") or 0.0)
                sum_traded_value_12m += c * v
            avg_daily_traded_value_12m = (sum_traded_value_12m / len(twelve_months_bars)) * forex_rate
        raw_values_snapshot["avg_daily_traded_value_12m"] = avg_daily_traded_value_12m

        if avg_daily_traded_value_3m < min_daily_traded_eur:
            reason = f"Liquidité 3m insuffisante: {avg_daily_traded_value_3m:,.2f} EUR < {min_daily_traded_eur:,.2f} EUR"
            rejections.append({
                "ticker": eod_symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Filtre 3 - Liquidité",
                "rejection_code": "INSUFFICIENT_LIQUIDITY",
                "rejection_reason": reason,
                "raw_values_snapshot": raw_values_snapshot,
                "warnings": ",".join(warnings),
            })
            rejection_reasons.append("INSUFFICIENT_LIQUIDITY")
            insufficient_liquidity_count += 1
            continue

        passed_prefilters_count += 1

        # Filtre 4 - Anti-couteau qui tombe (Absolute performance 12m < -40%)
        perf, last_date, start_date, perf_warns = calculate_perf_12m(valid_bars, as_of_date)
        if perf is None:
            reason = f"Impossible de calculer la performance 12m: {perf_warns}"
            rejections.append({
                "ticker": eod_symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Filtre 4 - Momentum Absolu",
                "rejection_code": "INSUFFICIENT_PRICE_HISTORY",
                "rejection_reason": reason,
                "raw_values_snapshot": raw_values_snapshot,
                "warnings": ",".join(warnings),
            })
            rejection_reasons.append("INSUFFICIENT_PRICE_HISTORY")
            continue

        stock_perf_12m = perf
        raw_values_snapshot["stock_perf_12m"] = stock_perf_12m

        if stock_perf_12m < -0.4:
            reason = f"Performance absolue sur 12 mois trop faible: {stock_perf_12m * 100.0:.2f}% < -40%"
            rejections.append({
                "ticker": eod_symbol,
                "isin": issuer.isin,
                "company_name": issuer.name,
                "rejected_at_filter": "Filtre 4 - Momentum Absolu",
                "rejection_code": "ABSOLUTE_MOMENTUM_TOO_WEAK",
                "rejection_reason": reason,
                "raw_values_snapshot": raw_values_snapshot,
                "warnings": ",".join(warnings),
            })
            rejection_reasons.append("ABSOLUTE_MOMENTUM_TOO_WEAK")
            absolute_momentum_too_weak_count += 1
            continue

        passed_prefilters_count += 1

        # Filtre 5 - Relative performance 12m < -20% (optional)
        relative_perf_12m = None
        relative_momentum_status = "not_computed"
        
        if index_perf_12m is not None:
            relative_perf_12m = stock_perf_12m - index_perf_12m
            raw_values_snapshot["relative_perf_12m"] = relative_perf_12m
            relative_momentum_status = "computed"
            
            if relative_perf_12m < -0.2:
                reason = f"Performance relative sur 12 mois trop faible: {relative_perf_12m * 100.0:.2f}% < -20%"
                rejections.append({
                    "ticker": eod_symbol,
                    "isin": issuer.isin,
                    "company_name": issuer.name,
                    "rejected_at_filter": "Filtre 5 - Momentum Relatif",
                    "rejection_code": "RELATIVE_MOMENTUM_TOO_WEAK",
                    "rejection_reason": reason,
                    "raw_values_snapshot": raw_values_snapshot,
                    "warnings": ",".join(warnings),
                })
                rejection_reasons.append("RELATIVE_MOMENTUM_TOO_WEAK")
                relative_momentum_too_weak_count += 1
                continue

        passed_prefilters_count += 1

        # Filtre 6 - Capitalisation maximale (seulement si disponible)
        market_cap_raw = eodhd_item.get("MarketCapitalization") or eodhd_item.get("market_cap") or eodhd_item.get("MarketCap")
        market_cap_eur = None
        market_cap_status = "unavailable"
        
        if market_cap_raw is not None:
            try:
                market_cap_val = float(market_cap_raw)
                market_cap_eur = market_cap_val * forex_rate
                market_cap_status = "available"
                raw_values_snapshot["market_cap_eur"] = market_cap_eur
                
                if market_cap_eur > max_market_cap_eur:
                    reason = f"Capitalisation trop élevée: {market_cap_eur:,.2f} EUR > {max_market_cap_eur:,.2f} EUR"
                    rejections.append({
                        "ticker": eod_symbol,
                        "isin": issuer.isin,
                        "company_name": issuer.name,
                        "rejected_at_filter": "Filtre 6 - Capitalisation",
                        "rejection_code": "MARKET_CAP_TOO_HIGH",
                        "rejection_reason": reason,
                        "raw_values_snapshot": raw_values_snapshot,
                        "warnings": ",".join(warnings),
                    })
                    rejection_reasons.append("MARKET_CAP_TOO_HIGH")
                    market_cap_too_high_count += 1
                    continue
            except (ValueError, TypeError):
                pass
                
        if market_cap_status == "unavailable":
            warnings.append("MARKET_CAP_UNAVAILABLE")
            warnings_log.append(f"{eod_symbol}: MARKET_CAP_UNAVAILABLE")
            
        passed_prefilters_count += 1

        # Add to candidates!
        candidates.append({
            "ticker": eod_symbol,
            "isin": issuer.isin,
            "company_name": issuer.name,
            "exchange": eod_exchange,
            "currency": eod_currency,
            "instrument_type": instrument_type,
            "instrument_type_status": instrument_type_status,
            "price_history_days": len(valid_bars),
            "last_price_date": last_price_date.isoformat(),
            "last_close": float(valid_bars[0].get("close") or 0.0),
            "avg_daily_traded_value_3m": avg_daily_traded_value_3m,
            "avg_daily_traded_value_12m": avg_daily_traded_value_12m,
            "stock_perf_12m": stock_perf_12m,
            "index_symbol": index_symbol or "",
            "index_perf_12m": index_perf_12m,
            "relative_perf_12m": relative_perf_12m,
            "relative_momentum_status": relative_momentum_status,
            "market_cap_eur": market_cap_eur,
            "market_cap_status": market_cap_status,
            "passed_prefilters_count": passed_prefilters_count,
            "warning_count": len(warnings),
            "warnings": ",".join(warnings),
            "source_summary": f"EODHD {eodhd_backend.upper()}",
        })

    # 9. Output generation
    candidates_count = len(candidates)
    rejections_count = len(rejections)

    out_csv_path = output_csv or f"data/screeners/prefilter_candidates_{market_arg}_{as_of_date}.csv"

    # Save Candidates CSV
    if candidates:
        csv_file_path = Path(out_csv_path)
        csv_file_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_file_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(candidates[0].keys()))
            writer.writeheader()
            writer.writerows(candidates)
        LOGGER.info("Saved prefilter candidates CSV to %s", csv_file_path)
    else:
        dummy = {
            "ticker": "", "isin": "", "company_name": "", "exchange": "", "country": "", "currency": "",
            "instrument_type": "", "instrument_type_status": "", "price_history_days": 0, "last_price_date": "",
            "last_close": 0.0, "avg_daily_traded_value_3m": 0.0, "avg_daily_traded_value_12m": 0.0,
            "stock_perf_12m": 0.0, "index_symbol": "", "index_perf_12m": 0.0, "relative_perf_12m": 0.0,
            "relative_momentum_status": "", "market_cap_eur": 0.0, "market_cap_status": "",
            "passed_prefilters_count": 0, "warning_count": 0, "warnings": "", "source_summary": ""
        }
        csv_file_path = Path(out_csv_path)
        csv_file_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_file_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(dummy.keys()))
            writer.writeheader()
        LOGGER.info("Saved empty prefilter candidates CSV to %s", csv_file_path)

    # Save Rejections CSV
    rej_csv_path = None
    if explain_rejections:
        rej_csv_path = out_csv_path.replace("candidates", "rejections")
        if rej_csv_path == out_csv_path:
            rej_csv_path = out_csv_path + ".rejections.csv"
        
        csv_rej_file = Path(rej_csv_path)
        csv_rej_file.parent.mkdir(parents=True, exist_ok=True)
        if rejections:
            csv_rejections = []
            for r in rejections:
                csv_rejections.append({
                    "ticker": r["ticker"],
                    "isin": r["isin"],
                    "company_name": r["company_name"],
                    "rejected_at_filter": r["rejected_at_filter"],
                    "rejection_code": r["rejection_code"],
                    "rejection_reason": r["rejection_reason"],
                    "raw_values_snapshot": json.dumps(r["raw_values_snapshot"], ensure_ascii=False),
                    "warnings": r["warnings"],
                })

            with csv_rej_file.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["ticker", "isin", "company_name", "rejected_at_filter", "rejection_code", "rejection_reason", "raw_values_snapshot", "warnings"])
                writer.writeheader()
                writer.writerows(csv_rejections)
        else:
            with csv_rej_file.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["ticker", "isin", "company_name", "rejected_at_filter", "rejection_code", "rejection_reason", "raw_values_snapshot", "warnings"])
                writer.writeheader()
        LOGGER.info("Saved prefilter rejections CSV to %s", csv_rej_file)

    # Save JSON if requested
    if output_json:
        json_file_path = Path(output_json)
        json_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        rejection_counter = Counter(rejection_reasons)
        
        json_data = {
            "metadata": {
                "market": market_arg,
                "normalized_market": normalized_market,
                "exchange_code": exchange_code,
                "as_of_date": as_of_date.isoformat(),
                "execution_time": datetime.now().isoformat(),
                "force": force,
                "limit": limit,
                "index_symbol": index_symbol or "",
                "eodhd_backend": eodhd_backend,
            },
            "thresholds": {
                "min_daily_traded_value_eur": min_daily_traded_eur,
                "max_market_cap_eur": max_market_cap_eur,
            },
            "candidates": candidates,
            "rejections": rejections,
            "warnings": warnings_log,
            "counters_by_filter": {
                "initial_local_count": initial_local_count,
                "mapped_count": mapped_count,
                "passed_price_history_count": passed_price_history_count,
                "insufficient_liquidity_count": insufficient_liquidity_count,
                "absolute_momentum_too_weak_count": absolute_momentum_too_weak_count,
                "relative_momentum_too_weak_count": relative_momentum_too_weak_count,
                "market_cap_too_high_count": market_cap_too_high_count,
                "rejections_by_code": dict(rejection_counter),
            }
        }
        json_file_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Saved structured JSON output to %s", json_file_path)

    # 10. Display Diagnostics
    print("--------------------------------------------------")
    print("DIAGNOSTIC DU PRÉFILTRAGE HIGGONS")
    print("--------------------------------------------------")
    print(f"Nombre d'émetteurs locaux trouvés     : {initial_local_count}")
    print(f"Nombre mappés avec EODHD              : {mapped_count}")
    print(f"Nombre avec historique prix exploitable: {passed_price_history_count}")
    print(f"Nombre rejetés pour liquidité         : {insufficient_liquidity_count}")
    print(f"Nombre rejetés pour momentum absolu   : {absolute_momentum_too_weak_count}")
    print(f"Nombre rejetés pour momentum relatif  : {relative_momentum_too_weak_count}")
    print(f"Nombre rejetés pour capitalisation    : {market_cap_too_high_count}")
    print(f"Nombre final de candidats             : {candidates_count}")
    print("--------------------------------------------------")
    
    rejection_counter = Counter(rejection_reasons)
    print("Top 10 raisons de rejet :")
    for code, count in rejection_counter.most_common(10):
        print(f"  - {code}: {count}")
    print("--------------------------------------------------")
    print("Chemins des fichiers générés :")
    print(f"  - Candidats CSV : {out_csv_path}")
    if explain_rejections and rej_csv_path:
        print(f"  - Rejets CSV    : {rej_csv_path}")
    if output_json:
        print(f"  - Sortie JSON   : {output_json}")
    print("--------------------------------------------------")

    return 0
