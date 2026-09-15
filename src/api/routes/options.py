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
    calc_option_mid,
    clean_price,
    clean_size,
    clean_greek,
    calc_moneyness_pct,
    filter_strikes_window,
    select_best_option_chain,
    calc_bs_greeks,
    is_market_open_for_symbol,
    determine_market_statuses,
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


def _build_option_greeks_from_snap(snap: OptionSnapshot, right: str, strike: float) -> OptionGreeks:
    has_g = _snap_is_valid(snap)
    delta = round(snap.delta, 4) if (has_g and snap.delta is not None) else None
    gamma = round(snap.gamma, 4) if (has_g and snap.gamma is not None) else None
    theta = round(snap.theta, 4) if (has_g and snap.theta is not None) else None
    vega = round(snap.vega, 4) if (has_g and snap.vega is not None) else None
    iv = round(snap.implied_vol, 4) if (has_g and snap.implied_vol is not None and snap.implied_vol > 0) else None

    bid = clean_price(snap.bid)
    bid_size = clean_size(snap.bid_size) if bid is not None else None
    ask = clean_price(snap.ask)
    ask_size = clean_size(snap.ask_size) if ask is not None else None
    mid = calc_option_mid(bid, ask)

    last_price = clean_price(snap.last_price) if (snap.last_price and snap.last_price > 0) else None
    last_date = snap.last_trade_date.strftime("%Y-%m-%d %H:%M:%S") if (snap.last_trade_date and last_price is not None) else None

    und_price = clean_price(snap.underlying_price)
    intrinsic = round(calc_option_intrinsic(right, strike, und_price), 4) if (und_price and strike > 0) else None
    extrinsic = round(max(mid - intrinsic, 0.0), 4) if (mid is not None and intrinsic is not None) else None

    is_open = is_market_open_for_symbol(snap.symbol)
    statuses = determine_market_statuses(
        is_open=is_open,
        has_bid_ask=(bid is not None or ask is not None),
        has_greeks=(delta is not None or iv is not None),
        source="db"
    )

    return OptionGreeks(
        symbol=snap.symbol,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        implied_vol=iv,
        underlying_price=und_price,
        bid=bid,
        bid_size=bid_size,
        ask=ask,
        ask_size=ask_size,
        mid=mid,
        intrinsic_value=intrinsic,
        extrinsic_value=extrinsic,
        last_price=last_price,
        volume=snap.volume or 0,
        open_interest=snap.open_interest or 0,
        last_date=last_date,
        market_data_status=statuses["market_data_status"],
        quote_status=statuses["quote_status"],
        greeks_status=statuses["greeks_status"]
    )


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
            return _build_option_greeks_from_snap(snap, right, strike)

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
                        snap.bid = cboe_data.bid
                        snap.bid_size = cboe_data.bid_size
                        snap.ask = cboe_data.ask
                        snap.ask_size = cboe_data.ask_size

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
                return _build_option_greeks_from_snap(snap, right, strike)
            raise e

        if not qualified or not qualified[0]:
            if snap:
                logger.warning(
                    f"Contract qualification failed for {underlying}, serving STALE cache.")
                return _build_option_greeks_from_snap(snap, right, strike)
            raise HTTPException(
                status_code=404,
                detail=f"Option contract not found: {underlying} {expiry} {strike} {right}")

        # Request market data and wait for valid Greeks / Quote
        max_retries = 3
        best_g = None
        best_t = None

        async def _fetch_market_data_with_retries():
            nonlocal best_g, best_t
            for attempt in range(max_retries):
                client.reqMktData(qualified[0], '100,101,106', False, False)

                t = None
                g = None

                try:
                    for _ in range(50):
                        await asyncio.sleep(0.1)
                        t = client.ticker(qualified[0])
                        if t:
                            g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks
                            has_g = _greeks_are_valid(g)
                            has_b = t.bid is not None and not math.isnan(t.bid) and t.bid >= 0
                            has_a = t.ask is not None and not math.isnan(t.ask) and t.ask >= 0
                            if has_g and (has_b or has_a):
                                break
                            if _ >= 30 and (has_g or (has_b and has_a) or (t.last is not None and not math.isnan(t.last) and t.last > 0)):
                                break
                finally:
                    client.cancelMktData(qualified[0])

                current_g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks if t else None
                current_last = t.last if (t and t.last is not None and not math.isnan(t.last)) else None
                has_b = t and t.bid is not None and not math.isnan(t.bid) and t.bid >= 0
                has_a = t and t.ask is not None and not math.isnan(t.ask) and t.ask >= 0

                is_valid = _greeks_are_valid(current_g)
                has_price = current_last is not None and current_last > 0
                has_quote = has_b or has_a

                if is_valid and has_quote:
                    best_g = current_g
                    best_t = t
                    logger.info(f"Fetched valid Greeks and Quote on attempt {attempt+1}/{max_retries}")
                    return

                if is_valid or has_price or has_quote:
                    if best_t is None:
                        best_t = t
                        best_g = current_g

                if attempt < max_retries - 1:
                    logger.info(f"Attempt {attempt+1}/{max_retries} for {display_symbol} yielded incomplete data. Retrying...")
                    await asyncio.sleep(1.0)
                else:
                    logger.warning(f"Failed to fetch complete market data for {display_symbol} after {max_retries} attempts.")
                    if best_t is None:
                        best_t = t
                        best_g = current_g

        try:
            await asyncio.wait_for(_fetch_market_data_with_retries(), timeout=20.0)
        except asyncio.TimeoutError:
            logger.warning(f"Global timeout (20s) fetching market data for {display_symbol}")

        t = best_t
        g = best_g

        has_bid = t and t.bid is not None and not math.isnan(t.bid) and t.bid >= 0
        has_ask = t and t.ask is not None and not math.isnan(t.ask) and t.ask >= 0

        if not t or not (g or (t.last is not None and not math.isnan(t.last) and t.last > 0) or has_bid or has_ask):
            if snap:
                logger.warning(
                    f"No live data received for {underlying}, serving STALE cache.")
                return _build_option_greeks_from_snap(snap, right, strike)
            raise HTTPException(
                status_code=404,
                detail="No live market data received and no cache available")

        t_vol = getattr(t, 'volume', None)
        t_oi = getattr(t, 'openInterest', None)
        t_last = getattr(t, 'last', None)
        t_time = getattr(t, 'time', None) or getattr(t, 'lastTime', None)

        bid = clean_price(t.bid) if t else None
        bid_size = clean_size(t.bidSize) if (t and bid is not None) else None
        ask = clean_price(t.ask) if t else None
        ask_size = clean_size(t.askSize) if (t and ask is not None) else None
        mid = calc_option_mid(bid, ask)

        last_trade = clean_price(t_last) if (t_last is not None and t_last > 0) else None
        last_price = last_trade
        last_date = None
        if last_trade is not None:
            if t_time and hasattr(t_time, 'strftime'):
                last_date = t_time.strftime("%Y-%m-%d %H:%M:%S")
        elif snap and snap.last_price and snap.last_price > 0:
            last_price = snap.last_price
            if snap.last_trade_date and hasattr(snap.last_trade_date, 'strftime'):
                last_date = snap.last_trade_date.strftime("%Y-%m-%d %H:%M:%S")

        has_valid_greeks = _greeks_are_valid(g)
        if has_valid_greeks:
            delta = round(clean_greek(g.delta), 4) if clean_greek(g.delta) is not None else None
            gamma = round(clean_greek(g.gamma), 4) if clean_greek(g.gamma) is not None else None
            theta = round(clean_greek(g.theta), 4) if clean_greek(g.theta) is not None else None
            vega = round(clean_greek(g.vega), 4) if clean_greek(g.vega) is not None else None
            raw_iv = clean_greek(g.impliedVol)
            iv = round(raw_iv, 4) if (raw_iv is not None and raw_iv > 0) else None
            raw_und = clean_greek(g.undPrice)
            und_price = raw_und if (raw_und is not None and raw_und > 0) else None
        elif snap and _snap_is_valid(snap):
            delta = round(snap.delta, 4) if snap.delta is not None else None
            gamma = round(snap.gamma, 4) if snap.gamma is not None else None
            theta = round(snap.theta, 4) if snap.theta is not None else None
            vega = round(snap.vega, 4) if snap.vega is not None else None
            iv = round(snap.implied_vol, 4) if (snap.implied_vol is not None and snap.implied_vol > 0) else None
            und_price = snap.underlying_price if (snap.underlying_price and snap.underlying_price > 0) else None
        else:
            delta = gamma = theta = vega = iv = None
            und_price = snap.underlying_price if (snap and snap.underlying_price and snap.underlying_price > 0) else None

        intrinsic = round(calc_option_intrinsic(right, strike, und_price), 4) if (und_price and strike > 0) else None
        extrinsic = round(max(mid - intrinsic, 0.0), 4) if (mid is not None and intrinsic is not None) else None

        has_valid_price = last_trade is not None
        has_valid_quote = (bid is not None) or (ask is not None)

        # Determine if this is a non-US option during closed EU hours
        if prefix_exchange:
            is_likely_us_for_save = False
        else:
            is_likely_us_for_save = '.' not in underlying or underlying in ['SPX', 'VIX', 'NDX', 'RUT']
        eu_closed = not is_likely_us_for_save and not _is_market_open()

        should_save = False
        if qualified and qualified[0]:
            if has_valid_greeks or has_valid_price or has_valid_quote:
                should_save = True
            elif eu_closed and g is not None:
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
            snap.conId = cid

            snap.updated_at = datetime.now()
            snap.delta = delta
            snap.gamma = gamma
            snap.theta = theta
            snap.vega = vega
            snap.implied_vol = iv
            snap.underlying_price = und_price
            if last_trade is not None:
                snap.last_price = last_trade
                if t_time and hasattr(t_time, 'strftime'):
                    snap.last_trade_date = t_time
            snap.volume = int(t_vol) if (t_vol is not None and not math.isnan(t_vol) and t_vol >= 0) else (snap.volume or 0)
            snap.open_interest = int(t_oi) if (t_oi is not None and not math.isnan(t_oi) and t_oi >= 0) else (snap.open_interest or 0)
            if bid is not None:
                snap.bid = bid
                snap.bid_size = bid_size
            if ask is not None:
                snap.ask = ask
                snap.ask_size = ask_size

            is_open_sym = is_market_open_for_symbol(
                symbol=underlying,
                exchange=prefix_exchange or (qualified[0].exchange if qualified and qualified[0] else None),
                currency=prefix_currency or (qualified[0].currency if qualified and qualified[0] else None)
            )
            mkt_data_type = getattr(t, 'marketDataType', 1) if t else 1
            statuses = determine_market_statuses(
                is_open=is_open_sym,
                has_bid_ask=(bid is not None or ask is not None),
                has_greeks=(delta is not None or iv is not None),
                market_data_type=mkt_data_type,
                source="ibkr"
            )
            snap.market_data_status = statuses["market_data_status"]
            snap.quote_status = statuses["quote_status"]
            try:
                db.commit()
                logger.info(f"Cached data for {display_symbol} (conId={cid}, Greeks={has_valid_greeks}, Price={has_valid_price}, Quote={has_valid_quote})")
            except Exception as commit_err:
                logger.warning(f"Failed to cache data for {display_symbol}: {commit_err}. Retrying with fresh transaction...")
                db.rollback()
                try:
                    fresh_snap = db.query(OptionSnapshot).filter(
                        or_(
                            OptionSnapshot.symbol == display_symbol,
                            OptionSnapshot.symbol == f"{underlying} {expiry} {strike} {right}"
                        )
                    ).first()
                    if fresh_snap:
                        fresh_snap.delta = delta
                        fresh_snap.gamma = gamma
                        fresh_snap.theta = theta
                        fresh_snap.vega = vega
                        fresh_snap.implied_vol = iv
                        fresh_snap.underlying_price = und_price
                        if last_trade is not None:
                            fresh_snap.last_price = last_trade
                        if bid is not None:
                            fresh_snap.bid = bid
                            fresh_snap.bid_size = bid_size
                        if ask is not None:
                            fresh_snap.ask = ask
                            fresh_snap.ask_size = ask_size
                        fresh_snap.market_data_status = statuses["market_data_status"]
                        fresh_snap.quote_status = statuses["quote_status"]
                        fresh_snap.greeks_status = statuses["greeks_status"]
                        fresh_snap.updated_at = datetime.now()
                        db.commit()
                except Exception as retry_err:
                    logger.warning(f"Retry commit failed for {display_symbol}: {retry_err}")
                    db.rollback()
        elif qualified and qualified[0]:
            logger.warning(f"Skipping DB cache for {display_symbol}: No valid market data or Greeks")
            is_open_sym = is_market_open_for_symbol(
                symbol=underlying,
                exchange=prefix_exchange or (qualified[0].exchange if qualified and qualified[0] else None),
                currency=prefix_currency or (qualified[0].currency if qualified and qualified[0] else None)
            )
            mkt_data_type = getattr(t, 'marketDataType', 1) if t else 1
            statuses = determine_market_statuses(
                is_open=is_open_sym,
                has_bid_ask=(bid is not None or ask is not None),
                has_greeks=(delta is not None or iv is not None),
                market_data_type=mkt_data_type,
                source="ibkr"
            )
        else:
            statuses = {"market_data_status": "CLOSED", "quote_status": "CLOSED", "greeks_status": "CLOSED"}

        return OptionGreeks(
            symbol=display_symbol,
            delta=delta,
            gamma=gamma,
            vega=vega,
            theta=theta,
            implied_vol=iv,
            underlying_price=und_price,
            bid=bid,
            bid_size=bid_size,
            ask=ask,
            ask_size=ask_size,
            mid=mid,
            intrinsic_value=intrinsic,
            extrinsic_value=extrinsic,
            volume=int(t_vol) if (
                t_vol is not None and not math.isnan(t_vol) and t_vol >= 0) else 0,
            open_interest=int(t_oi) if (
                t_oi is not None and not math.isnan(t_oi) and t_oi >= 0) else 0,
            last_price=last_price,
            last_date=last_date,
            market_data_status=statuses["market_data_status"],
            quote_status=statuses["quote_status"],
            greeks_status=statuses["greeks_status"]
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
        client.reqMktData(qualified[0], '100,101,106', False, False)

        t = None

        async def _fetch_risk_data():
            nonlocal t
            for _ in range(50):
                await asyncio.sleep(0.1)
                t = client.ticker(qualified[0])
                if t:
                    g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks
                    has_g = _greeks_are_valid(g)
                    has_b = t.bid is not None and not math.isnan(t.bid) and t.bid >= 0
                    has_a = t.ask is not None and not math.isnan(t.ask) and t.ask >= 0
                    if has_g and (has_b or has_a):
                        break
                    if _ >= 30 and (has_g or (has_b and has_a) or (t.last is not None and not math.isnan(t.last) and t.last > 0)):
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
        t_time = getattr(t, 'time', None) or getattr(t, 'lastTime', None)

        bid = clean_price(t.bid) if t else None
        bid_size = clean_size(t.bidSize) if (t and bid is not None) else None
        ask = clean_price(t.ask) if t else None
        ask_size = clean_size(t.askSize) if (t and ask is not None) else None
        mid = calc_option_mid(bid, ask)

        last_trade = clean_price(t_last) if (t_last is not None and t_last > 0) else None
        last_date = t_time.strftime("%Y-%m-%d %H:%M:%S") if (t_time and hasattr(t_time, 'strftime') and last_trade is not None) else None

        has_valid_greeks = _greeks_are_valid(g)
        if has_valid_greeks:
            delta = round(clean_greek(g.delta), 4) if clean_greek(g.delta) is not None else None
            gamma = round(clean_greek(g.gamma), 4) if clean_greek(g.gamma) is not None else None
            theta = round(clean_greek(g.theta), 4) if clean_greek(g.theta) is not None else None
            vega = round(clean_greek(g.vega), 4) if clean_greek(g.vega) is not None else None
            raw_iv = clean_greek(g.impliedVol)
            iv = round(raw_iv, 4) if (raw_iv is not None and raw_iv > 0) else None
            raw_und = clean_greek(g.undPrice)
            und_price = raw_und if (raw_und is not None and raw_und > 0) else None
        else:
            delta = gamma = theta = vega = iv = und_price = None

        intrinsic = round(calc_option_intrinsic(right, strike_val, und_price), 4) if (und_price and strike_val > 0) else None
        extrinsic = round(max(mid - intrinsic, 0.0), 4) if (mid is not None and intrinsic is not None) else None

        is_open_sym = is_market_open_for_symbol(
            symbol=symbol,
            exchange=qualified[0].exchange if qualified and qualified[0] else None,
            currency=qualified[0].currency if qualified and qualified[0] else None
        )
        mkt_data_type = getattr(t, 'marketDataType', 1) if t else 1
        statuses = determine_market_statuses(
            is_open=is_open_sym,
            has_bid_ask=(bid is not None or ask is not None),
            has_greeks=(delta is not None or iv is not None),
            market_data_type=mkt_data_type,
            source="ibkr"
        )

        return OptionGreeks(
            symbol=symbol,
            delta=delta,
            gamma=gamma,
            vega=vega,
            theta=theta,
            implied_vol=iv,
            underlying_price=und_price,
            bid=bid,
            bid_size=bid_size,
            ask=ask,
            ask_size=ask_size,
            mid=mid,
            intrinsic_value=intrinsic,
            extrinsic_value=extrinsic,
            volume=int(t_vol) if (
                t_vol is not None and not math.isnan(t_vol) and t_vol >= 0) else 0,
            open_interest=int(t_oi) if (
                t_oi is not None and not math.isnan(t_oi) and t_oi >= 0) else 0,
            last_price=last_trade,
            last_date=last_date,
            market_data_status=statuses["market_data_status"],
            quote_status=statuses["quote_status"],
            greeks_status=statuses["greeks_status"]
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
        exchange="SMART",
        currency=stk_currency
    )
    if stk_exchange and stk_exchange != "SMART":
        contract.primaryExchange = stk_exchange

    qualified = await client.qualifyContractsAsync(contract)
    if not qualified or not qualified[0]:
        contract_raw = Contract(
            symbol=ticker,
            secType="STK",
            exchange=stk_exchange,
            currency=stk_currency
        )
        qualified = await client.qualifyContractsAsync(contract_raw)

    if not qualified or not qualified[0]:
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
    trading_class: Optional[str] = None,
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
        exchange="SMART",
        currency=stk_currency
    )
    if stk_exchange and stk_exchange != "SMART":
        contract.primaryExchange = stk_exchange

    qualified = await client.qualifyContractsAsync(contract)
    if not qualified or not qualified[0]:
        contract_raw = Contract(
            symbol=ticker,
            secType="STK",
            exchange=stk_exchange,
            currency=stk_currency
        )
        qualified = await client.qualifyContractsAsync(contract_raw)

    if not qualified or not qualified[0]:
        raise HTTPException(status_code=404, detail=f"Underlying {symbol} not found")

    underlying = qualified[0]

    # Fetch underlying spot price for ATM calculations and moneyness
    client.reqMarketDataType(4)
    underlying_price = 0.0
    try:
        client.reqMktData(underlying, '', False, False)
        for _ in range(20):
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
                if (t_und.bid is not None and not math.isnan(t_und.bid) and t_und.bid > 0
                        and t_und.ask is not None and not math.isnan(t_und.ask) and t_und.ask > 0):
                    underlying_price = (t_und.bid + t_und.ask) * 0.5
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

    # Smart selection of the primary / most liquid trading class
    selected_chain = select_best_option_chain(
        chains,
        target_exchange=target_exchange,
        expiry=clean_expiry,
        trading_class=trading_class,
        underlying_symbol=ticker,
    )

    if not selected_chain:
        all_expirations = sorted(list({exp for c in chains for exp in c.expirations}))
        all_classes = sorted(list({c.tradingClass for c in chains}))
        if trading_class:
            raise HTTPException(
                status_code=404,
                detail=f"No option chain found matching trading_class='{trading_class}' for {symbol} on expiry {clean_expiry}. Available trading classes: {all_classes}"
            )
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

        bid = clean_price(t.bid) if t else None
        bid_size = clean_size(t.bidSize) if (t and bid is not None) else None
        ask = clean_price(t.ask) if t else None
        ask_size = clean_size(t.askSize) if (t and ask is not None) else None
        mid = calc_option_mid(bid, ask)

        last_trade = clean_price(t.last) if (t and t.last is not None and t.last > 0) else None

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

        # Greeks: find candidate with valid IV or delta
        g = None
        for cand in (t.modelGreeks, t.lastGreeks, t.bidGreeks, t.askGreeks) if t else ():
            if cand and cand.impliedVol is not None and not math.isnan(cand.impliedVol) and cand.impliedVol > 0:
                g = cand
                break
        if not g and t:
            for cand in (t.modelGreeks, t.lastGreeks, t.bidGreeks, t.askGreeks):
                if cand and cand.delta is not None and not math.isnan(cand.delta):
                    g = cand
                    break

        raw_iv = clean_greek(g.impliedVol) if g else None
        iv_cand = raw_iv if (raw_iv is not None and raw_iv > 0) else 0.0

        if underlying_price <= 0.0 and g and clean_greek(g.undPrice) and clean_greek(g.undPrice) > 0.0:
            underlying_price = clean_greek(g.undPrice)

        if _greeks_are_valid(g):
            delta = round(clean_greek(g.delta), 4) if clean_greek(g.delta) is not None else None
            gamma = round(clean_greek(g.gamma), 4) if clean_greek(g.gamma) is not None else None
            theta = round(clean_greek(g.theta), 4) if clean_greek(g.theta) is not None else None
            vega = round(clean_greek(g.vega), 4) if clean_greek(g.vega) is not None else None
            iv = round(iv_cand, 4) if iv_cand > 0 else None
        elif underlying_price > 0.0 and iv_cand > 0.0 and clean_expiry:
            bs = calc_bs_greeks(r, underlying_price, s, clean_expiry, iv_cand)
            delta = bs["delta"]
            gamma = bs["gamma"]
            theta = bs["theta"]
            vega = bs["vega"]
            iv = round(iv_cand, 4)
        else:
            delta = gamma = theta = vega = iv = None

        intrinsic = round(calc_option_intrinsic(r, s, underlying_price), 2) if (underlying_price > 0 and s > 0) else None
        extrinsic = round(max(mid - intrinsic, 0.0), 2) if (mid is not None and intrinsic is not None) else None

        last_date_str = None
        t_time = getattr(t, 'time', None) or getattr(t, 'lastTime', None)
        if t_time and hasattr(t_time, 'strftime') and last_trade is not None:
            last_date_str = t_time.strftime("%Y-%m-%d %H:%M:%S")

        symbol_name = c.localSymbol or f"{underlying.symbol} {clean_expiry} {s} {r}"

        mkt_data_type = getattr(t, 'marketDataType', 1) if t else 1
        is_open_chain = is_market_open_for_symbol(
            symbol=underlying.symbol,
            exchange=selected_chain.exchange,
            currency=underlying.currency
        )
        statuses = determine_market_statuses(
            is_open=is_open_chain,
            has_bid_ask=(bid is not None or ask is not None),
            has_greeks=(delta is not None or iv is not None),
            market_data_type=mkt_data_type,
            source="ibkr"
        )

        quotes_by_key[(s, r)] = OptionQuoteItem(
            conId=c.conId,
            symbol=symbol_name,
            right=r,
            strike=s,
            bid=bid,
            bid_size=bid_size,
            ask=ask,
            ask_size=ask_size,
            mid=mid,
            last_price=last_trade,
            volume=vol,
            open_interest=oi,
            implied_vol=iv,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            intrinsic_value=intrinsic,
            extrinsic_value=extrinsic,
            last_date=last_date_str,
            market_data_status=statuses["market_data_status"],
            quote_status=statuses["quote_status"],
            greeks_status=statuses["greeks_status"]
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

    is_open_chain = is_market_open_for_symbol(
        symbol=underlying.symbol,
        exchange=selected_chain.exchange,
        currency=underlying.currency
    )
    chain_status = "CLOSED" if not is_open_chain else ("DELAYED" if any(getattr(client.ticker(c), 'marketDataType', 1) == 3 for c in valid_contracts[:5]) else "LIVE")

    return OptionChainQuotesResponse(
        symbol=underlying.symbol,
        underlying_price=round(underlying_price, 2),
        expiry=clean_expiry,
        exchange=selected_chain.exchange,
        trading_class=selected_chain.tradingClass,
        multiplier=selected_chain.multiplier,
        market_data_status=chain_status,
        strikes=strike_rows
    )

