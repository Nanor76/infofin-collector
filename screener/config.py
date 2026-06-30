from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class ScreenerConfig:
    # Level 1
    max_market_cap_eur: float = 12_000_000_000.0
    min_daily_traded_value_eur: float = 50_000.0
    min_relative_perf_12m: float = -0.20

    # Level 2
    max_pe_ratio: float = 12.0
    max_p_cf_ratio: float = 10.0

    # Level 3
    min_ebit_margin: float = 0.05
    min_roe: float = 0.09
    min_roce: float = 0.10

    # Level 4
    max_net_debt_to_ebitda: float = 3.0
