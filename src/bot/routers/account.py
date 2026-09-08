import logging
import httpx
from aiogram import Router, types
from aiogram.filters import Command

from src.config import settings
from src.bot.client import API_HEADERS, http_client_ctx
from src.bot.rich import (
    cell,
    text_bold,
    block_heading,
    block_table,
    block_details,
    send_rich_message,
)

logger = logging.getLogger("ibkr-bot")

router = Router()


@router.message(Command("nav", ignore_case=True))
async def cmd_nav(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with http_client_ctx() as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/summary", headers=API_HEADERS)
            r.raise_for_status()
            d = r.json()

            rows = [
                # Section 1: Net Liquidation
                [cell(text_bold("Account Summary"), is_header=True, colspan=2)],
                [cell("💰 Net Liquidation"), cell(f"{d['NetLiquidation']:+,.2f}", align="right")],
                [cell("📈 Stock Value"), cell(f"{d['StockMarketValue']:+,.2f}", align="right")],
                [cell("📊 Unrealized PnL"), cell(f"{d['UnrealizedPnL']:+,.2f}", align="right")],

                # Section 2: Daily PnL
                [cell(text_bold("Daily Variation"), is_header=True, colspan=2)],
                [cell("📆 Daily PnL"), cell(f"{d['DailyPnL']:+,.2f}", align="right")],
                [cell("📅 Daily Realized PnL"), cell(f"{d['DailyRealizedPnL']:+,.2f}", align="right")],

                # Section 3: Cash & Currencies
                [cell(text_bold("Cash Balances"), is_header=True, colspan=2)],
                [cell("💵 Base Cash"), cell(f"{d['TotalCashValue']:+,.2f}", align="right")],
                [cell("💶 EUR Cash"), cell(f"{d['EUR']:+,.2f}", align="right")],
                [cell("💵 USD Cash"), cell(f"{d['USD']:+,.2f}", align="right")],
                [cell("💷 GBP Cash"), cell(f"{d['GBP']:+,.2f}", align="right")],

                # Section 4: Risk & Margin
                [cell(text_bold("Risk & Margin"), is_header=True, colspan=2)],
                [cell("🛡️ Cushion"), cell(f"{d['Cushion']:.6f}", align="right")],
                [cell("🚀 Buying Power"), cell(f"{d['BuyingPower']:+,.2f}", align="right")],
                [cell("💧 Excess Liquidity"), cell(f"{d['ExcessLiquidity']:+,.2f}", align="right")],
                [cell("🧱 Maint. Margin"), cell(f"{d['FullMaintMargin']:+,.2f}", align="right")]
            ]

            blocks = [
                block_heading("Net Asset Value Report"),
                block_table(rows, is_bordered=True, is_striped=True)
            ]

            await send_rich_message(m.chat.id, blocks)
        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /nav: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /nav: {e}", exc_info=True)
            await m.answer("❌ Internal error. Check logs.")


@router.message(Command("pos", "positions", ignore_case=True))
async def cmd_pos(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with http_client_ctx() as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/positions", headers=API_HEADERS)
            r.raise_for_status()
            positions = r.json()

            if not positions:
                await m.answer("📭 No open positions.")
                return

            # Separate stocks and options, sort alphabetically
            stocks = sorted([p for p in positions if p.get('secType') != 'OPT'], key=lambda x: x['symbol'])
            options = sorted([p for p in positions if p.get('secType') == 'OPT'], key=lambda x: x['symbol'])

            blocks = []

            # Stocks table
            if stocks:
                blocks.append(block_heading("📈 Stocks"))
                stock_rows = [
                    [cell("Symbol", is_header=True), cell("Pos.", is_header=True, align="right"), cell("Avg Cost", is_header=True, align="right")]
                ]
                for p in stocks:
                    stock_rows.append([
                        cell(p['symbol']),
                        cell(f"{p['qty']:.0f}", align="right"),
                        cell(f"{p['cost']:.4f}", align="right")
                    ])
                blocks.append(block_table(stock_rows, is_bordered=True, is_striped=True))

            # Options table
            if options:
                option_rows = [
                    [cell("Symbol", is_header=True), cell("Pos.", is_header=True, align="right"), cell("Avg Cost", is_header=True, align="right")]
                ]
                for p in options:
                    sym = str(p['symbol']).replace(' ', '')
                    option_rows.append([
                        cell(sym),
                        cell(f"{p['qty']:.0f}", align="right"),
                        cell(f"{p['cost']:.4f}", align="right")
                    ])
                opt_table = block_table(option_rows, is_bordered=True, is_striped=True)
                blocks.append(block_details("📋 Open Options Positions", [opt_table]))

            if not blocks:
                await m.answer("📭 No open positions.")
                return

            await send_rich_message(m.chat.id, blocks)

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /pos: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /pos: {e}", exc_info=True)
            await m.answer("❌ Internal error. Check logs.")
