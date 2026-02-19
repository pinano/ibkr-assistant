import math
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ib_async import Contract

from src.api.auth import verify_key
from src.api.connection import get_ib
from src.api.database import get_db
from src.api.helpers import parse_symbol
from src.models import MarketSnapshot, MarketCache

logger = logging.getLogger("ibkr-api")

router = APIRouter()


@router.get("/market/snapshot/{symbol}", response_model=MarketSnapshot,
             dependencies=[Depends(verify_key)])
async def get_market_snapshot(symbol: str, db: Session = Depends(get_db)):
    """
    Fetch market snapshot for a symbol (Stock or FX).
    Prioritizes DB cache (valid for 60 mins) before hitting IBKR live.
    """
    symbol = symbol.strip().upper()

    try:
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
            contract = Contract(symbol=symbol[:3],
                                secType="CASH",
                                exchange="IDEALPRO",
                                currency=symbol[3:])
            logger.info(f"Detected FX pair {symbol}, using CASH contract: {contract.symbol}.{contract.currency}")
        else:
            ticker, exchange, currency = parse_symbol(symbol)
            contract = Contract(
                symbol=ticker,
                secType="STK",
                exchange=exchange,
                currency=currency)

        qualified = await client.qualifyContractsAsync(contract)
        if not qualified:
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
            raise HTTPException(status_code=503, detail="Market data temporarily unavailable")

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

    except Exception as e:
        logger.error(f"Unexpected error in get_market_snapshot: {e}", exc_info=True)
        if isinstance(e, HTTPException):
             raise e
        raise HTTPException(status_code=500, detail="Internal error fetching market snapshot")
