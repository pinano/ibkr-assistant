import logging
import httpx
from aiogram import Router, types
from aiogram.filters import Command

from src.config import settings
from src.bot.client import API_HEADERS, http_client_ctx
from src.bot.rich import (
    cell,
    block_heading,
    block_paragraph,
    block_table,
    send_rich_message,
)

logger = logging.getLogger("ibkr-bot")

router = Router()


@router.message(Command("orders", ignore_case=True))
async def cmd_orders(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with http_client_ctx() as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/orders", headers=API_HEADERS)
            r.raise_for_status()
            orders = r.json()

            if not orders:
                await m.answer("📭 No active orders.")
                return

            rows = [
                [
                    cell("Action", is_header=True),
                    cell("Qty", is_header=True, align="right"),
                    cell("Symbol", is_header=True),
                    cell("Price", is_header=True, align="right"),
                    cell("Status", is_header=True)
                ]
            ]
            for o in orders[:15]:  # Limit to 15
                price_str = f"{o['lmtPrice']:.2f}" if o.get('lmtPrice') else "MKT"
                rows.append([
                    cell(o['action']),
                    cell(f"{o['totalQuantity']:.0f}", align="right"),
                    cell(o['symbol']),
                    cell(price_str, align="right"),
                    cell(o['status'])
                ])

            blocks = [
                block_heading("📋 Active Orders"),
                block_table(rows, is_bordered=True, is_striped=True)
            ]
            if len(orders) > 15:
                blocks.append(block_paragraph(f"... and {len(orders) - 15} more active orders."))

            await send_rich_message(m.chat.id, blocks)

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /orders: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /orders: {e}", exc_info=True)
            await m.answer("❌ Internal error. Check logs.")


@router.message(Command("trades", ignore_case=True))
async def cmd_trades(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with http_client_ctx() as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/trades", headers=API_HEADERS)
            r.raise_for_status()
            trades = r.json()

            if not trades:
                await m.answer("📭 No trades executed today.")
                return

            rows = [
                [
                    cell("Time", is_header=True),
                    cell("Side", is_header=True),
                    cell("Qty", is_header=True, align="right"),
                    cell("Symbol", is_header=True),
                    cell("Price", is_header=True, align="right"),
                    cell("Comm", is_header=True, align="right")
                ]
            ]
            for t in trades[:15]:
                time_str = t['time'].split('T')[1].split('.')[0] if 'T' in t['time'] else t['time']

                price_val = float(t['price']) if t.get('price') is not None else 0.0
                shares_val = float(t['shares']) if t.get('shares') is not None else 0.0
                shares_str = f"{shares_val:.0f}" if shares_val.is_integer() else f"{shares_val:.2f}"

                comm_val = t.get('commission')
                comm_str = f"{float(comm_val):.2f}" if comm_val is not None else "-"

                rows.append([
                    cell(time_str),
                    cell(t['side']),
                    cell(shares_str, align="right"),
                    cell(t['symbol']),
                    cell(f"{price_val:.2f}", align="right"),
                    cell(comm_str, align="right")
                ])

            blocks = [
                block_heading("🤝 Today's Trades"),
                block_table(rows, is_bordered=True, is_striped=True)
            ]
            if len(trades) > 15:
                blocks.append(block_paragraph(f"... and {len(trades) - 15} more trades today."))

            await send_rich_message(m.chat.id, blocks)

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /trades: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /trades: {e}", exc_info=True)
            await m.answer("❌ Internal error. Check logs.")
