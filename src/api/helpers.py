import logging
import time
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
    clean_price,
    clean_size,
    clean_greek,
    calc_option_mid,
    calc_option_intrinsic,
    calc_option_extrinsic,
    is_market_open_for_symbol,
    determine_market_statuses,
)
from src.models import OptionGreeks
from src.config import settings

logger = logging.getLogger("ibkr-api")

_CBOE_CACHE = {}  # {cboe_ticker: (monotonic_ts, data)}
_CBOE_CACHE_TTL = 180.0  # 3 minutes


def _clear_cboe_cache():
    """Clear the in-memory CBOE responses cache."""
    _CBOE_CACHE.clear()


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

        now_mono = time.monotonic()
        cached = _CBOE_CACHE.get(cboe_ticker)
        if cached and (now_mono - cached[0] < _CBOE_CACHE_TTL):
            data = cached[1]
        else:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    return None
                data = r.json()
            if data and 'data' in data and 'options' in data['data']:
                _CBOE_CACHE[cboe_ticker] = (now_mono, data)

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

        strike_fmt = f"{int(strike)}" if strike == int(strike) else f"{strike}"

        cboe_bid = clean_price(option_data.get('bid'))
        cboe_bid_size = clean_size(option_data.get('bid_size')) if cboe_bid is not None else None
        cboe_ask = clean_price(option_data.get('ask'))
        cboe_ask_size = clean_size(option_data.get('ask_size')) if cboe_ask is not None else None
        cboe_mid = calc_option_mid(cboe_bid, cboe_ask)

        cboe_last = clean_price(option_data.get('last_trade_price'))
        cboe_last_date = option_data.get('last_trade_time')

        raw_und = data.get('data', {}).get('current_price')
        cboe_und = clean_price(raw_und)

        delta = clean_greek(option_data.get('delta'))
        gamma = clean_greek(option_data.get('gamma'))
        vega = clean_greek(option_data.get('vega'))
        theta = clean_greek(option_data.get('theta'))
        raw_iv = clean_greek(option_data.get('iv'))
        iv = raw_iv if (raw_iv is not None and raw_iv > 0) else None

        has_cboe_greeks = not (
            (delta is None or delta == 0) and
            (gamma is None or gamma == 0) and
            (vega is None or vega == 0) and
            (theta is None or theta == 0)
        )
        if not has_cboe_greeks:
            delta = gamma = vega = theta = iv = None

        intrinsic = round(calc_option_intrinsic(right, strike, cboe_und), 4) if (cboe_und and strike > 0) else None
        extrinsic = round(max(cboe_mid - intrinsic, 0.0), 4) if (cboe_mid is not None and intrinsic is not None) else None

        is_open = is_market_open_for_symbol(ticker, exchange="CBOE", currency="USD")
        statuses = determine_market_statuses(
            is_open=is_open,
            has_bid_ask=(cboe_bid is not None or cboe_ask is not None),
            has_greeks=(delta is not None or iv is not None),
            source="cboe"
        )

        return OptionGreeks(
            symbol=f"{ticker} {expiry} {strike_fmt} {right} (CBOE)",
            delta=delta,
            gamma=gamma,
            vega=vega,
            theta=theta,
            implied_vol=iv,
            underlying_price=cboe_und,
            bid=cboe_bid,
            bid_size=cboe_bid_size,
            ask=cboe_ask,
            ask_size=cboe_ask_size,
            mid=cboe_mid,
            intrinsic_value=intrinsic,
            extrinsic_value=extrinsic,
            last_price=cboe_last,
            volume=int(val_or_zero(option_data.get('volume'))),
            open_interest=int(val_or_zero(option_data.get('open_interest'))),
            last_date=cboe_last_date,
            market_data_status=statuses["market_data_status"],
            quote_status=statuses["quote_status"],
            greeks_status=statuses["greeks_status"]
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
