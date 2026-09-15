"""
src/parsing.py
Pure utility functions for symbol and option parsing.

This module has NO dependencies on src.api.* so it can be safely imported
in unit tests without triggering FastAPI, SQLAlchemy, or IB initialization.
All authoritative definitions for market constants live here.
"""

import math
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo
from typing import Optional, Any, Dict

# ---------------------------------------------------------------------------
# Market constants
# ---------------------------------------------------------------------------

# Market suffix mappings for international stocks
MARKET_SUFFIXES = {
    ".L":  ("LSE",  "GBP"),   # London Stock Exchange
    ".DE": ("IBIS", "EUR"),   # Germany (Xetra)
    ".PA": ("SBF",  "EUR"),   # France (Euronext Paris)
    ".AS": ("AEB",  "EUR"),   # Netherlands (Amsterdam)
    ".SW": ("EBS",  "CHF"),   # Switzerland
    ".MC": ("BM",   "EUR"),   # Spain (Madrid)
    ".MI": ("BVME", "EUR"),   # Italy (Milan)
}

# Exchange prefix mapping (Google Finance / Yahoo Finance style -> IBKR)
EXCHANGE_PREFIXES = {
    "EPA": ("MONEP", "EUR"),  # Paris -> MONEP
    "AMS": ("FTA",   "EUR"),  # Amsterdam -> FTA
    "ETR": ("DTB",   "EUR"),  # Xetra -> DTB (Eurex)
    "FRA": ("DTB",   "EUR"),  # Frankfurt -> DTB
    "LON": ("LSE",   "GBP"),  # London
    "SWX": ("EBS",   "CHF"),  # SWX -> EBS (Swiss)
    "MC":  ("MEFF",  "EUR"),  # Madrid -> MEFF
    "MCE": ("MEFF",  "EUR"),  # Madrid (alternative)
}


# ---------------------------------------------------------------------------
# Symbol parsing
# ---------------------------------------------------------------------------

def parse_symbol(symbol: str) -> tuple:
    """
    Parse a symbol with optional market suffix.
    Returns (ticker, exchange, currency).

    Examples:
        'AAPL'    -> ('AAPL', 'SMART', 'USD')
        'BATS.L'  -> ('BATS', 'LSE',   'GBP')
        'RMS.PA'  -> ('RMS',  'SBF',   'EUR')
    """
    symbol = symbol.upper().strip()
    for suffix, (exchange, currency) in MARKET_SUFFIXES.items():
        if symbol.endswith(suffix.upper()):
            ticker = symbol[:-len(suffix)]
            return (ticker, exchange, currency)
    return (symbol, "SMART", "USD")


def parse_osi_symbol(symbol: str) -> dict:
    """
    Extract components from an OSI-format option symbol.

    e.g. 'ASTS  260109P00065000' -> {'ticker': 'ASTS', 'expiry': '20260109',
                                      'strike': 65.0, 'right': 'P'}

    Returns:
        dict with keys: ticker (str), expiry (YYYYMMDD str),
                        strike (float), right ('C' or 'P')
    """
    symbol_clean = symbol.replace(" ", "")
    strike = float(symbol_clean[-8:]) / 1000.0
    right = symbol_clean[-9]
    expiry_raw = symbol_clean[-15:-9]
    expiry = f"20{expiry_raw[0:2]}{expiry_raw[2:4]}{expiry_raw[4:6]}"
    ticker = symbol_clean[:-15].strip()
    return {"ticker": ticker, "expiry": expiry, "strike": strike, "right": right}


def parse_european_symbol(symbol: str) -> dict:
    """
    Extract components from an IBKR localSymbol (European options).

    e.g. 'P HMI  20260220 1900 M' -> {'right': 'P', 'ticker': 'HMI',
                                        'expiry': '20260220', 'strike': 1900.0}

    Returns:
        dict with keys: right ('P'/'C'), ticker (str),
                        expiry (YYYYMMDD str), strike (float)
    Raises:
        ValueError: if symbol has fewer than 4 space-separated parts
    """
    parts = symbol.split()
    if len(parts) < 4:
        raise ValueError(f"Invalid European option format: {symbol!r}")
    return {
        "right":  parts[0],
        "ticker": parts[1],
        "expiry": parts[2],
        "strike": float(parts[3]),
    }


# ---------------------------------------------------------------------------
# Greeks / snapshot validation
# ---------------------------------------------------------------------------

def greeks_are_valid(g, und_price_override=None) -> bool:
    """
    Return True if at least one Greek (delta/gamma/theta/vega) is non-zero
    and non-NaN.

    Accepts any duck-typed object with .delta, .gamma, .theta, .vega attributes
    (e.g. ib_async OptionGreeks or an OptionSnapshot ORM row).
    """
    if not g:
        return False

    def _safe(val):
        return val if (val is not None and not math.isnan(val)) else 0.0

    return not (
        _safe(g.delta) == 0
        and _safe(g.gamma) == 0
        and _safe(g.theta) == 0
        and _safe(g.vega) == 0
    )


def snap_is_valid(snap) -> bool:
    """
    Return True if an OptionSnapshot has at least one non-zero Greek.

    Accepts any duck-typed object with .delta, .gamma, .theta, .vega attributes.
    """
    if not snap:
        return False
    d = snap.delta or 0.0
    g = snap.gamma or 0.0
    t = snap.theta or 0.0
    v = snap.vega or 0.0
    return not (d == 0 and g == 0 and t == 0 and v == 0)


# ---------------------------------------------------------------------------
# Option chain calculations & strike filtering
# ---------------------------------------------------------------------------

def clean_price(val: Any) -> Optional[float]:
    """
    Return float if val is a valid non-negative price, else None.
    Negative numbers, None, NaN, and sentinel values like -1 return None.
    Distinguishes a real price of 0.0 (returns 0.0) from missing data (returns None).
    """
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or f < 0:
            return None
        return f
    except (ValueError, TypeError):
        return None


def clean_size(val: Any) -> Optional[int]:
    """
    Return int if val is a valid non-negative size, else None.
    Negative numbers, None, NaN return None.
    """
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or f < 0:
            return None
        return int(f)
    except (ValueError, TypeError):
        return None


def clean_greek(val: Any) -> Optional[float]:
    """
    Return float if val is a valid non-NaN Greek number, else None.
    """
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def calc_option_mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """
    Calculate option mid price: (bid + ask) / 2.
    Only calculated when BOTH bid and ask are valid numbers (not None, not NaN, and >= 0).
    If either is missing or invalid, returns None.
    """
    if bid is None or ask is None:
        return None
    if math.isnan(bid) or math.isnan(ask):
        return None
    if bid < 0 or ask < 0:
        return None
    return round((bid + ask) * 0.5, 4)


def calc_option_intrinsic(right: str, strike: float, underlying_price: float) -> Optional[float]:
    """
    Calculate option intrinsic value.

    For Call: max(0.0, underlying_price - strike)
    For Put:  max(0.0, strike - underlying_price)
    """
    if underlying_price is None or strike is None or underlying_price <= 0.0 or strike <= 0.0:
        return 0.0
    r = right.upper().strip()
    if r in ('P', 'PUT'):
        return max(0.0, strike - underlying_price)
    else:
        return max(0.0, underlying_price - strike)


def calc_option_extrinsic(price: Optional[float], intrinsic: Optional[float]) -> Optional[float]:
    """
    Calculate option extrinsic (time) value.
    extrinsic = max(0.0, price - intrinsic)
    If price or intrinsic is None, returns None.
    """
    if price is None or intrinsic is None:
        return None
    if price <= 0.0:
        return 0.0
    return max(0.0, price - max(0.0, intrinsic))


def calc_moneyness_pct(strike: float, underlying_price: float) -> float:
    """
    Calculate moneyness as percentage deviation of strike from underlying price:
    ((strike - underlying_price) / underlying_price) * 100.
    Positive indicates strike > underlying price.
    """
    if underlying_price <= 0.0:
        return 0.0
    return round(((strike - underlying_price) / underlying_price) * 100.0, 2)


def filter_strikes_window(
    strikes: list,
    min_strike: float = None,
    max_strike: float = None,
    underlying_price: float = 0.0,
    strikes_below: int = 5,
    strikes_above: int = 5,
    max_limit: int = 20,
) -> list:
    """
    Filter a list of strikes to a safe, focused window:
    1. If explicit [min_strike, max_strike] are given:
       Filters strikes in range. If the range contains more than max_limit strikes,
       centers the selection around underlying_price (ATM) to keep the most relevant strikes.
    2. If no range is given:
       Takes strikes_below strikes below ATM, the closest ATM strike, and strikes_above
       strikes above ATM, capped at max_limit.
    """
    if not strikes:
        return []

    sorted_strikes = sorted(set(strikes))
    ref = underlying_price if underlying_price > 0.0 else sorted_strikes[len(sorted_strikes) // 2]

    if min_strike is not None or max_strike is not None:
        filtered = [
            s for s in sorted_strikes
            if (min_strike is None or s >= min_strike)
            and (max_strike is None or s <= max_strike)
        ]
        if max_limit and len(filtered) > max_limit:
            # Range too wide: center the max_limit window around ATM strike within the filtered range
            filtered_closest_idx = min(range(len(filtered)), key=lambda i: abs(filtered[i] - ref))
            half = max_limit // 2
            start = max(0, filtered_closest_idx - half)
            end = min(len(filtered), start + max_limit)
            if end - start < max_limit:
                start = max(0, end - max_limit)
            filtered = filtered[start:end]
    else:
        closest_idx = min(range(len(sorted_strikes)), key=lambda i: abs(sorted_strikes[i] - ref))
        below = max(0, strikes_below if strikes_below is not None else 5)
        above = max(0, strikes_above if strikes_above is not None else 5)
        start_idx = max(0, closest_idx - below)
        end_idx = min(len(sorted_strikes), closest_idx + above + 1)
        filtered = sorted_strikes[start_idx:end_idx]

        if max_limit and len(filtered) > max_limit:
            filtered = filtered[:max_limit]

    return filtered


def select_best_option_chain(
    chains: list,
    target_exchange: str = "DTB",
    expiry: str = "",
    trading_class: str = None,
    underlying_symbol: str = "",
):
    """
    Select the best / primary OptionChain definition from a list of chains
    returned by IBKR reqSecDefOptParamsAsync.

    Selection priority:
    1. Filter by target_exchange (e.g. "DTB") and expiry. Fall back to any exchange with expiry if none match.
    2. If trading_class is explicitly provided, match it exactly (case-insensitive).
    3. Rank candidate chains by a multi-criteria heuristic:
       - No numeric suffix in tradingClass (adjusted contracts append digits like HMI2, HMI4, AAPL1).
       - Largest number of strikes (standard active contracts maintain the full strike grid).
       - Largest number of total expirations.
       - Exact match of tradingClass with underlying root ticker.
    """
    if not chains:
        return None

    def get_attr(obj, attr_name, default=None):
        if isinstance(obj, dict):
            return obj.get(attr_name, default)
        return getattr(obj, attr_name, default)

    clean_expiry = expiry.strip().replace("-", "") if expiry else ""
    target_exch = (target_exchange or "").strip().upper()

    # 1. Match target_exchange and expiry
    candidates = [
        c for c in chains
        if (not target_exch or get_attr(c, 'exchange', '').upper() == target_exch)
        and (not clean_expiry or clean_expiry in (get_attr(c, 'expirations') or []))
    ]

    # Fallback: any exchange with matching expiry
    if not candidates and clean_expiry:
        candidates = [
            c for c in chains
            if clean_expiry in (get_attr(c, 'expirations') or [])
        ]

    if not candidates:
        return None

    # 2. Filter by explicit trading_class if requested
    if trading_class:
        clean_tc = trading_class.strip().upper()
        tc_matched = [
            c for c in candidates
            if get_attr(c, 'tradingClass', '').upper() == clean_tc
        ]
        if tc_matched:
            return tc_matched[0]
        return None

    # 3. Smart ranking heuristic
    def score_chain(c):
        tc = get_attr(c, 'tradingClass', '')
        # Unadjusted standard contracts typically have no digits (e.g. HMI vs HMI2)
        no_digits = 1 if not any(ch.isdigit() for ch in tc) else 0
        # Number of available strikes
        strikes = get_attr(c, 'strikes') or []
        strike_count = len(strikes)
        # Number of available expirations
        expirations = get_attr(c, 'expirations') or []
        exp_count = len(expirations)
        # Ticker match
        sym_match = 1 if underlying_symbol and tc.upper() == underlying_symbol.upper() else 0
        return (no_digits, strike_count, exp_count, sym_match)

    candidates.sort(key=score_chain, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Black-Scholes Greeks Calculation
# ---------------------------------------------------------------------------

def calc_bs_greeks(
    right: str,
    underlying_price: float,
    strike: float,
    expiry: str,
    implied_vol: float,
    risk_free_rate: float = 0.035,
    eval_date: str = None
) -> dict:
    """
    Calculate Black-Scholes Option Greeks using the current underlying spot price
    and implied volatility.

    Parameters:
        right: 'C' / 'CALL' or 'P' / 'PUT'
        underlying_price: Current spot price of the underlying asset
        strike: Strike price
        expiry: Expiration date in YYYYMMDD format
        implied_vol: Implied volatility (e.g. 0.32 for 32%)
        risk_free_rate: Annualized risk-free interest rate (default 0.035 = 3.5%)
        eval_date: Optional evaluation date in YYYYMMDD format (defaults to today)

    Returns:
        dict with keys: 'delta', 'gamma', 'theta', 'vega' (all rounded floats)
    """
    if underlying_price <= 0.0 or strike <= 0.0 or implied_vol <= 0.0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    try:
        clean_exp = expiry.replace("-", "").strip()
        exp_dt = datetime.strptime(clean_exp, "%Y%m%d")
        if eval_date:
            now_dt = datetime.strptime(eval_date.replace("-", "").strip(), "%Y%m%d")
        else:
            now_dt = datetime.now()
        days_to_expiry = max((exp_dt.date() - now_dt.date()).days, 0)
        # Avoid division by zero on expiry date: minimum 1 day fraction
        T = max(days_to_expiry / 365.0, 1.0 / 365.0)
    except Exception:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    sigma = implied_vol
    r = risk_free_rate
    S = underlying_price
    K = strike

    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    def _norm_cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _norm_pdf(x):
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    is_call = right.upper().strip() in ("C", "CALL")

    if is_call:
        delta = _norm_cdf(d1)
        theta = (- (S * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(T)) - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (- (S * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(T)) + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0

    gamma = _norm_pdf(d1) / (S * sigma * math.sqrt(T))
    vega = (S * _norm_pdf(d1) * math.sqrt(T)) / 100.0

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 4),
        "theta": round(theta, 4),
        "vega": round(vega, 4)
    }


def is_market_open_for_symbol(
    symbol: Optional[str] = None,
    exchange: Optional[str] = None,
    currency: Optional[str] = None,
    ref_dt: Optional[datetime] = None,
) -> bool:
    """
    Check if the regular trading market session is currently open for a given instrument.

    Market session definitions:
    - US markets (USD, SMART, CBOE, AMEX, NASDAQ, NYSE, ISLAND): Mon-Fri 09:30-16:00 US/Eastern
    - UK markets (GBP, LSE, .L): Mon-Fri 08:00-16:30 Europe/London
    - EU markets (EUR, CHF, EUREX, DTB, SBF, EURONEXT, .PA, .AS, .MC, etc.): Mon-Fri 09:00-17:30 Europe/Paris
    """
    if ref_dt is None:
        ref_dt = datetime.now(timezone.utc)
    elif ref_dt.tzinfo is None:
        ref_dt = ref_dt.replace(tzinfo=timezone.utc)

    sym_upper = (symbol or "").upper().strip()
    exch_upper = (exchange or "").upper().strip()
    curr_upper = (currency or "").upper().strip()

    first_token = sym_upper.split()[0] if sym_upper else ""
    first_token_clean = first_token.split('.')[0] if '.' in first_token else first_token

    eu_tickers = {
        'RMS', 'MC', 'OR', 'SAN', 'BBVA', 'ITX', 'AIR', 'ASML', 'BNP', 'SU', 'DG',
        'GLE', 'CS', 'SAP', 'SIE', 'ALV', 'BAYN', 'BMW', 'MBG', 'VOW3', 'OESX', 'ESTX50'
    }
    uk_tickers = {
        'BATS', 'RIO', 'ULVR', 'NG', 'SHEL', 'AZN', 'GSK', 'BP', 'HSBA'
    }

    is_uk = (
        curr_upper == "GBP"
        or exch_upper in ("LSE", "LSEETF")
        or sym_upper.endswith(".L")
        or first_token_clean in uk_tickers
    )
    is_eu = (
        curr_upper in ("EUR", "CHF")
        or exch_upper in ("EUREX", "DTB", "SBF", "EURONEXT", "BVME", "MEFF", "AEB", "SWX", "VIRTX", "SOFFEX")
        or any(sym_upper.endswith(sfx) for sfx in (".PA", ".AS", ".BR", ".MC", ".DE", ".MI", ".VI", ".SW"))
        or sym_upper.startswith(("P HMI", "C HMI", "P OESX", "C OESX"))
        or first_token_clean in eu_tickers
    )
    is_us = (
        curr_upper == "USD"
        or exch_upper in ("SMART", "CBOE", "AMEX", "NASDAQ", "NYSE", "ISLAND", "BATS", "BOX", "MIAX", "PHLX", "ISE", "EDGX")
    )

    if is_uk:
        dt = ref_dt.astimezone(ZoneInfo("Europe/London"))
        return dt.weekday() < 5 and (time(8, 0) <= dt.time() < time(16, 30))
    elif is_eu and not is_us:
        dt = ref_dt.astimezone(ZoneInfo("Europe/Paris"))
        return dt.weekday() < 5 and (time(9, 0) <= dt.time() < time(17, 30))
    else:
        # Default to US market hours
        dt = ref_dt.astimezone(ZoneInfo("America/New_York"))
        return dt.weekday() < 5 and (time(9, 30) <= dt.time() < time(16, 0))


def determine_market_statuses(
    is_open: bool,
    has_bid_ask: bool,
    has_greeks: bool,
    market_data_type: Optional[int] = 1,
    source: str = "ibkr",
    greeks_are_frozen: bool = False,
) -> Dict[str, str]:
    """
    Determine market_data_status, quote_status, and greeks_status.

    Possible return values:
      market_data_status: "LIVE", "FROZEN", "CLOSED", "DELAYED"
      quote_status: "LIVE", "FROZEN", "CLOSED", "DELAYED", "UNAVAILABLE"
      greeks_status: "LIVE", "FROZEN", "CLOSED", "DELAYED", "UNAVAILABLE"
    """
    if source == "db":
        if not is_open:
            return {
                "market_data_status": "CLOSED",
                "quote_status": "FROZEN" if has_bid_ask else "CLOSED",
                "greeks_status": "FROZEN" if has_greeks else "CLOSED",
            }
        else:
            return {
                "market_data_status": "FROZEN",
                "quote_status": "FROZEN" if has_bid_ask else "UNAVAILABLE",
                "greeks_status": "FROZEN" if has_greeks else "UNAVAILABLE",
            }

    if source == "cboe":
        if is_open:
            return {
                "market_data_status": "DELAYED",
                "quote_status": "DELAYED" if has_bid_ask else "UNAVAILABLE",
                "greeks_status": "FROZEN" if has_greeks else "UNAVAILABLE",
            }
        else:
            return {
                "market_data_status": "CLOSED",
                "quote_status": "FROZEN" if has_bid_ask else "CLOSED",
                "greeks_status": "FROZEN" if has_greeks else "CLOSED",
            }

    # source == "ibkr"
    if not is_open:
        mkt_status = "FROZEN" if market_data_type in (2, 4) else "CLOSED"
        q_status = "FROZEN" if has_bid_ask else "CLOSED"
        g_status = "FROZEN" if has_greeks else "CLOSED"
        return {
            "market_data_status": mkt_status,
            "quote_status": q_status,
            "greeks_status": g_status,
        }
    else:
        # Market is open
        if market_data_type == 3:
            mkt_status = "DELAYED"
            q_status = "DELAYED" if has_bid_ask else "UNAVAILABLE"
            g_status = "DELAYED" if has_greeks else "UNAVAILABLE"
        elif market_data_type in (2, 4):
            mkt_status = "FROZEN"
            q_status = "FROZEN" if has_bid_ask else "UNAVAILABLE"
            g_status = "FROZEN" if has_greeks else "UNAVAILABLE"
        else:
            mkt_status = "LIVE"
            q_status = "LIVE" if has_bid_ask else "UNAVAILABLE"
            g_status = "FROZEN" if greeks_are_frozen else ("LIVE" if has_greeks else "UNAVAILABLE")

        return {
            "market_data_status": mkt_status,
            "quote_status": q_status,
            "greeks_status": g_status,
        }
