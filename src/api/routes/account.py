import math
import asyncio
import logging
from fastapi import APIRouter, Depends
from typing import List

from src.api.auth import verify_key
from src.api.connection import get_ib
from src.models import AccountSummary, PositionItem, CurrencyItem

logger = logging.getLogger("ibkr-api")

router = APIRouter()


@router.get("/health")
async def health_check():
    """Liveness probe endpoint."""
    return {"status": "ok"}


@router.get("/account/summary", response_model=AccountSummary,
             dependencies=[Depends(verify_key)])
async def get_summary():
    client = await get_ib()
    v = client.accountValues()

    def get_val(tag, currency=None, default="0"):
        matches = [x for x in v if x.tag == tag]
        if not matches:
            return default

        if currency:
            for m in matches:
                if m.currency == currency:
                    return m.value

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
    account_id = client.managedAccounts(
    )[0] if client.managedAccounts() else ""
    daily_pnl = 0.0
    daily_realized = 0.0

    if account_id:
        try:
            client.reqPnL(account_id)
            await asyncio.sleep(0.5)
        except AssertionError:
            pass

        try:
            pnl_result = client.pnl()
            pnl_data = None

            if isinstance(pnl_result, list):
                for p in pnl_result:
                    if hasattr(p, 'account') and p.account == account_id:
                        pnl_data = p
                        break
                if pnl_data is None and pnl_result:
                    pnl_data = pnl_result[0]
            elif isinstance(pnl_result, dict):
                pnl_data = pnl_result.get(account_id) or (
                    list(pnl_result.values())[0] if pnl_result else None)
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


@router.get("/account/positions",
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
            
            underlying = p.contract.symbol
            if p.contract.currency != 'USD':
                from src.parsing import EXCHANGE_PREFIXES
                prefix = None
                for pref, (exch, curr) in EXCHANGE_PREFIXES.items():
                    if exch == p.contract.exchange and curr == p.contract.currency:
                        prefix = pref
                        break
                
                # Fallbacks for common exchanges if not exactly matching MONEP/MEFF/etc.
                if not prefix:
                    if p.contract.currency == 'EUR':
                        if p.contract.exchange in ('MONEP', 'SBF'):
                            prefix = 'EPA'
                        elif p.contract.exchange in ('MEFF', 'BM'):
                            prefix = 'MC'
                        elif p.contract.exchange in ('DTB', 'EUREX'):
                            prefix = 'ETR'
                        else:
                            prefix = 'EPA'  # general fallback for EUR options
                    elif p.contract.currency == 'GBP':
                        prefix = 'LON'
                    elif p.contract.currency == 'CHF':
                        prefix = 'SWX'
                
                if prefix:
                    underlying = f"{prefix}:{underlying}"
            
            item.underlying = underlying
        items.append(item)
    return items


@router.get("/account/currencies",
             response_model=List[CurrencyItem], dependencies=[Depends(verify_key)])
async def get_currencies():
    client = await get_ib()
    return [
        CurrencyItem(currency=v.currency, amount=float(v.value))
        for v in client.accountValues()
        if v.tag == 'CashBalance' and v.currency != 'BASE'
    ]
