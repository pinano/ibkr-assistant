import math
import logging
import httpx
from datetime import datetime

from src.api.constants import MARKET_SUFFIXES
from src.models import OptionGreeks

logger = logging.getLogger("ibkr-api")


def parse_symbol(symbol: str) -> tuple:
    """
    Parse a symbol with optional market suffix.
    Returns (ticker, exchange, currency).

    Examples:
        'AAPL' -> ('AAPL', 'SMART', 'USD')
        'BATS.L' -> ('BATS', 'LSE', 'GBP')
        'RMS.PA' -> ('RMS', 'SBF', 'EUR')
    """
    symbol = symbol.upper().strip()

    for suffix, (exchange, currency) in MARKET_SUFFIXES.items():
        if symbol.endswith(suffix.upper()):
            ticker = symbol[:-len(suffix)]
            return (ticker, exchange, currency)

    # Default: US stock
    return (symbol, "SMART", "USD")


async def _fetch_cboe_greeks(ticker: str, expiry: str, strike: float, right: str):
    """
    Fetch Greeks from CBOE (delayed quotes).
    Returns OptionGreeks object or None if not found/error.
    """
    try:
        cboe_ticker = ticker.upper()
        if cboe_ticker in ['SPX', 'VIX', 'RUT', 'NDX', 'OEX', 'DJX']:
            cboe_ticker = f"_{cboe_ticker}"

        url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{cboe_ticker}.json"

        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            data = r.json()

        if not data or 'data' not in data or 'options' not in data['data']:
            return None

        yy_expiry = expiry[2:]
        strike_int = int(round(strike * 1000))
        strike_str = f"{strike_int:08d}"
        clean_ticker = ticker.upper().replace('.', '')

        option_id = f"{clean_ticker}{yy_expiry}{right}{strike_str}"

        option_data = None
        for opt in data['data']['options']:
            if opt['option'] == option_id:
                option_data = opt
                break

        if not option_data:
            return None

        def val_or_zero(val):
            if val is None:
                return 0.0
            if isinstance(val, str):
                try:
                    return float(val)
                except Exception:
                    return 0.0
            return float(val)

        return OptionGreeks(
            symbol=f"{ticker} {expiry} {strike} {right} (CBOE)",
            delta=val_or_zero(option_data.get('delta')),
            gamma=val_or_zero(option_data.get('gamma')),
            vega=val_or_zero(option_data.get('vega')),
            theta=val_or_zero(option_data.get('theta')),
            implied_vol=val_or_zero(option_data.get('iv')),
            underlying_price=val_or_zero(data.get('data', {}).get('current_price')),
            last_price=val_or_zero(option_data.get('last_trade_price')),
            volume=int(val_or_zero(option_data.get('volume'))),
            open_interest=int(val_or_zero(option_data.get('open_interest'))),
            last_date=option_data.get('last_trade_time') or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    except Exception as e:
        logger.debug(f"CBOE Fetch failed for {ticker}: {e}")
        return None


def _greeks_are_valid(g, und_price_override=None):
    """Return True if the Greeks data is meaningful enough to cache.

    Requires at least one non-zero Greek (delta, gamma, theta, or vega).
    """
    if not g:
        return False

    def _safe(val):
        return val if (val is not None and not math.isnan(val)) else 0.0

    delta = _safe(g.delta)
    gamma = _safe(g.gamma)
    theta = _safe(g.theta)
    vega = _safe(g.vega)

    if delta == 0 and gamma == 0 and theta == 0 and vega == 0:
        return False
    return True


def _snap_is_valid(snap):
    """Return True if a cached OptionSnapshot has meaningful Greeks."""
    if not snap:
        return False
    d = snap.delta or 0.0
    g = snap.gamma or 0.0
    t = snap.theta or 0.0
    v = snap.vega or 0.0
    if d == 0 and g == 0 and t == 0 and v == 0:
        return False
    return True


def _is_market_open():
    """
    Return True if markets are considered active.
    Active hours: Monday-Friday, 09:00-23:00 container local time.
    Outside this window the system serves cached data from DB without
    hitting external sources (CBOE / IBKR) unless a record is missing.
    """
    now = datetime.now()
    if now.weekday() >= 5:  # Weekend
        return False
    if now.hour < 9 or now.hour >= 23:
        return False
    return True
