from fastapi.security import APIKeyHeader, APIKeyQuery
import os
import asyncio
import logging
import math
import httpx
from datetime import datetime
from typing import List
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from ib_async import IB, Option, Contract, ExecutionFilter

from src.config import settings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from src.models import AccountSummary, PositionItem, CurrencyItem, OptionGreeks, OrderItem, TradeItem, ContractDetailsItem, MarketSnapshot, OptionChainItem, OptionSnapshot, MarketCache

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ibkr-api")


# Database Setup - Robust resolution for Docker environments
db_url = os.environ.get("DB_URL") or settings.DB_URL

if not db_url:
    # If DB_URL is still empty, reconstruct it from individual components
    db_user = os.environ.get("DB_USER")
    db_pass = os.environ.get("DB_PASSWORD")
    db_name = os.environ.get("DB_NAME", "ibkr")
    if db_user and db_pass:
        db_url = f"mysql+pymysql://{db_user}:{db_pass}@{settings.PROJECT_ID}-db/{db_name}"
        logger.info("Constructed DB_URL from individual components")

if not db_url:
    logger.error("DB_URL is not set and could not be reconstructed.")
    # We'll let create_engine fail with a clear error if it proceeds

engine = create_engine(db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


app = FastAPI(title="IBKR API", version="1.0.0")
ib = IB()


@app.get("/health")
async def health_check():
    """Liveness probe endpoint."""
    return {"status": "ok"}


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
api_key_query = APIKeyQuery(name="api_key", auto_error=False)


async def verify_key(
    header: str = Security(api_key_header),
    query: str = Security(api_key_query)
):
    if header == settings.API_KEY:
        return header
    if query == settings.API_KEY:
        return query
    raise HTTPException(status_code=403, detail="Invalid API Key")

# Market suffix mappings for international stocks
MARKET_SUFFIXES = {
    ".L": ("LSE", "GBP"),      # London Stock Exchange
    ".DE": ("IBIS", "EUR"),    # Germany (Xetra)
    ".PA": ("SBF", "EUR"),     # France (Euronext Paris)
    ".AS": ("AEB", "EUR"),     # Netherlands (Amsterdam)
    ".SW": ("EBS", "CHF"),     # Switzerland
    ".MC": ("BM", "EUR"),      # Spain (Madrid)
    ".MI": ("BVME", "EUR"),    # Italy (Milan)
}


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
        # 1. Parse Ticker for CBOE (Indices need special handling)
        cboe_ticker = ticker.upper()
        # Common indices on CBOE often need an underscore prefix if accessing via certain endpoints,
        # but the delayed_quotes endpoint usually takes the symbol directly.
        # However, for consistency with some CBOE conventions:
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

        # 2. Construct Option ID (OCC format)
        # Format: TICKER + YYMMDD + C/P + 00000000 (strike * 1000)
        # Expiry is YYYYMMDD, need YYMMDD
        yy_expiry = expiry[2:] 
        strike_int = int(round(strike * 1000))
        strike_str = f"{strike_int:08d}"
        
        # Ticker in OCC ID should not have special chars usually, but CBOE API response 
        # 'option' field matches the CBOE ticker format.
        # If ticker was 'SPX', option ID starts with 'SPX'.
        clean_ticker = ticker.upper().replace('.', '')
        # Special case: N.Y -> NY, etc.

        option_id = f"{clean_ticker}{yy_expiry}{right}{strike_str}"
        
        # 3. Find the option in the list
        option_data = None
        for opt in data['data']['options']:
            if opt['option'] == option_id:
                option_data = opt
                break
        
        if not option_data:
            return None

        # 4. Map to OptionGreeks
        # CBOE fields: delta, gamma, theta, iv, open_interest, volume, last_trade_price, last_trade_time
        
        def val_or_zero(val):
            if val is None: return 0.0
            if isinstance(val, str):
                try: 
                    return float(val) 
                except: 
                    return 0.0
            return float(val)

        return OptionGreeks(
            symbol=f"{ticker} {expiry} {strike} {right} (CBOE)",
            delta=abs(val_or_zero(option_data.get('delta'))),
            gamma=val_or_zero(option_data.get('gamma')),
            vega=val_or_zero(option_data.get('vega')),
            theta=abs(val_or_zero(option_data.get('theta'))),
            implied_vol=val_or_zero(option_data.get('iv')),
            underlying_price=val_or_zero(data.get('data', {}).get('current_price')), # Underlying price from top level
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

    Requires at least one non-zero Greek AND a non-zero underlying price.
    """
    if not g:
        return False

    def _safe(val):
        return val if (val is not None and not math.isnan(val)) else 0.0

    delta = _safe(g.delta)
    gamma = _safe(g.gamma)
    theta = _safe(g.theta)
    vega = _safe(g.vega)
    und = _safe(und_price_override if und_price_override is not None else g.undPrice)

    if delta == 0 and gamma == 0 and theta == 0 and vega == 0:
        return False
    if und == 0:
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
    u = snap.underlying_price or 0.0
    if d == 0 and g == 0 and t == 0 and v == 0:
        return False
    if u == 0:
        return False
    return True


async def get_ib():
    if not ib.isConnected():
        retries = 3
        delay = 2
        for i in range(retries):
            try:
                logger.info(f"Connecting to IBKR Gateway (Attempt {i + 1}/{retries})...")
                await ib.connectAsync(
                    settings.IB_HOST,
                    settings.IB_PORT,
                    clientId=settings.IB_CLIENT_ID
                )
                logger.info("Connected to IBKR Gateway")
                return ib
            except Exception as e:
                logger.warning(f"Connection attempt {i + 1} failed: {e}")
                if i < retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error("All connection attempts failed.")
                    raise HTTPException(
                        status_code=503, detail="Could not connect to IBKR")
    return ib


@app.get("/account/summary", response_model=AccountSummary,
         dependencies=[Depends(verify_key)])
async def get_summary():
    client = await get_ib()
    v = client.accountValues()

    def get_val(tag, currency=None, default="0"):
        # Most tags we want are either 'BASE' or the account's primary currency
        # We search for the tag with a currency first, then fallback to any.
        matches = [x for x in v if x.tag == tag]
        if not matches:
            return default

        # If we specify a preference, try that first
        if currency:
            for m in matches:
                if m.currency == currency:
                    return m.value

        # Otherwise, prioritize 'BASE' then non-empty currency
        for m in matches:
            if m.currency == 'BASE':
                return m.value

        return matches[0].value

    net_liq_obj = next(
        (x for x in v if x.tag == 'NetLiquidation' and x.currency != 'BASE'),
        None)
    if not net_liq_obj:
        net_liq_obj = next((x for x in v if x.tag == 'NetLiquidation'), None)

    base_curr = net_liq_obj.currency if net_liq_obj else "Unknown"

    # Fetch Daily P&L using the pnl() function
    # This requires subscribing to P&L updates first
    account_id = client.managedAccounts(
    )[0] if client.managedAccounts() else ""
    daily_pnl = 0.0
    daily_realized = 0.0

    if account_id:
        # Try to subscribe, ignore if already subscribed
        try:
            client.reqPnL(account_id)
            await asyncio.sleep(0.5)  # Wait for P&L data to arrive
        except AssertionError:
            # Already subscribed, just use existing data
            pass

        # Try to get PnL data - handle both dict-like and list returns
        try:
            pnl_result = client.pnl()
            pnl_data = None

            # If it's a list, search for our account
            if isinstance(pnl_result, list):
                for p in pnl_result:
                    if hasattr(p, 'account') and p.account == account_id:
                        pnl_data = p
                        break
                if pnl_data is None and pnl_result:
                    pnl_data = pnl_result[0]
            # If it's a dict, try to get by account
            elif isinstance(pnl_result, dict):
                pnl_data = pnl_result.get(account_id) or (
                    list(pnl_result.values())[0] if pnl_result else None)
            # If it's a single PnL object
            elif hasattr(pnl_result, 'dailyPnL'):
                pnl_data = pnl_result

            if pnl_data:
                if hasattr(pnl_data, 'dailyPnL') and pnl_data.dailyPnL is not None and not math.isnan(
                        pnl_data.dailyPnL):
                    daily_pnl = pnl_data.dailyPnL
                if hasattr(pnl_data, 'realizedPnL') and pnl_data.realizedPnL is not None and not math.isnan(
                        pnl_data.realizedPnL):
                    daily_realized = pnl_data.realizedPnL
        except Exception as e:
            logger.error(f"Error fetching PnL: {e}")
    else:
        daily_pnl = 0.0
        daily_realized = 0.0

    return AccountSummary(
        NetLiquidation=float(get_val('NetLiquidationByCurrency', 'BASE')),
        AvailableMargin=float(get_val('FullAvailableMargin', base_curr)),
        Cushion=float(get_val('Cushion', '')),
        Currency=base_curr,
        BuyingPower=float(get_val('BuyingPower', base_curr)),
        ExcessLiquidity=float(get_val('ExcessLiquidity', base_curr)),
        FullMaintMargin=float(get_val('MaintMarginReq', base_curr)),
        EquityWithLoanValue=float(get_val('EquityWithLoanValue', base_curr)),
        TotalCashValue=float(get_val('TotalCashBalance', 'BASE')),
        UnrealizedPnL=float(get_val('UnrealizedPnL', 'BASE')),
        RealizedPnL=float(get_val('RealizedPnL', 'BASE')),
        DailyPnL=daily_pnl,
        DailyRealizedPnL=daily_realized,
        StockMarketValue=float(get_val('StockMarketValue', 'BASE')),
        EUR=float(get_val('CashBalance', 'EUR')),
        USD=float(get_val('CashBalance', 'USD')),
        GBP=float(get_val('CashBalance', 'GBP')),
        CHF=float(get_val('CashBalance', 'CHF')),
        SEK=float(get_val('CashBalance', 'SEK'))
    )


@app.get("/account/positions",
         response_model=List[PositionItem], dependencies=[Depends(verify_key)])
async def get_positions():
    client = await get_ib()
    items = []
    for p in client.positions():
        item = PositionItem(
            symbol=p.contract.localSymbol,
            qty=p.position,
            cost=p.avgCost,
            secType=p.contract.secType,
            conId=p.contract.conId
        )
        if p.contract.secType == 'OPT':
            item.expiry = p.contract.lastTradeDateOrContractMonth
            item.strike = p.contract.strike
            item.right = p.contract.right
            item.underlying = p.contract.symbol
        items.append(item)
    return items


@app.get("/account/currencies",
         response_model=List[CurrencyItem], dependencies=[Depends(verify_key)])
async def get_currencies():
    client = await get_ib()
    return [
        CurrencyItem(currency=v.currency, amount=float(v.value))
        for v in client.accountValues()
        if v.tag == 'CashBalance' and v.currency != 'BASE'
    ]


@app.get("/option/greeks", response_model=OptionGreeks,
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

        if right not in ('P', 'C'):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid right: {right}. Must be P or C")

        from datetime import timedelta
        snap = None

        # 0. Try to resolve conId and/or snap from DB first if conId is missing
        if not conId:
            # Try to find a snapshot that matches the contract parameters
            # Format in DB is: TICKER YYYYMMDD STRIKE RIGHT
            # We use a pattern match to be flexible
            pattern = f"{underlying}%{strike}%{right}"
            snap = db.query(OptionSnapshot).filter(
                OptionSnapshot.symbol.like(pattern)).first()
            if snap:
                conId = snap.conId
                logger.info(
                    f"Resolved conId={conId} from database snapshot for {underlying}")

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

        # 2. Check Cache
        if conId:
            if not snap:
                snap = db.query(OptionSnapshot).filter(
                    OptionSnapshot.conId == conId).first()

            # If we have a fresh cache (< 60 mins) with valid data, return it
            if not force_refresh and snap and snap.updated_at > datetime.now() - \
                    timedelta(minutes=60) and _snap_is_valid(snap):
                logger.info(f"Serving fresh cached greeks for conId={conId}")
                return OptionGreeks(
                    symbol=snap.symbol,
                    delta=snap.delta or 0.0,
                    gamma=snap.gamma or 0.0,
                    vega=snap.vega or 0.0,
                    theta=snap.theta or 0.0,
                    implied_vol=snap.implied_vol or 0.0,
                    underlying_price=snap.underlying_price or 0.0,
                    last_price=snap.last_price or 0.0,
                    volume=0,
                    open_interest=0,
                    last_date=snap.updated_at.strftime("%Y-%m-%d %H:%M:%S") if snap.updated_at else None
                )
            elif not force_refresh and snap and snap.updated_at > datetime.now() - \
                    timedelta(minutes=60) and not _snap_is_valid(snap):
                logger.warning(f"Cached data for conId={conId} has invalid Greeks, forcing live fetch")

        # 3. If we get here, we want live data (or cache was stale)
        qualified = None
        try:
            client = await get_ib()
            client.reqMarketDataType(4)
                logger.warning(f"Cached data for conId={conId} has invalid Greeks, forcing live fetch")

        # 2b. Check CBOE (for US options largely)
        # We try this BEFORE connecting to IBKR if we don't have a valid cache.
        # This is a "best effort" attempt.
        if not force_refresh:
            # Simple heuristic: if it looks like a US ticker (no suffix), try CBOE
            # or if we explicitly know it's US.
            is_likely_us = '.' not in underlying or underlying in ['SPX', 'VIX', 'NDX', 'RUT']
            
            if is_likely_us:
                try:
                    cboe_data = await _fetch_cboe_greeks(underlying, expiry, strike, right)
                    if cboe_data:
                        logger.info(f"Fetched Greeks from CBOE for {underlying} {expiry} {strike} {right}")
                        return cboe_data
                except Exception as e:
                    logger.debug(f"CBOE check failed: {e}")

        # 3. If we get here, we want live data (or cache was stale)
            if conId:
                contract = Option(conId=conId)
                qualified = await client.qualifyContractsAsync(contract)
                if qualified and qualified[0]:
                    logger.info(f"Qualified option via conId={conId}")

            if not qualified or not qualified[0]:
                # Fallback: try symbol-based qualification with parse_symbol
                ticker, _, currency = parse_symbol(underlying)
                contract = Option(
                    ticker,
                    expiry,
                    strike,
                    right,
                    'SMART',
                    currency=currency)
                qualified = await client.qualifyContractsAsync(contract)

                # Fallback: try symbol-based qualification via parallel
                # execution for other currencies
                if not qualified or not qualified[0]:
                    alt_currencies = [
                        c for c in [
                            'USD',
                            'EUR',
                            'GBP',
                            'CHF'] if c != currency]
                    tasks = []
                    for alt in alt_currencies:
                        c = Option(
                            ticker,
                            expiry,
                            strike,
                            right,
                            'SMART',
                            currency=alt)
                        tasks.append(client.qualifyContractsAsync(c))

                    # Run all qualification attempts in parallel
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for res in results:
                        if isinstance(res, list) and res and res[0]:
                            qualified = res
                            logger.info(f"Qualified option {underlying} {expiry} {strike} {right} via parallel fallback (currency={res[0].currency})")
                            break

                    if not qualified or not qualified[0]:
                        # Dynamic Fallback: Search for the underlying symbol
                        logger.info(f"Performing dynamic symbol search for {underlying}...")
                        descriptions = await client.reqMatchingSymbolsAsync(underlying)

                        best_match = None
                        for d in descriptions:
                            if d.contract.secType == 'STK' and d.contract.symbol == underlying:
                                best_match = d.contract
                                break

                        if best_match:
                            logger.info(f"Found match: {best_match.symbol} on {best_match.primaryExchange or best_match.exchange} ({best_match.currency})")
                            # Try to qualify option using the specific currency from the stock
                            c = Option(
                                ticker,
                                expiry,
                                strike,
                                right,
                                'SMART',
                                currency=best_match.currency)
                            # Sometimes the exchange also needs to be explicit if SMART fails, but usually currency is enough for top liquid stocks
                            qualified = await client.qualifyContractsAsync(c)
        except Exception as e:
            logger.warning(f"Live qualification failed: {e}")
            if snap:
                logger.info("Connection failed, falling back to STALE cache")
                return OptionGreeks(
                    symbol=snap.symbol,
                    delta=snap.delta or 0.0,
                    gamma=snap.gamma or 0.0,
                    vega=snap.vega or 0.0,
                    theta=snap.theta or 0.0,
                    implied_vol=snap.implied_vol or 0.0,
                    underlying_price=snap.underlying_price or 0.0,
                    last_price=snap.last_price or 0.0,
                    volume=0,
                    open_interest=0
                )
            raise e  # re-raise to be caught by outer block or handled as 503

        if not qualified or not qualified[0]:
            if snap:
                logger.warning(
                    f"Contract qualification failed for {underlying}, serving STALE cache.")
                return OptionGreeks(
                    symbol=snap.symbol,
                    delta=snap.delta or 0.0,
                    gamma=snap.gamma or 0.0,
                    vega=snap.vega or 0.0,
                    theta=snap.theta or 0.0,
                    implied_vol=snap.implied_vol or 0.0,
                    underlying_price=snap.underlying_price or 0.0,
                    last_price=snap.last_price or 0.0,
                    volume=0,
                    open_interest=0
                )
            raise HTTPException(
                status_code=404,
                detail=f"Option contract not found: {underlying} {expiry} {strike} {right}")

        # Request market data and wait for valid Greeks
        client.reqMktData(qualified[0], '', False, False)

        t = None
        g = None
        for _ in range(80):  # Wait up to 8 seconds for valid Greeks
            await asyncio.sleep(0.1)
            t = client.ticker(qualified[0])
            if t:
                g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks
                if _greeks_are_valid(g):
                    break
                # Accept last price only after 5s if no Greeks arrive
                if _ >= 50 and (t.last is not None and not math.isnan(t.last)):
                    break

        client.cancelMktData(qualified[0])

        if not t or not (t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks or (
                t.last is not None and not math.isnan(t.last))):
            if snap:
                logger.warning(
                    f"No live data received for {underlying}, serving STALE cache.")
                return OptionGreeks(
                    symbol=snap.symbol,
                    delta=snap.delta or 0.0,
                    gamma=snap.gamma or 0.0,
                    vega=snap.vega or 0.0,
                    theta=snap.theta or 0.0,
                    implied_vol=snap.implied_vol or 0.0,
                    underlying_price=snap.underlying_price or 0.0,
                    last_price=snap.last_price or 0.0,
                    volume=0,
                    open_interest=0
                )
            raise HTTPException(
                status_code=404,
                detail="No live market data received and no cache available")

        g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks

        t_vol = getattr(t, 'volume', None)
        t_oi = getattr(t, 'openInterest', None)
        t_last = getattr(t, 'last', None)
        t_time = getattr(t, 'lastTime', None)

        def safe_float(val):
            """Return 0.0 if val is None or NaN."""
            return val if (val is not None and not math.isnan(val)) else 0.0

        display_symbol = f"{underlying} {expiry} {strike} {right}"

        # Update Cache — only if Greeks are valid
        if qualified and qualified[0] and _greeks_are_valid(g):
            cid = qualified[0].conId
            snap = db.query(OptionSnapshot).filter(
                OptionSnapshot.conId == cid).first()
            if not snap:
                snap = OptionSnapshot(conId=cid)
                db.add(snap)

            snap.symbol = display_symbol
            snap.updated_at = datetime.now()
            snap.delta = safe_float(g.delta) if g else 0.0
            snap.gamma = safe_float(g.gamma) if g else 0.0
            snap.theta = safe_float(g.theta) if g else 0.0
            snap.vega = safe_float(g.vega) if g else 0.0
            snap.implied_vol = safe_float(g.impliedVol) if g else 0.0
            snap.underlying_price = safe_float(g.undPrice) if g else 0.0
            snap.last_price = safe_float(t_last)

            db.commit()
            logger.info(f"Cached valid Greeks for {display_symbol} (conId={cid})")
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
                t_last is not None and not math.isnan(t_last)) else 0.0,
            last_date=t_time.strftime("%Y-%m-%d %H:%M:%S") if t_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(
            f"Error fetching option greeks for {underlying} {expiry} {strike} {right}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/option/risk/{symbol}", response_model=OptionGreeks,
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
    client.reqMarketDataType(4)  # Delayed-Frozen fallback

    try:
        symbol = symbol.strip()

        # Detect format:
        # European format: starts with "P " or "C " (right first), e.g., "P HMI  20260220 1900 M"
        # OSI Format: ends with YYMMDD + P/C + 8-digit strike, e.g., "ASTS  260109P00065000"
        #             May have padding spaces between ticker and date

        # Check if it's European format (starts with P or C followed by space)
        is_european_format = len(symbol) > 2 and symbol[0] in (
            'P', 'C') and symbol[1] == ' '

        if is_european_format:
            # European/IBKR localSymbol format: "P HMI  20260220 1900 M"
            # Format: RIGHT SYMBOL YYYYMMDD STRIKE MULTIPLIER
            parts = symbol.split()

            if len(parts) < 4:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid option symbol format: {symbol}")

            # Parse based on position
            right = parts[0]  # P or C
            raw_ticker = parts[1]  # HMI, RMS, etc.
            expiry = parts[2]  # YYYYMMDD (already in correct format)
            # Strike price as-is (no division needed)
            strike_val = float(parts[3])
            # parts[4] is multiplier indicator (M), ignored for contract
            # creation

            # Parse ticker for international stocks (e.g., HMI.PA -> SBF/EUR)
            # Note: European option tickers usually don't have suffix in localSymbol,
            # but the underlying might have been originally specified with one
            ticker, exchange, currency = parse_symbol(raw_ticker)

            # For European format options without suffix, default to EUR instead of USD
            # since most European options trade in EUR
            if currency == "USD" and '.' not in raw_ticker:
                currency = "EUR"
        else:
            # OSI Format (US options): "ASTS  260109P00065000" or "ASTS260109P00065000"
            # Remove any internal spaces (padding between ticker and date)
            symbol_clean = symbol.replace(' ', '')

            # Strike: Last 8 chars (divided by 1000)
            strike_val = float(symbol_clean[-8:]) / 1000.0
            # Right: -9 char
            right = symbol_clean[-9]
            # Expiry: -15 to -9 (YYMMDD)
            expiry_raw = symbol_clean[-15:-9]
            expiry = f"20{expiry_raw[0:2]}{expiry_raw[2:4]}{expiry_raw[4:6]}"
            # Ticker: everything before expiry (may contain market suffix like
            # .L)
            raw_ticker = symbol_clean[:-15].strip()

            # Parse ticker for international stocks (e.g., BATS.L -> LSE/GBP)
            ticker, exchange, currency = parse_symbol(raw_ticker)

        # Build contract - try to qualify it
        contract = Option(
            ticker,
            expiry,
            strike_val,
            'P' if right == 'P' else 'C',
            'SMART',
            currency=currency)

        # 2. Qualify Contract
        qualified = await client.qualifyContractsAsync(contract)

        # For European format, if qualification fails with current currency,
        # try alternatives
        if (not qualified or not qualified[0]) and is_european_format:
            # Try with different currencies: EUR, GBP, CHF
            for alt_currency in ['EUR', 'GBP', 'CHF', 'USD']:
                if alt_currency == currency:
                    continue  # Already tried this one
                contract = Option(
                    ticker,
                    expiry,
                    strike_val,
                    'P' if right == 'P' else 'C',
                    'SMART',
                    currency=alt_currency)
                qualified = await client.qualifyContractsAsync(contract)
                if qualified and qualified[0]:
                    logger.info(
                        f"Found European option with currency {alt_currency}: {symbol}")
                    break

        if not qualified or not qualified[0]:
            raise HTTPException(
                status_code=404,
                detail=f"Option contract not found for {symbol}")

        # 3. Request Data and wait for it to arrive
        # Delayed data doesn't always arrive instantly in the first snapshot.
        client.reqMktData(qualified[0], '', False, False)

        t = None

        for _ in range(50):  # Wait up to 5 seconds
            await asyncio.sleep(0.1)
            t = client.ticker(qualified[0])
            if t:
                # Check if we have some data yet (Greeks or last price)
                g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks
                if g or (t.last is not None and not math.isnan(t.last)):
                    break

        # Cleanup subscription
        client.cancelMktData(qualified[0])

        if not t:
            raise HTTPException(status_code=404,
                                detail="No market data received after waiting")

        # Fallback logic: Model -> Bid -> Ask -> Last
        g = t.modelGreeks or t.bidGreeks or t.askGreeks or t.lastGreeks

        t_vol = getattr(t, 'volume', None)
        t_oi = getattr(t, 'openInterest', None)
        t_last = getattr(t, 'last', None)
        t_time = getattr(t, 'lastTime', None)

        return OptionGreeks(
            symbol=symbol,
            delta=g.delta if (g and g.delta is not None) else 0.0,
            gamma=g.gamma if (g and g.gamma is not None) else 0.0,
            vega=g.vega if (g and g.vega is not None) else 0.0,
            theta=g.theta if (g and g.theta is not None) else 0.0,
            implied_vol=g.impliedVol if (
                g and g.impliedVol is not None) else 0.0,
            underlying_price=g.undPrice if (
                g and g.undPrice is not None) else 0.0,
            volume=int(t_vol) if (
                t_vol is not None and not math.isnan(t_vol)) else 0,
            open_interest=int(t_oi) if (
                t_oi is not None and not math.isnan(t_oi)) else 0,
            last_price=t_last if (
                t_last is not None and not math.isnan(t_last)) else 0.0,
            last_date=t_time.strftime("%Y-%m-%d %H:%M:%S") if t_time else None
        )

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error fetching option risk for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/account/orders",
         response_model=List[OrderItem], dependencies=[Depends(verify_key)])
async def get_orders():
    client = await get_ib()
    # Use reqAllOpenOrdersAsync to see orders from other clients (Mobile app,
    # TWS, etc.)
    trades = await client.reqAllOpenOrdersAsync()

    items = []
    for t in trades:
        items.append(OrderItem(
            orderId=t.order.orderId,
            symbol=t.contract.localSymbol,
            action=t.order.action,
            totalQuantity=float(t.order.totalQuantity),
            orderType=t.order.orderType,
            lmtPrice=t.order.lmtPrice if t.order.lmtPrice else None,
            auxPrice=t.order.auxPrice if t.order.auxPrice else None,
            status=t.orderStatus.status
        ))
    return items


@app.get("/account/trades",
         response_model=List[TradeItem], dependencies=[Depends(verify_key)])
async def get_trades():
    client = await get_ib()
    logger.info("Fetching executions from IBKR...")

    # Request executions for the current session
    exec_filter = ExecutionFilter()
    fills = await client.reqExecutionsAsync(exec_filter)

    logger.info(f"reqExecutionsAsync returned {len(fills)} fills")

    # Fallback to already received fills if reqExecutionsAsync is empty
    # (sometimes it returns empty if the connection is very fresh or clientId mismatch)
    if not fills:
        fills = client.fills()
        if fills:
            logger.info(f"Falling back to client.fills(): {len(fills)} found")

    items = []
    # Map execution Id to avoid duplicates if fallback used
    seen_ids = set()

    for f in fills:
        eid = f.execution.execId
        if eid in seen_ids:
            continue
        seen_ids.add(eid)

        items.append(TradeItem(
            executionId=eid,
            symbol=f.contract.localSymbol or f.contract.symbol,
            time=f.time,
            side=f.execution.side,
            shares=float(f.execution.shares),
            price=f.execution.price,
            orderId=f.execution.orderId
        ))

    # Sort by time desc
    items.sort(key=lambda x: x.time, reverse=True)
    logger.info(f"Returning {len(items)} unique trades")
    return items


@app.get("/contract/search",
         response_model=List[ContractDetailsItem], dependencies=[Depends(verify_key)])
async def search_contract(symbol: str, secType: str = "STK"):
    client = await get_ib()

    # Parse symbol for international stocks (e.g., BATS.L -> LSE/GBP)
    ticker, exchange, currency = parse_symbol(symbol)

    # Define search criteria
    if secType == "STK":
        contract = Contract(
            symbol=ticker,
            secType="STK",
            exchange=exchange,
            currency=currency)
    elif secType == "OPT":
        contract = Contract(
            symbol=ticker,
            secType="OPT",
            exchange="SMART",
            currency=currency)
    else:
        contract = Contract(symbol=ticker, secType=secType)

    try:
        details = await client.reqContractDetailsAsync(contract)
    except Exception as e:
        logger.error(f"Error fetching contract details for {symbol}: {e}")
        details = []

    items = []
    # Limit to top 5 results to avoid long response times
    for d in details[:5]:
        c = d.contract

        # 1. Get ISIN if available (secIdList is in ContractDetails, TagValue
        # has 'tag' and 'value')
        isin = next(
            (id.value for id in d.secIdList if id.tag == 'ISIN'),
            None) if d.secIdList else None

        items.append(ContractDetailsItem(
            conId=c.conId,
            symbol=c.symbol,
            secType=c.secType,
            exchange=c.exchange,
            currency=c.currency,
            localSymbol=c.localSymbol,
            longName=d.longName,
            isin=isin
        ))

    return items


@app.get("/market/snapshot/{symbol}", response_model=MarketSnapshot,
         dependencies=[Depends(verify_key)])
async def get_market_snapshot(symbol: str, db: Session = Depends(get_db)):
    """
    Fetch market snapshot for a symbol (Stock or FX).
    Prioritizes DB cache (valid for 60 mins) before hitting IBKR live.
    """
    symbol = symbol.strip().upper()
    from datetime import timedelta

    # 1. Check Cache first
    cache = db.query(MarketCache).filter(MarketCache.symbol == symbol).first()
    if cache and cache.updated_at > datetime.now() - timedelta(minutes=60):
        logger.info(f"Serving cached snapshot for {symbol}")
        return MarketSnapshot(
            symbol=symbol,
            price=cache.price,
            bid=cache.bid,
            ask=cache.ask,
            timestamp=cache.updated_at
        )

    # 2. If not in cache or stale, query live
    client = await get_ib()
    client.reqMarketDataType(4)

    # Parse symbol: Detect 6-char FX pairs (e.g., EURUSD)
    if len(symbol) == 6 and symbol.isalpha():
        # Treat as CASH pair on IDEALPRO
        contract = Contract(symbol=symbol[:3],
                            secType="CASH",
                            exchange="IDEALPRO",
                            currency=symbol[3:])
        logger.info(f"Detected FX pair {symbol}, using CASH contract: {contract.symbol}.{contract.currency}")
    else:
        # Parse symbol for international stocks (e.g., BATS.L -> LSE/GBP)
        ticker, exchange, currency = parse_symbol(symbol)
        contract = Contract(
            symbol=ticker,
            secType="STK",
            exchange=exchange,
            currency=currency)

    qualified = await client.qualifyContractsAsync(contract)
    if not qualified:
        # If live fail but we have STALE cache, return it
        if cache:
            logger.warning(f"Live snapshot failed for {symbol}, serving STALE cache")
            return MarketSnapshot(
                symbol=symbol,
                price=cache.price,
                bid=cache.bid,
                ask=cache.ask,
                timestamp=cache.updated_at
            )
        raise HTTPException(status_code=404, detail="Contract not found")

    contract = qualified[0]

    logger.info(f"Requesting ticker for contract: {contract}")
    try:
        tickers = await client.reqTickersAsync(contract)
        if not tickers:
            if cache:
                logger.warning(f"No live data for {symbol}, serving STALE cache")
                return MarketSnapshot(
                    symbol=symbol,
                    price=cache.price,
                    bid=cache.bid,
                    ask=cache.ask,
                    timestamp=cache.updated_at
                )
            raise HTTPException(status_code=504,
                                detail="No market data received from IBKR")
        t = tickers[0]
    except Exception as e:
        logger.error(f"Error in reqTickersAsync: {e}", exc_info=True)
        if cache:
            logger.warning(f"IBKR Error for {symbol}, serving STALE cache")
            return MarketSnapshot(
                symbol=symbol,
                price=cache.price,
                bid=cache.bid,
                ask=cache.ask,
                timestamp=cache.updated_at
            )
        raise HTTPException(status_code=503, detail=f"IBKR Error: {str(e)}")

    v_last = t.last if (t.last is not None and not math.isnan(t.last)) else None
    v_bid = t.bid if (t.bid is not None and not math.isnan(t.bid)) else None
    v_ask = t.ask if (t.ask is not None and not math.isnan(t.ask)) else None
    v_close = t.close if (t.close is not None and not math.isnan(t.close)) else None

    price = v_last
    if price is None:
        if v_bid and v_ask:
            price = (v_bid + v_ask) / 2
        elif v_close:
            price = v_close
        else:
            price = v_bid or v_ask or 0.0

    logger.info(f"Snapshot for {symbol}: price={price} (live)")

    # 3. Update Cache
    if not cache:
        cache = MarketCache(symbol=symbol)
        db.add(cache)
    
    cache.price = price
    cache.bid = v_bid
    cache.ask = v_ask
    cache.updated_at = datetime.now()
    db.commit()

    return MarketSnapshot(
        symbol=symbol,
        price=price,
        bid=v_bid,
        ask=v_ask,
        timestamp=cache.updated_at
    )


@app.get("/options/chain/{symbol}",
         response_model=List[OptionChainItem], dependencies=[Depends(verify_key)])
async def get_option_chain(symbol: str):
    """
    Get available option expirations and strikes for a given underlying symbol.
    Returns option chain parameters from IBKR.
    """
    client = await get_ib()

    # Parse symbol for international stocks (e.g., BATS.L -> LSE/GBP)
    ticker, exchange, currency = parse_symbol(symbol)

    # First, qualify the underlying contract
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

    # Get option chain parameters
    try:
        chains = await client.reqSecDefOptParamsAsync(
            underlying.symbol,
            "",  # futFopExchange (empty for stocks)
            underlying.secType,
            underlying.conId
        )
    except Exception as e:
        logger.error(f"Error fetching option chain for {symbol}: {e}")
        raise HTTPException(status_code=500,
                            detail=f"Error fetching option chain: {e}")

    if not chains:
        raise HTTPException(status_code=404,
                            detail=f"No option chain found for {symbol}")

    items = []
    for chain in chains:
        # Sort expirations and strikes for cleaner output
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
