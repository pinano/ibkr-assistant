import asyncio
import logging
from fastapi import APIRouter, Depends
from typing import List
from ib_async import ExecutionFilter

from src.api.auth import verify_key
from src.api.connection import get_ib
from src.models import OrderItem, TradeItem

logger = logging.getLogger("ibkr-api")

router = APIRouter()


@router.get("/account/orders",
             response_model=List[OrderItem], dependencies=[Depends(verify_key)])
async def get_orders():
    client = await get_ib()
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


@router.get("/account/trades",
             response_model=List[TradeItem], dependencies=[Depends(verify_key)])
async def get_trades():
    client = await get_ib()
    logger.info("Fetching executions from IBKR...")

    exec_filter = ExecutionFilter()
    fills = await client.reqExecutionsAsync(exec_filter)

    logger.info(f"reqExecutionsAsync returned {len(fills)} fills")

    if not fills:
        fills = client.fills()
        if fills:
            logger.info(f"Falling back to client.fills(): {len(fills)} found")

    items = []
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

    items.sort(key=lambda x: x.time, reverse=True)
    logger.info(f"Returning {len(items)} unique trades")
    return items
