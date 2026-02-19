import logging
from fastapi import APIRouter, Depends
from typing import List
from ib_async import Contract

from src.api.auth import verify_key
from src.api.connection import get_ib
from src.api.helpers import parse_symbol
from src.models import ContractDetailsItem

logger = logging.getLogger("ibkr-api")

router = APIRouter()


@router.get("/contract/search",
             response_model=List[ContractDetailsItem], dependencies=[Depends(verify_key)])
async def search_contract(symbol: str, secType: str = "STK"):
    client = await get_ib()

    ticker, exchange, currency = parse_symbol(symbol)

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
    for d in details[:5]:
        c = d.contract

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
