import asyncio
import logging
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
import httpx
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.config import settings
from src.bot.client import API_HEADERS, http_client_ctx
from src.bot.rich import (
    cell,
    block_heading,
    block_paragraph,
    block_thinking,
    block_table,
    block_details,
    send_rich_message,
    edit_message_to_rich,
)

logger = logging.getLogger("ibkr-bot")

router = Router()


@router.message(Command("quote", ignore_case=True))
async def cmd_quote(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) < 2:
        await m.answer("ℹ️ Usage: `/quote <SYMBOL>` (e.g. `/quote SPY`)", parse_mode="Markdown")
        return

    symbol = args[1].upper()
    msg = await m.answer(f"🔍 Getting quote for {symbol}...")

    async with http_client_ctx() as client:
        try:
            # 1. Fetch market snapshot
            r = await client.get(f"{settings.WEB_SERVICE_URL}/market/snapshot/{symbol}", headers=API_HEADERS)
            r.raise_for_status()
            data = r.json()

            # 2. Fetch contract details in background to get the name and exchange
            contract_info = ""
            try:
                base_symbol = symbol.split(':')[-1] if ':' in symbol else symbol
                r_c = await client.get(f"{settings.WEB_SERVICE_URL}/contract/search?symbol={base_symbol}", headers=API_HEADERS)
                if r_c.status_code == 200:
                    details = r_c.json()
                    if details:
                        match = None
                        for d in details:
                            if d.get('symbol', '').upper() == base_symbol.upper():
                                match = d
                                break
                        if not match:
                            match = details[0]

                        long_name = match.get('longName') or 'No Name'
                        exchange = match.get('exchange') or 'Unknown'
                        con_id = match.get('conId')
                        contract_info = f"🏢 *{long_name}* ({exchange} | ID: `{con_id}`)\n"
            except Exception as e:
                logger.debug(f"Error fetching contract info in /quote: {e}")

            out = f"📈 *Quote: {data['symbol']}*\n"
            if contract_info:
                out += contract_info
            out += "\n"
            out += f"💰 Price: `{data['price']:.2f}`\n"
            if data.get('bid') is not None and data.get('ask') is not None:
                out += f"↔️ Bid/Ask: `{data['bid']:.2f} / {data['ask']:.2f}`\n"

            # Format timestamp
            ts_str = data['timestamp']
            if 'T' in ts_str:
                try:
                    dt_utc = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    tz = ZoneInfo(settings.TZ)
                    dt_local = dt_utc.astimezone(tz)
                    ts_formatted = dt_local.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    date_part, time_part = ts_str.split('T')
                    time_part = time_part.split('.')[0].replace('Z', '')
                    ts_formatted = f"{date_part} {time_part}"
            else:
                ts_formatted = ts_str

            out += f"⏱ `{ts_formatted}`"
            await msg.edit_text(out, parse_mode="Markdown")

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /quote: {err_detail}")
            await msg.edit_text(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /quote: {e}", exc_info=True)
            await msg.edit_text("❌ Internal error. Check logs.")


@router.message(Command("contract", "contracts", ignore_case=True))
async def cmd_contract(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) < 2:
        await m.answer("ℹ️ Usage: `/contract <SYMBOL>`", parse_mode="Markdown")
        return

    symbol = args[1].upper()
    status_msg = None
    try:
        status_msg = await m.answer(f"🔍 Searching contract for {symbol}... ⏳")
    except Exception as e:
        logger.warning(f"Could not send thinking status message: {e}")

    async with http_client_ctx() as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/contract/search?symbol={symbol}", headers=API_HEADERS)
            r.raise_for_status()
            details = r.json()

            if not details:
                err_msg = f"❌ No contract found for {symbol}."
                if status_msg:
                    await status_msg.edit_text(err_msg)
                else:
                    await m.answer(err_msg)
                return

            out = f"📄 *Contract Details ({len(details)})*:\n\n"
            for d in details[:3]:  # Limit to 3 detailed views
                out += f"🔹 *{d['symbol']}* ({d['secType']})\n"
                out += f"   • Name: {d.get('longName') or 'No Name'}\n"
                out += f"   • ID: `{d['conId']}` | Exch: {d['exchange']}\n"
                if d.get('isin'):
                    out += f"   • ISIN: `{d['isin']}`\n"
                out += "\n"

            if len(details) > 3:
                out += f"... and {len(details) - 3} more contracts found."

            if status_msg:
                await status_msg.edit_text(out, parse_mode="Markdown")
            else:
                await m.answer(out, parse_mode="Markdown")

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /contract: {err_detail}")
            err_msg = f"❌ API Error: {err_detail}"
            if status_msg:
                await status_msg.edit_text(err_msg)
            else:
                await m.answer(err_msg)
        except Exception as e:
            logger.error(f"Error in /contract: {e}", exc_info=True)
            err_msg = "❌ Internal error. Check logs."
            if status_msg:
                await status_msg.edit_text(err_msg)
            else:
                await m.answer(err_msg)


@router.message(Command("chain", ignore_case=True))
async def cmd_chain(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) < 2:
        await m.answer("ℹ️ Usage: `/chain <SYMBOL>` (e.g. `/chain AAPL`)", parse_mode="Markdown")
        return

    symbol = args[1].upper()
    status_msg = None
    msg_id = None
    try:
        status_msg = await send_rich_message(
            m.chat.id,
            [
                block_heading("Option Chain"),
                block_paragraph(f"Fetching option chain for {symbol}..."),
                block_thinking()
            ]
        )
        if status_msg:
            msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else getattr(status_msg, "message_id", None)
    except Exception as e:
        logger.warning(f"Could not send thinking status message: {e}")

    async with http_client_ctx() as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/options/chain/{symbol}", headers=API_HEADERS)
            r.raise_for_status()
            chains = r.json()

            if not chains:
                err_msg = f"❌ No option chain found for {symbol}."
                if msg_id:
                    await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
                else:
                    await m.answer(err_msg)
                return

            chain = chains[0]
            expirations = chain.get('expirations', [])
            strikes = chain.get('strikes', [])

            # Group expirations by month for compact display
            exp_by_month = {}
            for exp in expirations:
                month_key = exp[:6]  # YYYYMM
                if month_key not in exp_by_month:
                    exp_by_month[month_key] = []
                exp_by_month[month_key].append(exp[6:])  # Just the day

            # Build Expirations Table
            exp_rows = [
                [cell("Month", is_header=True), cell("Days", is_header=True)]
            ]
            sorted_months = sorted(exp_by_month.keys())
            for month_key in sorted_months[:12]:
                year = month_key[:4]
                month = month_key[4:6]
                days = ", ".join(exp_by_month[month_key])
                exp_rows.append([
                    cell(f"{year}-{month}"),
                    cell(days)
                ])
            if len(sorted_months) > 12:
                exp_rows.append([
                    cell("..."),
                    cell(f"and {len(sorted_months) - 12} more months")
                ])

            exp_table = block_table(exp_rows, is_bordered=True, is_striped=True)
            exp_details = block_details(f"📅 Expirations ({len(expirations)})", [exp_table], is_open=True)

            # Build Strikes Table
            strike_details = None
            if strikes:
                min_strike = min(strikes)
                max_strike = max(strikes)
                mid_idx = len(strikes) // 2
                sample_strikes = strikes[max(0, mid_idx - 3):mid_idx + 4]
                sample_str = ", ".join(f"{s:.2f}".rstrip('0').rstrip('.') for s in sample_strikes)

                strike_rows = [
                    [cell("Property", is_header=True), cell("Value", is_header=True)],
                    [cell("Range"), cell(f"{f'{min_strike:.2f}'.rstrip('0').rstrip('.')} - {f'{max_strike:.2f}'.rstrip('0').rstrip('.')}")],
                    [cell("Total Strikes"), cell(str(len(strikes)))],
                    [cell("Sample Strikes"), cell(sample_str)]
                ]
                strike_table = block_table(strike_rows, is_bordered=True)
                strike_details = block_details(f"🎯 Strike Prices ({len(strikes)})", [strike_table], is_open=False)

            blocks = [
                block_heading(f"📊 Option Chain: {symbol}"),
                block_paragraph(f"Exchange: {chain['exchange']} | Mult: {chain['multiplier']}"),
                exp_details
            ]
            if strike_details:
                blocks.append(strike_details)

            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, blocks)
            else:
                await send_rich_message(m.chat.id, blocks)

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /chain: {err_detail}")
            err_msg = f"❌ API Error: {err_detail}"
            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
            else:
                await m.answer(err_msg)
        except Exception as e:
            logger.error(f"Error in /chain: {e}", exc_info=True)
            err_msg = "❌ Internal error. Check logs."
            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
            else:
                await m.answer(err_msg)


@router.message(Command("options", ignore_case=True))
async def cmd_options(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with http_client_ctx() as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/positions", headers=API_HEADERS)
            r.raise_for_status()
            positions = r.json()

            # Filter for options
            options = [p for p in positions if p.get('secType') == 'OPT']

            if not options:
                await m.answer("📭 No open option positions.")
                return

            # Sort by expiry (ascending), then underlying symbol
            options.sort(
                key=lambda x: (
                    x.get('expiry') or "",
                    x.get('underlying') or ""))

            builder = InlineKeyboardBuilder()

            last_expiry = None
            for opt in options:
                curr_expiry = opt.get('expiry')
                if curr_expiry and len(curr_expiry) == 8 and curr_expiry.isdigit():
                    formatted_expiry = f"{curr_expiry[0:4]}-{curr_expiry[4:6]}-{curr_expiry[6:8]}"
                else:
                    formatted_expiry = curr_expiry or "Unknown"

                # Add a header button if expiry changes
                if formatted_expiry != last_expiry:
                    builder.row(types.InlineKeyboardButton(
                        text=f"📅 {formatted_expiry}",
                        callback_data="noop"
                    ))
                    last_expiry = formatted_expiry

                # Format Label: ASTS P 55 2026-01-09
                underlying = opt.get('underlying', "??")
                right = opt.get('right', "?")
                strike_val = opt.get('strike', 0)
                strike = f"{strike_val:.0f}" if float(strike_val).is_integer() else f"{strike_val}"

                label = f"{underlying} {right} {strike} {formatted_expiry}"

                builder.row(types.InlineKeyboardButton(
                    text=f"{label} ({opt['qty']})",
                    callback_data=f"opt:{opt.get('underlying','')}|{opt.get('expiry','')}|{opt.get('strike',0)}|{opt.get('right','')}|{opt.get('conId',0)}"
                ))

            await m.answer("📑 *Open Option Positions*",
                           reply_markup=builder.as_markup(),
                           parse_mode="Markdown")

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /options: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /options: {e}", exc_info=True)
            await m.answer("❌ Internal error. Check logs.")


@router.callback_query(F.data == "noop")
async def process_noop(callback: types.CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("opt:"))
async def process_opt_details(callback: types.CallbackQuery):
    try:
        parts = callback.data[4:].split("|")
        underlying, expiry, strike_str, right = parts[0], parts[1], parts[2], parts[3]
        strike = float(strike_str)
        con_id = int(parts[4]) if len(parts) > 4 else 0
    except (ValueError, IndexError):
        await callback.message.answer("❌ Invalid option data in callback.")
        await callback.answer()
        return

    async with http_client_ctx() as client:
        try:
            r = await client.get(
                f"{settings.WEB_SERVICE_URL}/option/greeks",
                params={
                    "underlying": underlying,
                    "expiry": expiry,
                    "strike": strike,
                    "right": right,
                    "conId": con_id},
                headers=API_HEADERS
            )
            r.raise_for_status()
            d = r.json()

            # Format display label
            strike_fmt = f"{strike:.0f}" if strike == int(strike) else f"{strike}"
            exp_fmt = f"{expiry[0:4]}-{expiry[4:6]}-{expiry[6:8]}" if len(expiry) == 8 else expiry
            display = f"{underlying} {right} {strike_fmt} {exp_fmt}"

            greeks_table = block_table([
                [cell("Metric", is_header=True), cell("Value", is_header=True, align="right")],
                [cell("Delta Δ"), cell(f"{d['delta']:.4f}", align="right")],
                [cell("Gamma γ"), cell(f"{d['gamma']:.4f}", align="right")],
                [cell("Vega ν"), cell(f"{d['vega']:.4f}", align="right")],
                [cell("Theta θ"), cell(f"{d['theta']:.4f}", align="right")]
            ], is_bordered=True, is_striped=True)

            mkt_table = block_table([
                [cell("Metric", is_header=True), cell("Value", is_header=True, align="right")],
                [cell("Implied Vol (IV)"), cell(f"{d['implied_vol'] * 100:.2f}%", align="right")],
                [cell("Underlying Price"), cell(f"{d['underlying_price']:.2f}", align="right")],
                [cell("Volume"), cell(str(d['volume']), align="right")],
                [cell("Open Interest"), cell(str(d['open_interest']), align="right")]
            ], is_bordered=True, is_striped=True)

            last_trade_table = block_table([
                [cell("Metric", is_header=True), cell("Value", is_header=True, align="right")],
                [cell("Last Price"), cell(f"{d['last_price']:.2f}", align="right")],
                [cell("Date"), cell(str(d['last_date'] or 'N/A'), align="right")]
            ], is_bordered=True, is_striped=True)

            blocks = [
                block_heading(f"📊 Option Details: {display}"),
                block_details("🧮 Greeks", [greeks_table], is_open=True),
                block_details("📈 Market Data", [mkt_table]),
                block_details("💰 Last Trade", [last_trade_table])
            ]

            await send_rich_message(callback.message.chat.id, blocks)
            await callback.answer()

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /options callback: {err_detail}")
            await callback.message.answer(f"❌ API Error: {err_detail}")
            await callback.answer()
        except Exception as e:
            logger.error(f"Error in /options callback: {e}", exc_info=True)
            await callback.message.answer("❌ Internal error. Check logs.")
            await callback.answer()


@router.message(Command("delta", ignore_case=True))
async def cmd_delta(m: types.Message):
    """On-demand check: show all short option positions with high delta."""
    if m.from_user.id not in settings.allowed_ids_list:
        return

    status_msg = None
    try:
        status_msg = await m.answer("Checking deltas for short positions... ⏳")
    except Exception as e:
        logger.warning(f"Could not send status message: {e}")

    try:
        async with http_client_ctx() as client:
            # Fetch positions
            r_pos = await client.get(
                f"{settings.WEB_SERVICE_URL}/account/positions",
                headers=API_HEADERS
            )
            if r_pos.status_code != 200:
                err_msg = f"❌ Failed to fetch positions (HTTP {r_pos.status_code})"
                if status_msg:
                    await status_msg.edit_text(err_msg)
                else:
                    await m.answer(err_msg)
                return

            positions = r_pos.json()
            short_options = [
                p for p in positions
                if p.get('secType') == 'OPT' and p.get('qty', 0) < 0
                and not (
                    p.get('underlying', '').upper() in settings.delta_alert_exclude_list or
                    p.get('underlying', '').split(':')[-1].upper() in settings.delta_alert_exclude_list
                )
            ]

            if not short_options:
                empty_msg = "✅ No short option positions found."
                if status_msg:
                    await status_msg.edit_text(empty_msg)
                else:
                    await m.answer(empty_msg)
                return

            # Fetch Greeks for all short options in parallel (max 3 concurrent)
            semaphore = asyncio.Semaphore(3)

            async def fetch_one_delta(opt):
                con_id = opt.get('conId', 0)
                if not con_id:
                    return None

                underlying = opt.get('underlying', '??')
                right = opt.get('right', '?')
                strike = opt.get('strike', 0)
                expiry = opt.get('expiry', '')
                qty = opt.get('qty', 0)
                strike_fmt = f"{strike:.0f}" if strike == int(strike) else f"{strike}"
                exp_fmt = expiry.replace("-", "")

                delta = None
                age_str = ""
                last_price = None
                underlying_price = None

                async with semaphore:
                    try:
                        params = {
                            'underlying': underlying,
                            'expiry': expiry,
                            'strike': strike,
                            'right': right,
                            'conId': con_id
                        }
                        r = await client.get(
                            f"{settings.WEB_SERVICE_URL}/option/greeks",
                            params=params, headers=API_HEADERS
                        )
                        if r.status_code == 200:
                            data = r.json()
                            raw_delta = data.get('delta', 0.0)
                            if abs(raw_delta) >= 0.0001:
                                delta = raw_delta

                            raw_last = data.get('last_price', 0.0)
                            raw_und = data.get('underlying_price', 0.0)
                            if raw_last and raw_last > 0:
                                last_price = raw_last
                            if raw_und and raw_und > 0:
                                underlying_price = raw_und

                            last_date_str = data.get('last_date')
                            if last_date_str:
                                try:
                                    last_dt = datetime.strptime(last_date_str, "%Y-%m-%d %H:%M:%S")
                                    age_min = int((datetime.now() - last_dt).total_seconds() / 60)
                                    age_str = f"{age_min}m" if age_min >= 2 else ""
                                except (ValueError, TypeError):
                                    pass
                    except Exception as e:
                        logger.debug(f"Error fetching greeks for conId={con_id}: {e}")

                # Compute intrinsic value and time value
                intrinsic = None
                time_value = None
                if last_price is not None and underlying_price is not None and strike:
                    right_upper = right.upper()
                    if right_upper == 'P':
                        intrinsic = max(0.0, strike - underlying_price)
                    else:  # Call
                        intrinsic = max(0.0, underlying_price - strike)
                    time_value = last_price - intrinsic

                display_und = (underlying.split(':')[-1] if ':' in underlying else underlying)[:5]
                return {
                    'underlying': display_und,
                    'right': right,
                    'strike': strike,
                    'right_strike': f"{right}{strike_fmt}",
                    'expiry': exp_fmt,
                    'delta': delta,
                    'qty': abs(qty),
                    'high': delta is not None and abs(delta) > settings.DELTA_ALERT_THRESHOLD,
                    'age': age_str,
                    'last_price': last_price,
                    'underlying_price': underlying_price,
                    'intrinsic': intrinsic,
                    'time_value': time_value,
                }

            fetch_tasks = [fetch_one_delta(opt) for opt in short_options]
            raw_results = await asyncio.gather(*fetch_tasks)
            results = [r for r in raw_results if r is not None]

            if not results:
                empty_msg = "✅ No short option positions found."
                if status_msg:
                    await status_msg.edit_text(empty_msg)
                else:
                    await m.answer(empty_msg)
                return

            # Sort: contracts with delta first (by abs desc), then None deltas at the bottom
            results.sort(key=lambda x: (x['delta'] is None, -abs(x['delta']) if x['delta'] is not None else 0))

            # Build digest message
            high_count = sum(1 for r in results if r['high'])
            no_data_count = sum(1 for r in results if r['delta'] is None)
            header = f"📊 <b>Delta Report — {len(results)} Short Position(s)</b>\n"
            if high_count:
                header += f"⚠️ <b>{high_count} above threshold ({settings.DELTA_ALERT_THRESHOLD})</b>\n"
            if no_data_count:
                header += f"⚪ <b>{no_data_count} without delta data</b>\n"

            # Calculate TV strings and format expiry
            for r in results:
                r['tv_str'] = ""
                r['low_tv'] = False
                if r['intrinsic'] is not None and r['time_value'] is not None:
                    tv_val = f"{r['time_value']:.2f}"
                    r['tv_str'] = f"tV{tv_val}"
                    if r['intrinsic'] > 0 and r['time_value'] <= (0.01 * r['strike']):
                        r['low_tv'] = True

                display_expiry = r['expiry']
                if len(display_expiry) == 8 and display_expiry.isdigit():
                    try:
                        dt = datetime.strptime(display_expiry, "%Y%m%d")
                        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                        month_str = months[dt.month - 1]
                        display_expiry = f"{dt.day:02d}{month_str}{dt.year % 100:02d}"
                    except Exception:
                        pass
                r['display_expiry'] = display_expiry

            max_qty = max(len(f"{r['qty']:.0f}") for r in results)
            max_und = max(len(r['underlying']) for r in results)
            max_rs = max(len(r['right_strike']) for r in results)
            max_exp = max(len(r['display_expiry']) for r in results)
            has_any_tv = any(r['tv_str'] != "" for r in results)
            max_tv = max(len(r['tv_str']) for r in results if r['tv_str'] != "") if has_any_tv else 0
            option_lines = []
            for r in results:
                if r['delta'] is None:
                    delta_str = "  —  "
                else:
                    delta_str = f"{abs(r['delta']):.2f}"

                if r['low_tv']:
                    marker = "⚠️"
                elif r['delta'] is None:
                    marker = "⚪"
                else:
                    marker = "🔴" if r['high'] else "🟢"
                qty_str = f"{r['qty']:.0f}".rjust(max_qty)
                und_padded = r['underlying'].ljust(max_und)
                rs_padded = r['right_strike'].ljust(max_rs)
                exp_padded = r['display_expiry'].ljust(max_exp)
                age = r['age']

                # Append TV column if available
                tv_part = ""
                if r['tv_str']:
                    tv_part = f" {r['tv_str'].ljust(max_tv)}"
                elif has_any_tv:
                    tv_part = " " * (max_tv + 1)

                age_part = f" {age}" if age else ""
                line = f"{marker} <code>{qty_str} {und_padded} {rs_padded} {exp_padded} Δ{delta_str}{tv_part}{age_part}</code>"
                option_lines.append(line.strip())

            options_text = "\n".join(option_lines)
            message = f"{header}\n{options_text}\n\n🔴 abs(Δ) &gt; {settings.DELTA_ALERT_THRESHOLD}  🟢 abs(Δ) ≤ {settings.DELTA_ALERT_THRESHOLD}  ⚪ no data\n⚠️ low time value (≤ 1% strike)"

            if status_msg:
                await status_msg.edit_text(message, parse_mode="HTML")
            else:
                await m.answer(message, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error in /delta: {e}", exc_info=True)
        err_msg = "❌ Internal error. Check logs."
        if status_msg:
            try:
                await status_msg.edit_text(err_msg)
            except Exception:
                await m.answer(err_msg)
        else:
            await m.answer(err_msg)
