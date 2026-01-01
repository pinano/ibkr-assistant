import asyncio
import logging
import math
from datetime import datetime
from typing import List
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from ib_async import IB, Option, Contract, ExecutionFilter

from src.config import settings
from src.models import AccountSummary, PositionItem, CurrencyItem, OptionGreeks, OrderItem, TradeItem, ContractDetailsItem, MarketSnapshot, OptionChainItem

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ibkr-api")

app = FastAPI(title="IBKR API", version="1.0.0")
ib = IB()

@app.get("/health")
async def health_check():
    """Liveness probe endpoint."""
    return {"status": "ok"}


api_key_header = APIKeyHeader(name="X-API-Key")

async def verify_key(header: str = Depends(api_key_header)):
    if header != settings.API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return header

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


async def get_ib():
    if not ib.isConnected():
        retries = 3
        delay = 2
        for i in range(retries):
            try:
                logger.info(f"Connecting to IBKR Gateway (Attempt {i+1}/{retries})...")
                await ib.connectAsync(
                    settings.IB_HOST, 
                    settings.IB_PORT, 
                    clientId=settings.IB_CLIENT_ID
                )
                logger.info("Connected to IBKR Gateway")
                return ib
            except Exception as e:
                logger.warning(f"Connection attempt {i+1} failed: {e}")
                if i < retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error("All connection attempts failed.")
                    raise HTTPException(status_code=503, detail="Could not connect to IBKR")
    return ib

@app.get("/account/summary", response_model=AccountSummary, dependencies=[Depends(verify_key)])
async def get_summary():
    client = await get_ib()
    v = client.accountValues()
    
    def get_val(tag, currency=None, default="0"):
        # Most tags we want are either 'BASE' or the account's primary currency
        # We search for the tag with a currency first, then fallback to any.
        matches = [x for x in v if x.tag == tag]
        if not matches: return default
        
        # If we specify a preference, try that first
        if currency:
            for m in matches:
                if m.currency == currency: return m.value
        
        # Otherwise, prioritize 'BASE' then non-empty currency
        for m in matches:
            if m.currency == 'BASE': return m.value
        
        return matches[0].value

    net_liq_obj = next((x for x in v if x.tag == 'NetLiquidation' and x.currency != 'BASE'), None)
    if not net_liq_obj:
        net_liq_obj = next((x for x in v if x.tag == 'NetLiquidation'), None)
    
    base_curr = net_liq_obj.currency if net_liq_obj else "Unknown"

    # Fetch Daily P&L using the pnl() function
    # This requires subscribing to P&L updates first
    account_id = client.managedAccounts()[0] if client.managedAccounts() else ""
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
                pnl_data = pnl_result.get(account_id) or (list(pnl_result.values())[0] if pnl_result else None)
            # If it's a single PnL object
            elif hasattr(pnl_result, 'dailyPnL'):
                pnl_data = pnl_result
            
            if pnl_data:
                if hasattr(pnl_data, 'dailyPnL') and pnl_data.dailyPnL is not None and not math.isnan(pnl_data.dailyPnL):
                    daily_pnl = pnl_data.dailyPnL
                if hasattr(pnl_data, 'realizedPnL') and pnl_data.realizedPnL is not None and not math.isnan(pnl_data.realizedPnL):
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


@app.get("/account/positions", response_model=List[PositionItem], dependencies=[Depends(verify_key)])
async def get_positions():
    client = await get_ib()
    items = []
    for p in client.positions():
        item = PositionItem(
            symbol=p.contract.localSymbol, 
            qty=p.position, 
            cost=p.avgCost,
            secType=p.contract.secType
        )
        if p.contract.secType == 'OPT':
            item.expiry = p.contract.lastTradeDateOrContractMonth
            item.strike = p.contract.strike
            item.right = p.contract.right
            item.underlying = p.contract.symbol
        items.append(item)
    return items

@app.get("/account/currencies", response_model=List[CurrencyItem], dependencies=[Depends(verify_key)])
async def get_currencies():
    client = await get_ib()
    return [
        CurrencyItem(currency=v.currency, amount=float(v.value)) 
        for v in client.accountValues() 
        if v.tag == 'CashBalance' and v.currency != 'BASE'
    ]



@app.get("/option/risk/{symbol}", response_model=OptionGreeks, dependencies=[Depends(verify_key)])
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
    client.reqMarketDataType(4) # Delayed-Frozen fallback
    
    try:
        symbol = symbol.strip()
        
        # Detect format:
        # European format: starts with "P " or "C " (right first), e.g., "P HMI  20260220 1900 M"
        # OSI Format: ends with YYMMDD + P/C + 8-digit strike, e.g., "ASTS  260109P00065000"
        #             May have padding spaces between ticker and date
        
        # Check if it's European format (starts with P or C followed by space)
        is_european_format = len(symbol) > 2 and symbol[0] in ('P', 'C') and symbol[1] == ' '
        
        if is_european_format:
            # European/IBKR localSymbol format: "P HMI  20260220 1900 M"
            # Format: RIGHT SYMBOL YYYYMMDD STRIKE MULTIPLIER
            parts = symbol.split()
            
            if len(parts) < 4:
                raise HTTPException(status_code=400, detail=f"Invalid option symbol format: {symbol}")
            
            # Parse based on position
            right = parts[0]  # P or C
            raw_ticker = parts[1]  # HMI, RMS, etc.
            expiry = parts[2]  # YYYYMMDD (already in correct format)
            strike_val = float(parts[3])  # Strike price as-is (no division needed)
            # parts[4] is multiplier indicator (M), ignored for contract creation
            
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
            # Ticker: everything before expiry (may contain market suffix like .L)
            raw_ticker = symbol_clean[:-15].strip()
            
            # Parse ticker for international stocks (e.g., BATS.L -> LSE/GBP)
            ticker, exchange, currency = parse_symbol(raw_ticker)

        # Build contract - try to qualify it
        contract = Option(ticker, expiry, strike_val, 'P' if right == 'P' else 'C', 'SMART', currency=currency)

        # 2. Qualify Contract
        qualified = await client.qualifyContractsAsync(contract)
        
        # For European format, if qualification fails with current currency, try alternatives
        if (not qualified or not qualified[0]) and is_european_format:
            # Try with different currencies: EUR, GBP, CHF
            for alt_currency in ['EUR', 'GBP', 'CHF', 'USD']:
                if alt_currency == currency:
                    continue  # Already tried this one
                contract = Option(ticker, expiry, strike_val, 'P' if right == 'P' else 'C', 'SMART', currency=alt_currency)
                qualified = await client.qualifyContractsAsync(contract)
                if qualified and qualified[0]:
                    logger.info(f"Found European option with currency {alt_currency}: {symbol}")
                    break
        
        if not qualified or not qualified[0]:
            raise HTTPException(status_code=404, detail=f"Option contract not found for {symbol}")
        
        # 3. Request Data and wait for it to arrive
        # Delayed data doesn't always arrive instantly in the first snapshot.
        client.reqMktData(qualified[0], '', False, False)
        
        t = None

        for _ in range(50): # Wait up to 5 seconds
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
             raise HTTPException(status_code=404, detail="No market data received after waiting")
             
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
            implied_vol=g.impliedVol if (g and g.impliedVol is not None) else 0.0,
            underlying_price=g.undPrice if (g and g.undPrice is not None) else 0.0,
            volume=int(t_vol) if (t_vol is not None and not math.isnan(t_vol)) else 0,
            open_interest=int(t_oi) if (t_oi is not None and not math.isnan(t_oi)) else 0,
            last_price=t_last if (t_last is not None and not math.isnan(t_last)) else 0.0,
            last_date=t_time.strftime("%Y-%m-%d %H:%M:%S") if t_time else None
        )
        
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        logger.error(f"Error fetching option risk for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/account/orders", response_model=List[OrderItem], dependencies=[Depends(verify_key)])
async def get_orders():
    client = await get_ib()
    # Use reqAllOpenOrdersAsync to see orders from other clients (Mobile app, TWS, etc.)
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

@app.get("/account/trades", response_model=List[TradeItem], dependencies=[Depends(verify_key)])
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
        if eid in seen_ids: continue
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

@app.get("/contract/search", response_model=List[ContractDetailsItem], dependencies=[Depends(verify_key)])
async def search_contract(symbol: str, secType: str = "STK"):
    client = await get_ib()
    
    # Parse symbol for international stocks (e.g., BATS.L -> LSE/GBP)
    ticker, exchange, currency = parse_symbol(symbol)
    
    # Define search criteria
    if secType == "STK":
        contract = Contract(symbol=ticker, secType="STK", exchange=exchange, currency=currency)
    elif secType == "OPT":
        contract = Contract(symbol=ticker, secType="OPT", exchange="SMART", currency=currency)
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
        
        # 1. Get ISIN if available (secIdList is in ContractDetails, TagValue has 'tag' and 'value')
        isin = next((id.value for id in d.secIdList if id.tag == 'ISIN'), None) if d.secIdList else None
        
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

@app.get("/market/snapshot/{symbol}", response_model=MarketSnapshot, dependencies=[Depends(verify_key)])
async def get_market_snapshot(symbol: str):
    client = await get_ib()
    client.reqMarketDataType(4)
    
    # Parse symbol for international stocks (e.g., BATS.L -> LSE/GBP)
    ticker, exchange, currency = parse_symbol(symbol)
    contract = Contract(symbol=ticker, secType="STK", exchange=exchange, currency=currency)

    
    qualified = await client.qualifyContractsAsync(contract)
    if not qualified:
        raise HTTPException(status_code=404, detail="Contract not found")
        
    contract = qualified[0]
    
    # Use reqTickersAsync. Note: ib_async expects contracts as positional arguments (*args)
    logger.info(f"Requesting ticker for contract: {contract}")
    try:
        tickers = await client.reqTickersAsync(contract)
        if not tickers:
            raise HTTPException(status_code=504, detail="No market data received from IBKR")
        t = tickers[0]
    except Exception as e:
        logger.error(f"Error in reqTickersAsync: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=f"IBKR Error: {str(e)}")
    
    # Robust price extraction
    # 1. Try marketPrice() (Last or Close)
    # 2. Try last
    # 3. Try bid/ask midpoint
    # 4. Try close
    
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

    logger.info(f"Snapshot for {symbol}: last={v_last}, bid={v_bid}, ask={v_ask}, close={v_close} -> price={price}")

    return MarketSnapshot(
        symbol=contract.localSymbol,
        price=price,
        bid=v_bid,
        ask=v_ask,
        timestamp=t.time or datetime.now()
    )

@app.get("/options/chain/{symbol}", response_model=List[OptionChainItem], dependencies=[Depends(verify_key)])
async def get_option_chain(symbol: str):
    """
    Get available option expirations and strikes for a given underlying symbol.
    Returns option chain parameters from IBKR.
    """
    client = await get_ib()
    
    # Parse symbol for international stocks (e.g., BATS.L -> LSE/GBP)
    ticker, exchange, currency = parse_symbol(symbol)
    
    # First, qualify the underlying contract
    contract = Contract(symbol=ticker, secType="STK", exchange=exchange, currency=currency)
    qualified = await client.qualifyContractsAsync(contract)
    if not qualified:
        raise HTTPException(status_code=404, detail=f"Underlying {symbol} not found")

    
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
        raise HTTPException(status_code=500, detail=f"Error fetching option chain: {e}")
    
    if not chains:
        raise HTTPException(status_code=404, detail=f"No option chain found for {symbol}")
    
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