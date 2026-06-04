import logging
import httpx
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from src.parsing import (
    parse_symbol,
    greeks_are_valid as _greeks_are_valid,
    snap_is_valid as _snap_is_valid,
)
from src.models import OptionGreeks
from src.config import settings

logger = logging.getLogger("ibkr-api")



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
            last_date=option_data.get('last_trade_time')
        )

    except Exception as e:
        logger.debug(f"CBOE Fetch failed for {ticker}: {e}")
        return None



def _is_market_open():
    """
    Return True if markets are considered active.
    Active hours: Monday-Friday, 09:00-23:00 in the configured timezone (TZ).
    Outside this window the system serves cached data from DB without
    hitting external sources (CBOE / IBKR) unless a record is missing.
    """
    try:
        tz = ZoneInfo(settings.TZ)
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()
    if now.weekday() >= 5:  # Weekend
        return False
    if now.hour < 9 or now.hour >= 23:
        return False
    return True
