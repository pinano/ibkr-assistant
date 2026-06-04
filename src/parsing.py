"""
src/parsing.py
Pure utility functions for symbol and option parsing.

This module has NO dependencies on src.api.* so it can be safely imported
in unit tests without triggering FastAPI, SQLAlchemy, or IB initialization.
All authoritative definitions for market constants live here.
"""

import math

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
