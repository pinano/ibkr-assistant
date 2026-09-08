import asyncio
import math
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session
from typing import List, Optional
from ib_async import Option, Contract

from src.api.auth import verify_key
from src.api.connection import get_ib
from src.api.constants import EXCHANGE_PREFIXES
from src.api.contracts import _qualify_option_contract
from src.api.database import get_db
from src.api.helpers import (
    parse_symbol,
    _fetch_cboe_greeks,
    _greeks_are_valid,
    _snap_is_valid,
    _is_market_open,
)
from src.parsing import (
    parse_osi_symbol,
    parse_european_symbol,
    calc_option_intrinsic,
    calc_option_extrinsic,
    calc_moneyness_pct,
    filter_strikes_window,
)
from src.models import (
    OptionGreeks,
    OptionSnapshot,
    OptionChainItem,
    OptionQuoteItem,
    StrikeChainRow,
    OptionChainQuotesResponse,
)

logger = logging.getLogger("ibkr-api")


router = APIRouter()


@router.get("/option/greeks", response_model=OptionGreeks,
             dependencies=[Depends(verify_key)])
async def get_option_greeks(
    underlying: str,
    expiry: str,
    strike: float,
    right: str,
    conId: int = 0,
    force_refresh: bool = False,
    db: Session = Depends(get_db)
):
    """
    Fetch Greeks for an option.
    1. Checks DB cache first (valid for 60 mins).
    2. Falls back to live IBKR query if stale or forced.
    3. Serves stale DB cache if live query fails or IBKR is disconnected.
    """
    try:
        right = right.upper().strip()
        underlying = underlying.strip()
        expiry = expiry.strip()

        # Check for explicit exchange prefix (e.g. "EPA:MC")
        prefix_exchange = None
        prefix_currency = None

        if ':' in underlying:
            parts = underlying.split(':')
            if len(parts) == 2:
                prefix = parts[0].upper()
                underlying = parts[1]

                if prefix in EXCHANGE_PREFIXES:
                    prefix_exchange, prefix_currency = EXCHANGE_PREFIXES[prefix]
                    logger.info(f"Using explicit prefix {prefix} -> {prefix_exchange}, {prefix_currency}")

        if right not in ('P', 'C'):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid right: {right}. Must be P or C")

        strike_fmt = f"{int(strike)}" if strike == int(strike) else f"{strike}"
        display_symbol = f"{underlying} {expiry} {strike_fmt} {right}"

        snap = None

        # Normalize expiry to avoid mismatch (remove hyphens)
        expiry = expiry.replace("-", "")

        if not conId:
            patterns = [f"{underlying}%{expiry}%{strike_fmt}%{right}"]
            if strike_fmt != str(strike):
                patterns.append(f"{underlying}%{expiry}%{strike}%{right}")
            snap = db.query(OptionSnapshot).filter(
                or_(*[OptionSnapshot.symbol.like(p) for p in patterns])
            ).order_by(OptionSnapshot.updated_at.desc()).first()
            if snap:
                conId = snap.conId
                logger.info(
                    f"Resolved conId={conId} from database snapshot for {underlying} {expiry}")

        # 1. If we still don't have conId, try live positions (if possible)
        if not conId:
            try:
                client = await get_ib()
                for p in client.positions():
                    c = p.contract
                    if (c.secType == 'OPT'
                        and c.symbol.upper() == underlying.upper()
                        and c.lastTradeDateOrContractMonth == expiry
                        and c.strike == strike
                            and c.right.upper() == right):
                        conId = c.conId
                        break
            except Exception:
                logger.debug(
                    "Could not resolve conId via positions (Gateway down)")

        # ------------------------------------------------------------------
        # Cache decision — single unified rule:
        #
        #   Market OPEN  (Mon-Fri 09:00-23:00 local):
        #       Serve from DB if snap exists AND is < 60 min old.
        #       Otherwise fall through to CBOE / IBKR.
        #
        #   Market CLOSED (weekends or outside 09:00-23:00):
        #       Always serve from DB if ANY snap exists, regardless of age.
        #       Only fall through to CBOE / IBKR when no record exists at all.
        # ------------------------------------------------------------------
        market_open = _is_market_open()
        use_cache = False

        if snap:
            if market_open:
                # Normal trading hours: use cache only if fresh (< 60 min)
                is_fresh = snap.updated_at and snap.updated_at > datetime.now() - timedelta(minutes=60)
                if is_fresh and not force_refresh:
                    logger.info(f"Market open: serving fresh cache for {underlying} (age < 60 min)")
                    use_cache = True
            else:
                # Market closed: serve whatever is in DB, no matter how old
                if not force_refresh:
                    logger.info(f"Market closed: serving cached data for {underlying} (age irrelevant)")
                    use_cache = True

        if use_cache:
            logger.info(f"Serving cached greeks for conId={conId}")
            # Prefer last_trade_date (actual trade time); fall back to updated_at for old rows
            effective_date = snap.last_trade_date or snap.updated_at
            cached_last_price = snap.last_price or 0.0
            return OptionGreeks(
                symbol=snap.symbol,
                delta=snap.delta or 0.0,
                gamma=snap.gamma or 0.0,
                vega=snap.vega or 0.0,
                theta=snap.theta or 0.0,
                implied_vol=snap.implied_vol or 0.0,
                underlying_price=snap.underlying_price or 0.0,
                last_price=cached_last_price if cached_last_price > 0 else 0.0,
                volume=snap.volume or 0,
                open_interest=snap.open_interest or 0,
                last_date=effective_date.strftime("%Y-%m-%d %H:%M:%S") if effective_date else None
            )


        # 2b. Check CBOE (for US options)
        if prefix_exchange:
            is_likely_us = False
        else:
            is_likely_us = '.' not in underlying or underlying in ['SPX', 'VIX', 'NDX', 'RUT']

        if is_likely_us and not force_refresh:
            try:
                cboe_data = await _fetch_cboe_greeks(underlying, expiry, strike, right)
                if cboe_data:
                    logger.info(f"Fetched Greeks from CBOE for {underlying} {expiry} {strike} {right}")

                    try:
                        db_symbol = display_symbol
                        snap = db.query(OptionSnapshot).filter(
                            or_(
                                OptionSnapshot.symbol == db_symbol,
                                OptionSnapshot.symbol == f"{underlying} {expiry} {strike} {right}"
                            )
                        ).first()
                        if snap is None:
                            snap = OptionSnapshot(symbol=db_symbol)
                            db.add(snap)
                        else:
                            snap.symbol = db_symbol

                        # Update conId if we now have a real one
                        if conId:
                            snap.conId = conId
                        elif snap.conId is None:
                            snap.conId = 0

                        snap.updated_at = datetime.now()
                        snap.delta = cboe_data.delta
                        snap.gamma = cboe_data.gamma
                        snap.theta = cboe_data.theta
                        snap.vega = cboe_data.vega
                        snap.implied_vol = cboe_data.implied_vol
                        snap.underlying_price = cboe_data.underlying_price
                        snap.last_price = cboe_data.last_price
                        snap.volume = cboe_data.volume or 0
                        snap.open_interest = cboe_data.open_interest or 0

                        # Store actual last trade time from CBOE
                        if cboe_data.last_date:
                            try:
                                snap.last_trade_date = datetime.fromisoformat(cboe_data.last_date)
                            except (ValueError, TypeError):
                                snap.last_trade_date = None
                        else:
                            snap.last_trade_date = None

                        db.commit()
                        logger.info(f"Cached CBOE data for {db_symbol}")
                    except Exception as db_e:
                        logger.error(f"Failed to cache CBOE data: {db_e}")

                    return cboe_data
            except Exception as e:
                logger.debug(f"CBOE check failed: {e}")

        # 3. Live IBKR data
        qualified = None
        try:
            client = await get_ib()
            client.reqMarketDataType(4)

            if conId:
                contract = Option(conId=conId)
                qualified = await client.qualifyContractsAsync(contract)
                if qualified and qualified[0]:
                    logger.info(f"Qualified option via conId={conId}")

            if not qualified or not qualified[0]:
                if prefix_currency:
                    ticker = underlying
                    currency = prefix_currency
                    exchange = prefix_exchange or "SMART"
                else:
                    ticker, _, currency = parse_symbol(underlying)
                    exchange = "SMART"

                qualified = await _qualify_option_contract(
                    client,
                    ticker,
                    expiry,
                    strike,
                    right,
                    currency,
                    exchange
                )

        except Exception as e:
            logger.warning(f"Live qualification failed: {e}")
            if snap:
                logger.info("Connection failed, falling back to STALE cache")
                effective_date = snap.last_trade_date or snap.updated_at
                cached_last_price = snap.last_price or 0.0
                return OptionGreeks(
                    symbol=snap.symbol,
                    delta=snap.delta or 0.0,
                    gamma=snap.gamma or 0.0,
                    vega=snap.vega or 0.0,
                    theta=snap.theta or 0.0,
                    implied_vol=snap.implied_vol or 0.0,
                    underlying_price=snap.underlying_price or 0.0,
                    last_price=cached_last_price if cached_last_price > 0 else 0.0,
                    volume=snap.volume or 0,
                    open_interest=snap.open_interest or 0,
                    last_date=effective_date.strftime("%Y-%m-%d %H:%M:%S") if effective_date else None
                )
            raise e

        if not qualified or not qualified[0]:
            if snap:
                logger.warning(
                    f"Contract qualification failed for {underlying}, serving STALE cache.")
                effective_date = snap.last_trade_date or snap.updated_at
                cached_last_price = snap.last_price or 0.0
                return OptionGreeks(
                    symbol=snap.symbol,
                    delta=snap.delta or 0.0,
                    gamma=snap.gamma or 0.0,
                    vega=snap.vega or 0.0,
                    theta=snap.theta or 0.0,
                    implied_vol=snap.implied_vol or 0.0,
                    underlying_price=snap.underlying_price or 0.0,
                    last_price=cached_last_price if cached_last_price > 0 else 0.0,
                    volume=snap.volume or 0,
                    open_interest=snap.open_interest or 0,
                    last_date=effective_date.strftime("%Y-%m-%d %H:%M:%S") if effective_date else None
                )
            raise HTTPException(
                status_code=404,
                detail=f"Option contract not found: {underlying} {expiry} {strike} {right}")

        # Request market data and wait for valid Greeks
        max_retries = 3
        best_g = None
        best_t = None

        async def _fetch_market_data_with_retries():
            nonlocal best_g, best_t
            for attempt in range(max_retries):
                client.reqMktData(qualified[0], '', False, False)

                t = None
                g = None

                try:
                    for _ in range(50):
                        await asyncio.sleep(0.1)
                        t = client.ticker(qualified[0])
                        if t:
                            g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks
                            if _greeks_are_valid(g):
                                break
                            if _ >= 30 and (t.last is not None and not math.isnan(t.last)):
                                break
                finally:
                    client.cancelMktData(qualified[0])

                current_g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks if t else None
                current_last = t.last if (t and t.last is not None and not math.isnan(t.last)) else None

                is_valid = _greeks_are_valid(current_g)
                has_price = current_last is not None and current_last > 0

                if is_valid:
                    best_g = current_g
                    best_t = t
                    logger.info(f"Fetched valid Greeks on attempt {attempt+1}/{max_retries}")
                    return

                if has_price:
                    if best_t is None:
                        best_t = t
                        best_g = current_g

                if attempt < max_retries - 1:
                    logger.info(f"Attempt {attempt+1}/{max_retries} for {display_symbol} yielded incomplete data. Retrying...")
                    await asyncio.sleep(1.0)
                else:
                    logger.warning(f"Failed to fetch valid Greeks for {display_symbol} after {max_retries} attempts.")
                    if best_t is None:
                        best_t = t
                        best_g = current_g

        try:
            await asyncio.wait_for(_fetch_market_data_with_retries(), timeout=20.0)
        except asyncio.TimeoutError:
            logger.warning(f"Global timeout (20s) fetching market data for {display_symbol}")

        t = best_t
        g = best_g

        if not t or not (g or (t.last is not None and not math.isnan(t.last))):
            if snap:
                logger.warning(
                    f"No live data received for {underlying}, serving STALE cache.")
                effective_date = snap.last_trade_date or snap.updated_at
                cached_last_price = snap.last_price or 0.0
                return OptionGreeks(
                    symbol=snap.symbol,
                    delta=snap.delta or 0.0,
                    gamma=snap.gamma or 0.0,
                    vega=snap.vega or 0.0,
                    theta=snap.theta or 0.0,
                    implied_vol=snap.implied_vol or 0.0,
                    underlying_price=snap.underlying_price or 0.0,
                    last_price=cached_last_price if cached_last_price > 0 else 0.0,
                    volume=snap.volume or 0,
                    open_interest=snap.open_interest or 0,
                    last_date=effective_date.strftime("%Y-%m-%d %H:%M:%S") if effective_date else None
                )
            raise HTTPException(
                status_code=404,
                detail="No live market data received and no cache available")

        t_vol = getattr(t, 'volume', None)
        t_oi = getattr(t, 'openInterest', None)
        t_last = getattr(t, 'last', None)
        t_time = getattr(t, 'lastTime', None)

        def safe_float(val):
            """Return 0.0 if val is None or NaN."""
            return val if (val is not None and not math.isnan(val)) else 0.0

        has_valid_greeks = _greeks_are_valid(g)
        has_valid_price = t_last is not None and not math.isnan(t_last) and t_last > 0

        # Determine if this is a non-US option during closed EU hours
        if prefix_exchange:
            is_likely_us_for_save = False
        else:
            is_likely_us_for_save = '.' not in underlying or underlying in ['SPX', 'VIX', 'NDX', 'RUT']
        eu_closed = not is_likely_us_for_save and not _is_market_open()

        should_save = False
        if qualified and qualified[0]:
             if has_valid_greeks:
                 should_save = True
             elif has_valid_price:
                 should_save = True
             elif eu_closed and g is not None:
                 # EU market is closed — frozen data is the best we'll get.
                 # Cache it to avoid hammering the gateway on every request.
                 should_save = True
                 logger.info(f"EU closed: caching frozen data for {display_symbol} (Greeks may be partial)")

        if should_save:
            cid = qualified[0].conId
            snap = db.query(OptionSnapshot).filter(
                or_(
                    OptionSnapshot.symbol == display_symbol,
                    OptionSnapshot.symbol == f"{underlying} {expiry} {strike} {right}"
                )
            ).first()
            if snap is None:
                snap = OptionSnapshot(symbol=display_symbol)
                db.add(snap)
            else:
                snap.symbol = display_symbol
            snap.conId = cid  # Always update: may have been 0 from a prior CBOE save

            snap.updated_at = datetime.now()
            snap.delta = safe_float(g.delta) if g else 0.0
            snap.gamma = safe_float(g.gamma) if g else 0.0
            snap.theta = safe_float(g.theta) if g else 0.0
            snap.vega = safe_float(g.vega) if g else 0.0
            snap.implied_vol = safe_float(g.impliedVol) if g else 0.0
            snap.underlying_price = safe_float(g.undPrice) if g else 0.0
            # Filter IBKR's -1 sentinel (means "no data")
            snap.last_price = safe_float(t_last) if (t_last is not None and t_last > 0) else 0.0
            snap.volume = int(t_vol) if (t_vol is not None and not math.isnan(t_vol)) else 0
            snap.open_interest = int(t_oi) if (t_oi is not None and not math.isnan(t_oi)) else 0

            # Store actual last trade time from IBKR (allow NULL database entry if missing)
            snap.last_trade_date = t_time  # t_time is a datetime object or None

            db.commit()
            logger.info(f"Cached data for {display_symbol} (conId={cid}, Greeks={has_valid_greeks}, Price={has_valid_price})")
        elif qualified and qualified[0]:
            logger.warning(f"Skipping DB cache for {display_symbol}: Greeks data invalid (all zeros or missing underlying price)")

        return OptionGreeks(
            symbol=display_symbol,
            delta=safe_float(g.delta) if g else 0.0,
            gamma=safe_float(g.gamma) if g else 0.0,
            vega=safe_float(g.vega) if g else 0.0,
            theta=safe_float(g.theta) if g else 0.0,
            implied_vol=safe_float(g.impliedVol) if g else 0.0,
            underlying_price=safe_float(g.undPrice) if g else 0.0,
            volume=int(t_vol) if (
                t_vol is not None and not math.isnan(t_vol)) else 0,
            open_interest=int(t_oi) if (
                t_oi is not None and not math.isnan(t_oi)) else 0,
            last_price=t_last if (
                t_last is not None and not math.isnan(t_last) and t_last > 0) else 0.0,
            last_date=t_time.strftime("%Y-%m-%d %H:%M:%S") if t_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(
            f"Error fetching option greeks for {underlying} {expiry} {strike} {right}: {e}")
        raise HTTPException(status_code=500, detail="Internal error processing option greeks request")


@router.get("/option/risk/{symbol}", response_model=OptionGreeks,
             dependencies=[Depends(verify_key)])
async def get_option_risk(symbol: str):
    """
    Fetch Greeks for an option symbol.

    Supports two formats:
    1. OSI Format (US options): TICKER YYMMDD C/P STRIKE (continuous string)
       Example: ASTS251114P00050000
    2. IBKR localSymbol format (European options): R TICKER YYYYMMDD STRIKE M
       Example: P HMI  20260220 1900 M
    """
    client = await get_ib()
    client.reqMarketDataType(4)

    try:
        symbol = symbol.strip()

        prefix_exchange = None
        prefix_currency = None

        if ':' in symbol:
            parts = symbol.split(':')
            if len(parts) == 2:
                prefix = parts[0].upper()
                symbol = parts[1]

                if prefix in EXCHANGE_PREFIXES:
                    prefix_exchange, prefix_currency = EXCHANGE_PREFIXES[prefix]
                    logger.info(f"Using explicit prefix {prefix} -> {prefix_exchange}, {prefix_currency}")

        is_european_format = len(symbol) > 2 and symbol[0] in ('P', 'C') and symbol[1] == ' '

        ticker = ""
        expiry = ""
        strike_val = 0.0
        right = ""
        currency = "USD"

        if prefix_currency:
            currency = prefix_currency

        if is_european_format:
            try:
                parsed = parse_european_symbol(symbol)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid option symbol format: {symbol}")

            right = parsed["right"]
            raw_ticker = parsed["ticker"]
            expiry = parsed["expiry"]
            strike_val = parsed["strike"]

            if not prefix_currency:
                ticker, exchange, currency = parse_symbol(raw_ticker)
            else:
                ticker = raw_ticker

            if currency == "USD" and '.' not in raw_ticker and not prefix_currency:
                currency = "EUR"
        else:
            parsed = parse_osi_symbol(symbol)
            strike_val = parsed["strike"]
            right = parsed["right"]
            expiry = parsed["expiry"]
            raw_ticker = parsed["ticker"]

            if prefix_currency:
                ticker = raw_ticker
            else:
                ticker, exchange, currency = parse_symbol(raw_ticker)

        qualified = await _qualify_option_contract(
            client,
            ticker,
            expiry,
            strike_val,
            'P' if right == 'P' else 'C',
            currency,
            prefix_exchange or "SMART"
        )

        if not qualified or not qualified[0]:
             if is_european_format and currency == 'USD':
                 qualified = await _qualify_option_contract(
                    client, ticker, expiry, strike_val, 'P' if right == 'P' else 'C', 'EUR'
                 )

        if not qualified or not qualified[0]:
            raise HTTPException(
                status_code=404,
                detail=f"Option contract not found for {symbol}")

        # Request Data and wait (with global timeout)
        client.reqMktData(qualified[0], '', False, False)

        t = None

        async def _fetch_risk_data():
            nonlocal t
            for _ in range(50):
                await asyncio.sleep(0.1)
                t = client.ticker(qualified[0])
                if t:
                    g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks
                    if g or (t.last is not None and not math.isnan(t.last)):
                        break

        try:
            await asyncio.wait_for(_fetch_risk_data(), timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout (8s) fetching risk data for {symbol}")
        finally:
            client.cancelMktData(qualified[0])

        if not t:
            raise HTTPException(status_code=404,
                                detail="No market data received after waiting")

        g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks

        t_vol = getattr(t, 'volume', None)
        t_oi = getattr(t, 'openInterest', None)
        t_last = getattr(t, 'last', None)
        t_time = getattr(t, 'lastTime', None)

        def safe_float(val):
            return val if (val is not None and not math.isnan(val)) else 0.0

        return OptionGreeks(
            symbol=symbol,
            delta=safe_float(g.delta) if g else 0.0,
            gamma=safe_float(g.gamma) if g else 0.0,
            vega=safe_float(g.vega) if g else 0.0,
            theta=safe_float(g.theta) if g else 0.0,
            implied_vol=safe_float(g.impliedVol) if g else 0.0,
            underlying_price=safe_float(g.undPrice) if g else 0.0,
            volume=int(t_vol) if (
                t_vol is not None and not math.isnan(t_vol)) else 0,
            open_interest=int(t_oi) if (
                t_oi is not None and not math.isnan(t_oi)) else 0,
            last_price=safe_float(t_last),
            last_date=t_time.strftime("%Y-%m-%d %H:%M:%S") if t_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error fetching option risk for {symbol}: {e}")
        raise HTTPException(status_code=500, detail="Internal error processing option risk request")


@router.get("/options/chain/{symbol}",
             response_model=List[OptionChainItem], dependencies=[Depends(verify_key)])
async def get_option_chain(symbol: str):
    """
    Get available option expirations and strikes for a given underlying symbol.
    Returns option chain parameters from IBKR.
    """
    client = await get_ib()

    ticker, exchange, currency = parse_symbol(symbol)

    contract = Contract(
        symbol=ticker,
        secType="STK",
        exchange=exchange,
        currency=currency)
    qualified = await client.qualifyContractsAsync(contract)
    if not qualified:
        raise HTTPException(status_code=404,
                            detail=f"Underlying {symbol} not found")

    underlying = qualified[0]

    try:
        chains = await client.reqSecDefOptParamsAsync(
            underlying.symbol,
            "",
            underlying.secType,
            underlying.conId
        )
    except Exception as e:
        logger.error(f"Error fetching option chain for {symbol}: {e}")
        raise HTTPException(status_code=500,
                            detail="Error fetching option chain")

    if not chains:
        raise HTTPException(status_code=404,
                            detail=f"No option chain found for {symbol}")

    items = []
    for chain in chains:
        expirations = sorted(chain.expirations) if chain.expirations else []
        strikes = sorted(chain.strikes) if chain.strikes else []

        items.append(OptionChainItem(
            exchange=chain.exchange,
            underlyingConId=chain.underlyingConId,
            tradingClass=chain.tradingClass,
            multiplier=chain.multiplier,
            expirations=expirations,
            strikes=strikes
        ))

    return items


@router.get("/options/chain/{symbol}/quotes",
             response_model=OptionChainQuotesResponse,
             dependencies=[Depends(verify_key)])
async def get_option_chain_quotes(
    symbol: str,
    expiry: str,
    min_strike: Optional[float] = None,
    max_strike: Optional[float] = None,
    strikes_below: Optional[int] = 5,
    strikes_above: Optional[int] = 5,
    strikes_count: Optional[int] = None,
    exchange: Optional[str] = "DTB",
    right: Optional[str] = "BOTH",
    db: Session = Depends(get_db)
):
    """
    Get option chain quotes with live/delayed-frozen market data for a given expiration
    and strike range. Specifically designed for European (EUREX/DTB) and US options.
    Returns strike-by-strike bid, ask, volume, open interest, greeks, and intrinsic/extrinsic values.
    """
    client = await get_ib()

    clean_symbol = symbol.strip()
    prefix_exchange = None
    prefix_currency = None

    if ':' in clean_symbol:
        parts = clean_symbol.split(':')
        if len(parts) == 2:
            prefix = parts[0].upper()
            clean_symbol = parts[1]
            if prefix in EXCHANGE_PREFIXES:
                prefix_exchange, prefix_currency = EXCHANGE_PREFIXES[prefix]

    ticker, parsed_exchange, currency = parse_symbol(clean_symbol)
    stk_exchange = prefix_exchange or parsed_exchange
    stk_currency = prefix_currency or currency

    contract = Contract(
        symbol=ticker,
        secType="STK",
        exchange=stk_exchange,
        currency=stk_currency
    )
    qualified = await client.qualifyContractsAsync(contract)
    if not qualified or not qualified[0]:
        contract_smart = Contract(
            symbol=ticker,
            secType="STK",
            exchange="SMART",
            currency=stk_currency
        )
        qualified = await client.qualifyContractsAsync(contract_smart)

    if not qualified or not qualified[0]:
        raise HTTPException(status_code=404, detail=f"Underlying {symbol} not found")

    underlying = qualified[0]

    # Fetch underlying spot price for ATM calculations and moneyness
    client.reqMarketDataType(4)
    underlying_price = 0.0
    try:
        client.reqMktData(underlying, '', False, False)
        for _ in range(15):
            await asyncio.sleep(0.1)
            t_und = client.ticker(underlying)
            if t_und:
                m_price = t_und.marketPrice()
                if not math.isnan(m_price) and m_price > 0:
                    underlying_price = m_price
                    break
                if t_und.last is not None and not math.isnan(t_und.last) and t_und.last > 0:
                    underlying_price = t_und.last
                    break
                if t_und.close is not None and not math.isnan(t_und.close) and t_und.close > 0:
                    underlying_price = t_und.close
                    break
        client.cancelMktData(underlying)
    except Exception as e:
        logger.debug(f"Could not fetch underlying spot price for {underlying.symbol}: {e}")

    try:
        chains = await client.reqSecDefOptParamsAsync(
            underlying.symbol,
            "",
            underlying.secType,
            underlying.conId
        )
    except Exception as e:
        logger.error(f"Error fetching option chain definitions for {symbol}: {e}")
        raise HTTPException(status_code=500, detail="Error fetching option chain parameters")

    if not chains:
        raise HTTPException(status_code=404, detail=f"No option chain found for {symbol}")

    clean_expiry = expiry.strip().replace("-", "")
    target_exchange = (exchange or "DTB").strip().upper()

    # Match target exchange and expiry
    selected_chain = None
    for chain in chains:
        if chain.exchange.upper() == target_exchange and clean_expiry in chain.expirations:
            selected_chain = chain
            break

    if not selected_chain:
        for chain in chains:
            if clean_expiry in chain.expirations:
                selected_chain = chain
                break

    if not selected_chain:
        all_expirations = sorted(list({exp for c in chains for exp in c.expirations}))
        raise HTTPException(
            status_code=404,
            detail=f"Expiration {clean_expiry} not found for {symbol}. Available expirations: {all_expirations[:12]}"
        )

    # Filter strikes: use strikes_below / strikes_above (or fallback to strikes_count)
    if strikes_count is not None:
        below = strikes_count // 2
        above = strikes_count // 2
    else:
        below = strikes_below if strikes_below is not None else 5
        above = strikes_above if strikes_above is not None else 5

    filtered_strikes = filter_strikes_window(
        selected_chain.strikes,
        min_strike=min_strike,
        max_strike=max_strike,
        underlying_price=underlying_price,
        strikes_below=below,
        strikes_above=above,
        max_limit=20
    )

    if not filtered_strikes:
        raise HTTPException(status_code=404, detail="No strikes matched filter criteria")

    target_right = (right or "BOTH").strip().upper()
    fetch_call = target_right in ("BOTH", "C", "CALL")
    fetch_put = target_right in ("BOTH", "P", "PUT")

    # Build contracts
    contracts_to_qualify = []
    for s in filtered_strikes:
        if fetch_call:
            c = Option(
                symbol=underlying.symbol,
                lastTradeDateOrContractMonth=clean_expiry,
                strike=s,
                right="C",
                exchange=selected_chain.exchange,
                multiplier=selected_chain.multiplier,
                currency=underlying.currency,
                tradingClass=selected_chain.tradingClass
            )
            contracts_to_qualify.append(c)
        if fetch_put:
            p = Option(
                symbol=underlying.symbol,
                lastTradeDateOrContractMonth=clean_expiry,
                strike=s,
                right="P",
                exchange=selected_chain.exchange,
                multiplier=selected_chain.multiplier,
                currency=underlying.currency,
                tradingClass=selected_chain.tradingClass
            )
            contracts_to_qualify.append(p)

    try:
        qualified_options = await client.qualifyContractsAsync(*contracts_to_qualify)
    except Exception as e:
        logger.error(f"Error qualifying option contracts: {e}")
        qualified_options = contracts_to_qualify

    valid_contracts = [c for c in qualified_options if c and getattr(c, 'conId', 0)]

    # Request market data with generic ticks: 100 (Volume), 101 (Open Interest), 106 (Greeks & IV)
    for c in valid_contracts:
        client.reqMktData(c, "100,101,106", False, False)

    try:
        # Give gateway time to stream quotes and greeks
        for _ in range(25):
            await asyncio.sleep(0.1)
    finally:
        for c in valid_contracts:
            client.cancelMktData(c)

    def safe_f(v):
        return float(v) if (v is not None and not math.isnan(v)) else 0.0

    def safe_i(v):
        return int(v) if (v is not None and not math.isnan(v)) else 0

    quotes_by_key = {}  # (strike, right) -> OptionQuoteItem

    for c in valid_contracts:
        t = client.ticker(c)
        r = c.right.upper()
        s = c.strike

        bid = safe_f(t.bid) if t and t.bid != -1 else 0.0
        bid_size = safe_i(t.bidSize) if t else 0
        ask = safe_f(t.ask) if t and t.ask != -1 else 0.0
        ask_size = safe_i(t.askSize) if t else 0
        last = safe_f(t.last) if t and t.last != -1 and t.last > 0 else 0.0
        mid = (bid + ask) * 0.5 if (bid > 0 and ask > 0) else (last or bid or ask or 0.0)

        # Volume
        vol = safe_i(getattr(t, 'volume', 0)) if t else 0
        if vol == 0 and t:
            vol = safe_i(getattr(t, 'callVolume' if r == 'C' else 'putVolume', 0))

        # Open Interest (tickType 27 / 28)
        oi = 0
        if t:
            if r == 'C':
                oi = safe_i(getattr(t, 'callOpenInterest', 0))
            else:
                oi = safe_i(getattr(t, 'putOpenInterest', 0))
            if oi == 0:
                oi = safe_i(getattr(t, 'openInterest', 0))

        # Greeks
        g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks if t else None
        delta = safe_f(g.delta) if g else 0.0
        gamma = safe_f(g.gamma) if g else 0.0
        theta = safe_f(g.theta) if g else 0.0
        vega = safe_f(g.vega) if g else 0.0
        iv = safe_f(g.impliedVol) if g else 0.0

        if underlying_price <= 0.0 and g and safe_f(g.undPrice) > 0.0:
            underlying_price = safe_f(g.undPrice)

        intrinsic = calc_option_intrinsic(r, s, underlying_price)
        effective_price = last if last > 0 else mid
        extrinsic = calc_option_extrinsic(effective_price, intrinsic)

        last_date_str = None
        if t and t.lastTime:
            last_date_str = t.lastTime.strftime("%Y-%m-%d %H:%M:%S")

        symbol_name = c.localSymbol or f"{underlying.symbol} {clean_expiry} {s} {r}"

        quotes_by_key[(s, r)] = OptionQuoteItem(
            conId=c.conId,
            symbol=symbol_name,
            right=r,
            strike=s,
            bid=bid,
            bid_size=bid_size,
            ask=ask,
            ask_size=ask_size,
            mid=round(mid, 4),
            last_price=last,
            volume=vol,
            open_interest=oi,
            implied_vol=round(iv, 4),
            delta=round(delta, 4),
            gamma=round(gamma, 4),
            theta=round(theta, 4),
            vega=round(vega, 4),
            intrinsic_value=round(intrinsic, 2),
            extrinsic_value=round(extrinsic, 2),
            last_date=last_date_str
        )

    # Build strike rows
    strike_rows = []
    for s in filtered_strikes:
        c_quote = quotes_by_key.get((s, "C"))
        p_quote = quotes_by_key.get((s, "P"))
        moneyness = calc_moneyness_pct(s, underlying_price)
        strike_rows.append(StrikeChainRow(
            strike=s,
            moneyness_pct=moneyness,
            call=c_quote,
            put=p_quote
        ))

    return OptionChainQuotesResponse(
        symbol=underlying.symbol,
        underlying_price=round(underlying_price, 2),
        expiry=clean_expiry,
        exchange=selected_chain.exchange,
        trading_class=selected_chain.tradingClass,
        multiplier=selected_chain.multiplier,
        strikes=strike_rows
    )

