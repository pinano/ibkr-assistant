import asyncio
import io
import logging
import math
import httpx
from collections import defaultdict
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
import matplotlib
matplotlib.use("Agg")  # headless backend — no display required
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.flex import FlexReporter
from src.config import settings
from src.models import Base, CashBalance
from src.monitor import Monitor

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ibkr-bot")

# DB Setup
engine = create_engine(settings.DB_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

# Ensure tables exist
Base.metadata.create_all(engine)

# Validate required settings
if not settings.TELEGRAM_TOKEN:
    logger.critical("TELEGRAM_TOKEN is not set. Bot cannot start.")
    raise SystemExit("Missing required environment variable: TELEGRAM_TOKEN")
if not settings.DB_URL:
    logger.critical("DB_URL is not set. Bot cannot start.")
    raise SystemExit("Missing required environment variable: DB_URL")

# Bot Setup
bot = Bot(token=settings.TELEGRAM_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
monitor = Monitor(SessionLocal)


# ---------------------------------------------------------------------------
# NAV chart helpers
# ---------------------------------------------------------------------------

MAX_CHART_POINTS = 150  # Target number of points after downsampling


def _query_nav_series(
    session, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """
    Return all (date, nav) records in [start, end], ordered ascending.
    Used by all NAV chart commands to fetch the raw time series.
    """
    rows = (
        session.query(CashBalance)
        .filter(CashBalance.date >= start, CashBalance.date <= end)
        .order_by(CashBalance.date.asc())
        .all()
    )
    return [(r.date, float(r.nav)) for r in rows if r.nav is not None]


def _build_nav_chart(
    series: list[tuple[datetime, float]],
    period_name: str,
) -> bytes:
    """
    Build a NAV evolution chart from a time series and return PNG bytes.

    Downsamples to MAX_CHART_POINTS using uniform time-bucket aggregation
    (last value per bucket). X-axis format and line color are chosen
    automatically based on the time range and performance.

    Must be called via asyncio.to_thread — matplotlib is blocking.
    """
    if len(series) < 2:
        raise ValueError("Not enough data points to build a chart")

    # --- Adaptive downsampling -------------------------------------------
    if len(series) > MAX_CHART_POINTS:
        t0 = series[0][0].timestamp()
        t1 = series[-1][0].timestamp()
        bucket_size = (t1 - t0) / MAX_CHART_POINTS

        buckets: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
        for dt, nav in series:
            idx = int((dt.timestamp() - t0) / bucket_size)
            # Clamp to avoid off-by-one at the very end
            idx = min(idx, MAX_CHART_POINTS - 1)
            buckets[idx].append((dt, nav))

        # Keep the last value in each bucket (most recent snapshot)
        series = [bucket[-1] for _, bucket in sorted(buckets.items())]

    dates = [dt for dt, _ in series]
    navs = [nav for _, nav in series]

    # --- X-axis format auto-detection ------------------------------------
    total_seconds = (dates[-1] - dates[0]).total_seconds()
    total_days = total_seconds / 86_400

    if total_days < 2:
        date_fmt = "%H:%M"
    elif total_days < 90:
        date_fmt = "%d/%m"
    else:
        date_fmt = "%m/%y"

    # --- Color based on performance --------------------------------------
    color = "#4CAF50" if navs[-1] >= navs[0] else "#EF5350"
    fill_color = color

    # --- Plot -----------------------------------------------------------
    fig, ax = plt.subplots(figsize=(8, 3.2))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#1e1e2e")

    ax.plot(dates, navs, linewidth=1.8, color=color, zorder=3)
    ax.fill_between(dates, navs, min(navs), alpha=0.25, color=fill_color, zorder=2)

    # Axes styling
    ax.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))
    fig.autofmt_xdate(rotation=30, ha="right")
    ax.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: (
            f"{x/1_000_000:.1f}M" if abs(x) >= 1_000_000
            else f"{x/1_000:.0f}k" if abs(x) >= 1_000
            else f"{x:.0f}"
        ))
    )
    for spine in ax.spines.values():
        spine.set_edgecolor("#444466")
    ax.tick_params(colors="#aaaacc", labelsize=8)
    ax.yaxis.label.set_color("#aaaacc")
    ax.set_title(
        f"NAV — {period_name}",
        color="#ccccee",
        fontsize=10,
        pad=6,
    )
    ax.grid(axis="y", linestyle="--", linewidth=0.5, color="#333355", alpha=0.7)

    fig.tight_layout(pad=0.8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


async def _send_nav_chart(
    m: types.Message,
    series: list[tuple[datetime, float]],
    period_name: str,
    caption: str,
    parse_mode: str = "Markdown",
) -> bool:
    """
    Generate and send a NAV chart photo with caption.
    Returns True if the photo was sent, False on failure (caller falls back).
    """
    if len(series) < 2:
        return False
    try:
        chart_bytes = await asyncio.to_thread(_build_nav_chart, series, period_name)
        await m.answer_photo(
            BufferedInputFile(chart_bytes, filename="nav.png"),
            caption=caption,
            parse_mode=parse_mode,
        )
        return True
    except Exception as e:
        logger.warning(f"Chart generation failed for {period_name}: {e}")
        return False


# ---------------------------------------------------------------------------


def get_now() -> datetime:
    """Return current time in the configured timezone."""
    try:
        tz = ZoneInfo(settings.TZ)
        return datetime.now(tz)
    except Exception:
        return datetime.now()


def format_nav_date(dt: datetime, now_dt: datetime, include_year: bool = False) -> str:
    """Format date to HH:MM if today, else DD/MM HH:MM. If include_year is True, DD/MM/YY HH:MM."""
    if dt.date() == now_dt.date():
        return dt.strftime("%H:%M")
    if include_year:
        return dt.strftime("%d/%m/%y %H:%M")
    return dt.strftime("%d/%m %H:%M")

async def notify_admins(text: str, parse_mode: str = "Markdown"):
    for chat_id in settings.allowed_ids_list:
        try:
            await bot.send_message(chat_id, text, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Failed to notify admin {chat_id}: {e}")

API_HEADERS = {"X-API-Key": settings.API_KEY}

# Constants
EMOJI_MAP = {
    "EUR": "💶",
    "USD": "💵",
    "GBP": "💷",
    "CHF": "🇨🇭",
    "SEK": "🇸🇪"
}


async def check_and_archive(force_insert: bool = False):
    """
    Monitoring check that fetches current balances and detects cash changes.

    Args:
        force_insert: If True, always insert a record to DB (used for periodic snapshots).
                     If False, only insert when cash balance changes are detected.
    """
    log_suffix = " (forced DB insert)" if force_insert else ""
    logger.info(f"Running monitoring check{log_suffix}...")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Fetch Summary
            r_sum = await client.get(f"{settings.WEB_SERVICE_URL}/account/summary", headers=API_HEADERS)
            if r_sum.status_code != 200:
                logger.warning(f"Failed to fetch summary: {r_sum.status_code}")
                return

            summary = r_sum.json()

            # 2. Fetch Currencies
            r_curr = await client.get(f"{settings.WEB_SERVICE_URL}/account/currencies", headers=API_HEADERS)
            currencies = r_curr.json() if r_curr.status_code == 200 else []

            # 3. DB Operations
            try:
                with SessionLocal() as session:
                    # Map currencies for quick lookup
                    curr_map = {c['currency']: c['amount'] for c in currencies}

                    # Get previous cash balance for change detection
                    last_record = session.query(CashBalance).order_by(
                        CashBalance.date.desc()).first()

                    # Create new record (but don't add to session yet)
                    new_record = CashBalance(
                        nav=summary['NetLiquidation'],
                        stock=summary['StockMarketValue'],
                        pnl=summary['UnrealizedPnL'],
                        base=summary['TotalCashValue'],
                        eur=curr_map.get('EUR', 0.0),
                        usd=curr_map.get('USD', 0.0),
                        gbp=curr_map.get('GBP', 0.0),
                        chf=curr_map.get('CHF', 0.0),
                        sek=curr_map.get('SEK', 0.0),
                        cushion=summary['Cushion'],
                        buyingPower=summary['BuyingPower'],
                        excessLiq=summary['ExcessLiquidity'],
                        maintMargin=summary['FullMaintMargin']
                    )

                    # Check for alerts using the previous record
                    alerts = []
                    cash_changed = False
                    if last_record:
                        for curr in ['eur', 'usd', 'gbp', 'chf', 'sek']:
                            old_val = float(getattr(last_record, curr) or 0.0)
                            new_val = float(getattr(new_record, curr) or 0.0)

                            if new_val != old_val:
                                cash_changed = True
                                diff = new_val - old_val
                                sign = "+" if diff > 0 else "-"
                                abs_diff = abs(diff)

                                # Emoji mapping
                                curr_upper = curr.upper()
                                emoji = EMOJI_MAP.get(curr_upper, "💰")

                                alert = (
                                    f"<code>{curr_upper} {emoji} {diff:+.4f}</code>\n"
                                    f"<code>{old_val:.4f} {sign} {abs_diff:.4f} = {new_val:.4f}</code>"
                                )
                                alerts.append(alert)

                    # Only insert to DB if:
                    # 1. Cash balance has changed, OR
                    # 2. force_insert is True (periodic snapshot)
                    should_insert = cash_changed or force_insert

                    if should_insert:
                        session.add(new_record)
                        session.commit()
                        if cash_changed:
                            logger.info(
                                "DB record inserted due to cash balance change")
                        else:
                            logger.info("DB record inserted (periodic snapshot)")
                    else:
                        logger.debug("No DB insert - no cash changes detected")

                    if alerts:
                        await notify_admins(
                            "💰 <b>Cash balance change:</b>\n" + "\n".join(alerts),
                            parse_mode="HTML"
                        )

            except Exception as e:
                logger.error(f"DB/Logic Error: {e}")

    except Exception as e:
        logger.error(f"Monitoring Job Error: {e}")


@dp.message(Command("nav", ignore_case=True))
async def cmd_nav(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/summary", headers=API_HEADERS)
            r.raise_for_status()
            d = r.json()

            # Format with entire lines in inline code (monospace without blue
            # box)
            msg = f"<code>💰 NAV:      {d['NetLiquidation']:>+12.2f}</code>\n"
            msg += f"<code>📈 Stock:    {d['StockMarketValue']:>+12.2f}</code>\n"
            msg += f"<code>📊 Pnl:      {d['UnrealizedPnL']:>+12.2f}</code>\n"
            msg += "-------------------\n"
            msg += f"<code>📆 Day Pnl:  {d['DailyPnL']:>+12.2f}</code>\n"
            msg += f"<code>📅 Day Rlz:  {d['DailyRealizedPnL']:>+12.2f}</code>\n"
            msg += "-------------------\n"
            msg += f"<code>💵 Base:     {d['TotalCashValue']:>+12.2f}</code>\n"
            msg += f"<code>💶 EUR:      {d['EUR']:>+12.2f}</code>\n"
            msg += f"<code>💵 USD:      {d['USD']:>+12.2f}</code>\n"
            msg += f"<code>💷 GBP:      {d['GBP']:>+12.2f}</code>\n"
            msg += "-------------------\n"
            msg += f"<code>🛡️ Cushion:  {d['Cushion']:>12.6f}</code>\n"
            msg += f"<code>🚀 BuyPwr:   {d['BuyingPower']:>12.2f}</code>\n"
            msg += f"<code>💧 exLiq:    {d['ExcessLiquidity']:>12.2f}</code>\n"
            msg += f"<code>🧱 margin:   {d['FullMaintMargin']:>12.2f}</code>"

            await m.answer(msg, parse_mode="HTML")
        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /nav: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /nav: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.message(Command("pos", ignore_case=True))
async def cmd_pos(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/positions", headers=API_HEADERS)
            r.raise_for_status()
            positions = r.json()

            if not positions:
                await m.answer("📭 No open positions.")
                return

            # Separate stocks and options, sort alphabetically
            stocks = sorted([p for p in positions if p.get(
                'secType') != 'OPT'], key=lambda x: x['symbol'])
            options = sorted([p for p in positions if p.get(
                'secType') == 'OPT'], key=lambda x: x['symbol'])

            # Stocks table (8-char symbol width)
            if stocks:
                header = "Symbol  | Pos.  | Avg\n"
                header += "--------|-------|-------------\n"

                rows = []
                for p in stocks:
                    sym = str(p['symbol']).ljust(8)
                    qty = str(p['qty']).ljust(6)
                    cost = f"{p['cost']:.4f}"
                    rows.append(f"{sym}| {qty}| {cost}")

                msg = "� *Stocks*\n\n```\n" + \
                    header + "\n".join(rows) + "\n```"
                await m.answer(msg, parse_mode="Markdown")

            # Options table (20-char symbol width, spaces removed)
            if options:
                header = "Symbol              | Pos.  | Avg\n"
                header += "--------------------|-------|-------------\n"

                rows = []
                for p in options:
                    # Remove spaces from option symbols for compact display
                    sym = str(p['symbol']).replace(' ', '').ljust(20)
                    qty = str(p['qty']).ljust(6)
                    cost = f"{p['cost']:.4f}"
                    rows.append(f"{sym}| {qty}| {cost}")

                msg = "📋 *Options*\n\n```\n" + \
                    header + "\n".join(rows) + "\n```"
                await m.answer(msg, parse_mode="Markdown")

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /pos: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /pos: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.message(Command("options", ignore_case=True))
async def cmd_options(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
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
                # Format expiry for readability if it's YYYYMMDD
                if curr_expiry and len(
                        curr_expiry) == 8 and curr_expiry.isdigit():
                    formatted_expiry = f"{curr_expiry[0:4]}-{curr_expiry[4:6]}-{curr_expiry[6:8]}"
                else:
                    formatted_expiry = curr_expiry or "Unknown"

                # Add a header button (not clickable or for info) if expiry
                # changes
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
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.callback_query(F.data == "noop")
async def process_noop(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("opt:"))
async def process_opt_details(callback: types.CallbackQuery):
    # Parse structured data: opt:UNDERLYING|EXPIRY|STRIKE|RIGHT|CONID
    try:
        parts = callback.data[4:].split("|")
        underlying, expiry, strike_str, right = parts[0], parts[1], parts[2], parts[3]
        strike = float(strike_str)
        con_id = int(parts[4]) if len(parts) > 4 else 0
    except (ValueError, IndexError):
        await callback.message.answer("❌ Invalid option data in callback.")
        await callback.answer()
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
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

            msg = (
                f"📊 *Option Details: {display}*\n\n"
                f"🧮 *Greeks:*\n"
                f"• Δ Delta: `{d['delta']:.4f}`\n"
                f"• γ Gamma: `{d['gamma']:.4f}`\n"
                f"• ν Vega: `{d['vega']:.4f}`\n"
                f"• θ Theta: `{d['theta']:.4f}`\n\n"
                f"📈 *Market Data:*\n"
                f"• IV: `{d['implied_vol'] * 100:.2f}%`\n"
                f"• Underl. Price: `{d['underlying_price']:.2f}`\n"
                f"• Volume: `{d['volume']}`\n"
                f"• Open Interest: `{d['open_interest']}`\n\n"
                f"💰 *Last Trade:*\n"
                f"• Price: `{d['last_price']:.2f}`\n"
                f"• Date: `{d['last_date'] or 'N/A'}`"
            )

            await callback.message.answer(msg, parse_mode="Markdown")
            await callback.answer()

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /options callback: {err_detail}")
            await callback.message.answer(f"❌ API Error: {err_detail}")
            await callback.answer()
        except Exception as e:
            logger.error(f"Error in /options callback: {e}", exc_info=True)
            await callback.message.answer("❌ Error interno. Revisa los logs.")
            await callback.answer()


@dp.message(Command("max", ignore_case=True))
async def cmd_max(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            # 1. Fetch Real-time Summary
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/summary", headers=API_HEADERS)
            r.raise_for_status()
            realtime_data = r.json()
            raw_nav = realtime_data.get('NetLiquidation')
            if raw_nav is not None and float(raw_nav) > 0:
                curr_val = float(raw_nav)
            else:
                curr_val = None

            # 2. Get Max NAV from DB
            with SessionLocal() as session:
                max_rec = session.query(CashBalance).order_by(
                    CashBalance.nav.desc()).first()
                if not max_rec:
                    await m.answer("📭 No historical data available in database.")
                    return

                max_val = float(max_rec.nav or 0)

                # If current real-time NAV is higher than historical max, use
                # current as "new high"
                if curr_val is not None and curr_val > max_val:
                    max_val = curr_val
                    max_date_str = "Now (Real-time)"
                else:
                    max_date_str = max_rec.date.strftime("%Y-%m-%d %H:%M:%S")

                if curr_val is not None:
                    drawdown = ((curr_val - max_val) / max_val *
                                100) if max_val > 0 else 0
                    realtime_section = (
                        f"⚡️ *Real-time Status*\n"
                        f"💰 NAV: `{curr_val:.2f}`\n"
                        f"📉 Drawdown: `{drawdown:+.2f}%`"
                    )
                else:
                    realtime_section = r"⚠️ _Real\-time NAV unavailable_"

                msg = (
                    f"🏆 *All Time High*\n"
                    f"💰 NAV: `{max_val:.2f}`\n"
                    f"📅 Date: `{max_date_str}`\n\n"
                    + realtime_section
                )

                # Attach full-history chart
                with SessionLocal() as chart_session:
                    series = _query_nav_series(
                        chart_session,
                        datetime.min,
                        datetime.now(),
                    )
                if curr_val is not None:
                    series.append((datetime.now(), curr_val))

                sent = await _send_nav_chart(m, series, "All Time", msg)
                if not sent:
                    await m.answer(msg, parse_mode="Markdown")

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /max: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /max: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.message(Command("today", "day", ignore_case=True))
async def cmd_today(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            # 1. Fetch Real-time Summary
            curr_val = None
            try:
                r = await client.get(f"{settings.WEB_SERVICE_URL}/account/summary", headers=API_HEADERS)
                if r.status_code == 200:
                    realtime_data = r.json()
                    raw_nav = realtime_data.get('NetLiquidation')
                    if raw_nav is not None and float(raw_nav) > 0:
                        curr_val = float(raw_nav)
            except Exception as e:
                logger.warning(f"Could not fetch real-time NAV: {e}")

            # 2. Query data from DB
            with SessionLocal() as session:
                args = m.text.split()
                n_days = None
                if len(args) > 1:
                    try:
                        n_days = int(args[1])
                    except ValueError:
                        await m.answer("❌ Format error. Use `/today [N]` or `/day [N]`.")
                        return

                now = get_now()
                if n_days is not None:
                    today_start = (now - timedelta(days=n_days)).replace(tzinfo=None)
                    period_name = f"Last {n_days} Day{'s' if n_days > 1 else ''}"
                else:
                    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
                    period_name = "Today"

                today_end = now.replace(tzinfo=None)

                first_rec = session.query(CashBalance).filter(
                    CashBalance.date >= today_start,
                    CashBalance.date <= today_end).order_by(
                    CashBalance.date.asc()).first()
                last_db_rec = session.query(CashBalance).filter(
                    CashBalance.date >= today_start,
                    CashBalance.date <= today_end).order_by(
                    CashBalance.date.desc()).first()
                min_rec = session.query(CashBalance).filter(
                    CashBalance.date >= today_start,
                    CashBalance.date <= today_end).order_by(
                    CashBalance.nav.asc()).first()
                max_rec = session.query(CashBalance).filter(
                    CashBalance.date >= today_start,
                    CashBalance.date <= today_end).order_by(
                    CashBalance.nav.desc()).first()

                if not first_rec and curr_val is None:
                    await m.answer(f"📭 No records found for {period_name}.")
                    return

                include_year = n_days is not None and n_days > 365
                start_nav = float(first_rec.nav) if first_rec else curr_val
                start_date = format_nav_date(first_rec.date, now, include_year) if first_rec else "Now"

                end_nav = curr_val if curr_val is not None else (float(last_db_rec.nav) if last_db_rec else start_nav)
                end_date = "Now" if curr_val is not None else (format_nav_date(last_db_rec.date, now, include_year) if last_db_rec else start_date)
                is_now = curr_val is not None

                period_var = ((end_nav - start_nav) / start_nav * 100) if start_nav else 0

                min_val = float(min_rec.nav) if min_rec else curr_val
                min_date = format_nav_date(min_rec.date, now, include_year) if min_rec else "Now"

                max_val = float(max_rec.nav) if max_rec else curr_val
                max_date = format_nav_date(max_rec.date, now, include_year) if max_rec else "Now"

                if curr_val is not None:
                    if curr_val < min_val:
                        min_val = curr_val
                        min_date = "Now"
                    if curr_val > max_val:
                        max_val = curr_val
                        max_date = "Now"

                range_var = ((max_val - min_val) / min_val * 100) if min_val else 0

                msg = (
                    f"📅 *NAV Analysis for {period_name}*\n\n"
                    f"🏁 *Period:*\n"
                    f"• Start: `{start_nav:.2f}` ({start_date})\n"
                    f"• End:   `{end_nav:.2f}` ({end_date})\n"
                    f"• Var:   `{end_nav - start_nav:+.2f} ({period_var:+.2f}%)`"
                )

                msg += "\n-------------------\n"

                msg += (
                    f"📈 *Range:*\n"
                    f"• Min:   `{min_val:.2f}` ({min_date})\n"
                    f"• Max:   `{max_val:.2f}` ({max_date})\n"
                    f"• Var:   `{max_val - min_val:+.2f} ({range_var:+.2f}%)`"
                )

                # Attach chart
                series = _query_nav_series(session, today_start, today_end)
                if curr_val is not None:
                    series.append((datetime.now(), curr_val))
                sent = await _send_nav_chart(m, series, period_name, msg)
                if not sent:
                    await m.answer(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in cmd_today: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")



@dp.message(Command("year", ignore_case=True))
async def cmd_year(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    target_year = get_now().year
    years_back = None
    if len(args) > 1:
        try:
            val = int(args[1])
            if val < 100:
                years_back = val
                target_year = None
            else:
                target_year = val
        except ValueError:
            await m.answer("❌ Invalid format. Use `/year YYYY` or `/year N`.", parse_mode="Markdown")
            return

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            # 1. Fetch Real-time Summary
            curr_val = None
            try:
                r = await client.get(f"{settings.WEB_SERVICE_URL}/account/summary", headers=API_HEADERS)
                if r.status_code == 200:
                    realtime_data = r.json()
                    raw_nav = realtime_data.get('NetLiquidation')
                    if raw_nav is not None and float(raw_nav) > 0:
                        curr_val = float(raw_nav)
            except Exception as e:
                logger.warning(f"Could not fetch real-time NAV: {e}")

            # 2. Query year data from DB
            with SessionLocal() as session:
                now_dt = get_now().replace(tzinfo=None)
                if years_back is not None:
                    try:
                        year_start = now_dt.replace(year=now_dt.year - years_back)
                    except ValueError:
                        year_start = now_dt.replace(year=now_dt.year - years_back, day=28)
                    year_end = now_dt
                    period_name = f"Last {years_back} Year{'s' if years_back > 1 else ''}"
                else:
                    year_start = datetime(target_year, 1, 1)
                    year_end = datetime(target_year, 12, 31, 23, 59, 59)
                    period_name = str(target_year)

                # Period Start
                first_rec = session.query(CashBalance).filter(
                    CashBalance.date >= year_start,
                    CashBalance.date <= year_end).order_by(
                    CashBalance.date.asc()).first()
                # Period End
                last_db_rec = session.query(CashBalance).filter(
                    CashBalance.date >= year_start,
                    CashBalance.date <= year_end).order_by(
                    CashBalance.date.desc()).first()
                # Min/Max in DB
                min_rec = session.query(CashBalance).filter(
                    CashBalance.date >= year_start,
                    CashBalance.date <= year_end).order_by(
                    CashBalance.nav.asc()).first()
                max_rec = session.query(CashBalance).filter(
                    CashBalance.date >= year_start,
                    CashBalance.date <= year_end).order_by(
                    CashBalance.nav.desc()).first()

                if not first_rec and (
                        curr_val is None or (target_year is not None and target_year != get_now().year)):
                    await m.answer(f"📭 No records found for {period_name}.")
                    return

                # Calculate Start
                now = get_now()
                include_y = years_back is not None
                start_nav = float(first_rec.nav) if first_rec else curr_val
                start_date = format_nav_date(first_rec.date, now, include_y) if first_rec else "Now"

                # Calculate End
                is_now = False
                if (years_back is not None or target_year == now.year) and curr_val is not None:
                    end_nav = curr_val
                    end_date = "Now"
                    is_now = True
                else:
                    end_nav = float(
                        last_db_rec.nav) if last_db_rec else start_nav
                    end_date = format_nav_date(last_db_rec.date, now, include_y) if last_db_rec else start_date

                period_var = ((end_nav - start_nav) /
                              start_nav * 100) if start_nav else 0

                # Calculate Min/Max (including current if applicable)
                min_val = float(min_rec.nav) if min_rec else curr_val
                min_date = format_nav_date(min_rec.date, now, include_y) if min_rec else "Now"

                max_val = float(max_rec.nav) if max_rec else curr_val
                max_date = format_nav_date(max_rec.date, now, include_y) if max_rec else "Now"

                if (years_back is not None or target_year == get_now().year) and curr_val is not None:
                    if curr_val < min_val:
                        min_val = curr_val
                        min_date = "Now"
                    if curr_val > max_val:
                        max_val = curr_val
                        max_date = "Now"

                range_var = (
                    (max_val - min_val) / min_val * 100) if min_val else 0

                msg = (
                    f"📅 *NAV Analysis for {period_name}*\n\n"
                    f"🏁 *Period:*\n"
                    f"• Start: `{start_nav:.2f}` ({start_date})\n"
                    f"• End:   `{end_nav:.2f}` ({end_date})\n"
                    f"• Var:   `{end_nav - start_nav:+.2f} ({period_var:+.2f}%)`"
                )

                msg += "\n-------------------\n"

                msg += (
                    f"📊 *Range:*\n"
                    f"• Min:   `{min_val:.2f}` ({min_date})\n"
                    f"• Max:   `{max_val:.2f}` ({max_date})\n"
                    f"• Var:   `{max_val - min_val:+.2f} ({range_var:+.2f}%)`"
                )

                # Attach chart
                series = _query_nav_series(session, year_start, year_end)
                if curr_val is not None and is_now:
                    series.append((datetime.now(), curr_val))
                sent = await _send_nav_chart(m, series, period_name, msg)
                if not sent:
                    await m.answer(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in cmd_year: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.message(Command("month", ignore_case=True))
async def cmd_month(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            # 1. Fetch Real-time Summary
            curr_val = None
            try:
                r = await client.get(f"{settings.WEB_SERVICE_URL}/account/summary", headers=API_HEADERS)
                if r.status_code == 200:
                    realtime_data = r.json()
                    raw_nav = realtime_data.get('NetLiquidation')
                    if raw_nav is not None and float(raw_nav) > 0:
                        curr_val = float(raw_nav)
            except Exception as e:
                logger.warning(f"Could not fetch real-time NAV: {e}")

            # 2. Query data from DB
            with SessionLocal() as session:
                args = m.text.split()
                n_months = None
                if len(args) > 1:
                    try:
                        n_months = int(args[1])
                    except ValueError:
                        await m.answer("❌ Format error. Use `/month [N]`.")
                        return

                now = get_now()
                if n_months is not None:
                    # Subtract months logic
                    year = now.year - (n_months // 12)
                    month = now.month - (n_months % 12)
                    if month <= 0:
                        month += 12
                        year -= 1
                    try:
                        month_start = now.replace(year=year, month=month).replace(tzinfo=None)
                    except ValueError:
                        # Handle cases like March 31 -> Feb 28
                        import calendar
                        _, last_day = calendar.monthrange(year, month)
                        month_start = now.replace(year=year, month=month, day=last_day).replace(tzinfo=None)
                    
                    period_name = f"Last {n_months} Month{'s' if n_months > 1 else ''}"
                else:
                    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
                    month_name = now.strftime("%B %Y")
                    period_name = month_name

                month_end = now.replace(tzinfo=None)

                first_rec = session.query(CashBalance).filter(
                    CashBalance.date >= month_start,
                    CashBalance.date <= month_end).order_by(
                    CashBalance.date.asc()).first()
                last_db_rec = session.query(CashBalance).filter(
                    CashBalance.date >= month_start,
                    CashBalance.date <= month_end).order_by(
                    CashBalance.date.desc()).first()
                min_rec = session.query(CashBalance).filter(
                    CashBalance.date >= month_start,
                    CashBalance.date <= month_end).order_by(
                    CashBalance.nav.asc()).first()
                max_rec = session.query(CashBalance).filter(
                    CashBalance.date >= month_start,
                    CashBalance.date <= month_end).order_by(
                    CashBalance.nav.desc()).first()

                if not first_rec and curr_val is None:
                    await m.answer(f"📭 No records found for {period_name}.")
                    return

                include_year = (n_months is not None and n_months > 12) or (n_months is None)
                start_nav = float(first_rec.nav) if first_rec else curr_val
                start_date = format_nav_date(first_rec.date, now, include_year) if first_rec else "Now"

                end_nav = curr_val if curr_val is not None else (float(last_db_rec.nav) if last_db_rec else start_nav)
                end_date = "Now" if curr_val is not None else (format_nav_date(last_db_rec.date, now, include_year) if last_db_rec else start_date)
                is_now = curr_val is not None

                period_var = ((end_nav - start_nav) / start_nav * 100) if start_nav else 0

                min_val = float(min_rec.nav) if min_rec else curr_val
                min_date = format_nav_date(min_rec.date, now, include_year) if min_rec else "Now"

                max_val = float(max_rec.nav) if max_rec else curr_val
                max_date = format_nav_date(max_rec.date, now, include_year) if max_rec else "Now"

                if curr_val is not None:
                    if curr_val < min_val:
                        min_val = curr_val
                        min_date = "Now"
                    if curr_val > max_val:
                        max_val = curr_val
                        max_date = "Now"

                range_var = ((max_val - min_val) / min_val * 100) if min_val else 0

                msg = (
                    f"📅 *NAV Analysis for {period_name}*\n\n"
                    f"🏁 *Period:*\n"
                    f"• Start: `{start_nav:.2f}` ({start_date})\n"
                    f"• End:   `{end_nav:.2f}` ({end_date})\n"
                    f"• Var:   `{end_nav - start_nav:+.2f} ({period_var:+.2f}%)`"
                )

                msg += "\n-------------------\n"

                msg += (
                    f"📊 *Range:*\n"
                    f"• Min:   `{min_val:.2f}` ({min_date})\n"
                    f"• Max:   `{max_val:.2f}` ({max_date})\n"
                    f"• Var:   `{max_val - min_val:+.2f} ({range_var:+.2f}%)`"
                )

                # Attach chart
                series = _query_nav_series(session, month_start, month_end)
                if curr_val is not None:
                    series.append((datetime.now(), curr_val))
                sent = await _send_nav_chart(m, series, period_name, msg)
                if not sent:
                    await m.answer(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in cmd_month: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.message(Command("week", ignore_case=True))
async def cmd_week(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            curr_val = None
            try:
                r = await client.get(f"{settings.WEB_SERVICE_URL}/account/summary", headers=API_HEADERS)
                if r.status_code == 200:
                    realtime_data = r.json()
                    raw_nav = realtime_data.get('NetLiquidation')
                    if raw_nav is not None and float(raw_nav) > 0:
                        curr_val = float(raw_nav)
            except Exception as e:
                logger.warning(f"Could not fetch real-time NAV: {e}")

            with SessionLocal() as session:
                args = m.text.split()
                n_weeks = None
                if len(args) > 1:
                    try:
                        n_weeks = int(args[1])
                    except ValueError:
                        await m.answer("❌ Format error. Use `/week [N]`.")
                        return

                now = get_now()
                if n_weeks is not None:
                    week_start = (now - timedelta(weeks=n_weeks)).replace(tzinfo=None)
                    period_name = f"Last {n_weeks} Week{'s' if n_weeks > 1 else ''}"
                else:
                    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
                    week_num = now.isocalendar()[1]
                    period_name = f"Week {week_num}"

                week_end = now.replace(tzinfo=None)

                first_rec = session.query(CashBalance).filter(
                    CashBalance.date >= week_start,
                    CashBalance.date <= week_end).order_by(
                    CashBalance.date.asc()).first()
                last_db_rec = session.query(CashBalance).filter(
                    CashBalance.date >= week_start,
                    CashBalance.date <= week_end).order_by(
                    CashBalance.date.desc()).first()
                min_rec = session.query(CashBalance).filter(
                    CashBalance.date >= week_start,
                    CashBalance.date <= week_end).order_by(
                    CashBalance.nav.asc()).first()
                max_rec = session.query(CashBalance).filter(
                    CashBalance.date >= week_start,
                    CashBalance.date <= week_end).order_by(
                    CashBalance.nav.desc()).first()

                if not first_rec and curr_val is None:
                    await m.answer(f"📭 No records found for {period_name}.")
                    return

                include_year = n_weeks is not None and n_weeks > 52
                start_nav = float(first_rec.nav) if first_rec else curr_val
                start_date = format_nav_date(first_rec.date, now, include_year) if first_rec else "Now"

                end_nav = curr_val if curr_val is not None else (float(last_db_rec.nav) if last_db_rec else start_nav)
                end_date = "Now" if curr_val is not None else (format_nav_date(last_db_rec.date, now, include_year) if last_db_rec else start_date)
                is_now = curr_val is not None

                period_var = ((end_nav - start_nav) / start_nav * 100) if start_nav else 0

                min_val = float(min_rec.nav) if min_rec else curr_val
                min_date = format_nav_date(min_rec.date, now, include_year) if min_rec else "Now"

                max_val = float(max_rec.nav) if max_rec else curr_val
                max_date = format_nav_date(max_rec.date, now, include_year) if max_rec else "Now"

                if curr_val is not None:
                    if curr_val < min_val:
                        min_val = curr_val
                        min_date = "Now"
                    if curr_val > max_val:
                        max_val = curr_val
                        max_date = "Now"

                range_var = ((max_val - min_val) / min_val * 100) if min_val else 0

                msg = (
                    f"📅 *NAV Analysis for {period_name}*\n\n"
                    f"🏁 *Period:*\n"
                    f"• Start: `{start_nav:.2f}` ({start_date})\n"
                    f"• End:   `{end_nav:.2f}` ({end_date})\n"
                    f"• Var:   `{end_nav - start_nav:+.2f} ({period_var:+.2f}%)`"
                )

                msg += "\n-------------------\n"

                msg += (
                    f"📊 *Range:*\n"
                    f"• Min:   `{min_val:.2f}` ({min_date})\n"
                    f"• Max:   `{max_val:.2f}` ({max_date})\n"
                    f"• Var:   `{max_val - min_val:+.2f} ({range_var:+.2f}%)`"
                )

                # Attach chart
                series = _query_nav_series(session, week_start, week_end)
                if curr_val is not None:
                    series.append((datetime.now(), curr_val))
                sent = await _send_nav_chart(m, series, period_name, msg)
                if not sent:
                    await m.answer(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in cmd_week: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.message(Command("help", ignore_case=True))
async def cmd_help(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    help_text = (
        "🤖 *IBKR Bot Commands:*\n\n"
        "💰 /nav - Show current NAV and Cushion\n"
        "📦 /pos - Show current positions\n"
        "📋 /orders - Show active open orders\n"
        "🤝 /trades - Show today's executions\n"
        "📈 /quote SYMBOL - Real-time price snapshot\n"
        "📄 /contract SYMBOL - Search contract details\n"
        "🔗 /chain SYMBOL - Show option chain\n"
        "📑 /options - Interactive options dashboard\n"
        "🏆 /max - Show All Time High\n"
        "📊 /today [N] - Today's (or last N days) NAV variation\n"
        "📅 /week [N] - Week's (or last N weeks) NAV variation\n"
        "📅 /month [N] - Month's (or last N months) NAV variation\n"
        "📅 /year [YYYY|N] - Year's (or last N years) NAV variation\n"
        "📊 /flex [PARAM] - Manual Flex Report (PARAM: monthly|YYYYMMDD)\n"
        "⚠️ /delta - Check high delta short positions now\n"
        "❓ /help - Show this help message"
    )

    await m.answer(help_text, parse_mode="Markdown")


@dp.message(Command("orders", ignore_case=True))
async def cmd_orders(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/orders", headers=API_HEADERS)
            r.raise_for_status()
            orders = r.json()

            if not orders:
                await m.answer("📭 No active orders.")
                return

            msg = "📋 *Active Orders*:\n"
            for o in orders[:15]:  # Limit to 15
                msg += f"• `{o['action']} {o['totalQuantity']} {o['symbol']} @ {o['lmtPrice'] or 'MKT'}` ({o['status']})\n"

            await m.answer(msg, parse_mode="Markdown")
        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /orders: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /orders: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.message(Command("trades", ignore_case=True))
async def cmd_trades(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/account/trades", headers=API_HEADERS)
            r.raise_for_status()
            trades = r.json()

            if not trades:
                await m.answer("📭 No trades executed today.")
                return

            msg = "🤝 *Recent Trades*:\n"
            for t in trades[:15]:
                # 2025-12-30T10:00:00 -> 10:00:00
                time_str = t['time'].split('T')[1].split(
                    '.')[0] if 'T' in t['time'] else t['time']
                msg += f"• `{time_str}`: {t['side']} `{t['shares']}` *{t['symbol']}* @ `{t['price']}`\n"

            await m.answer(msg, parse_mode="Markdown")
        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /trades: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /trades: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.message(Command("quote", ignore_case=True))
async def cmd_quote(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) < 2:
        await m.answer("ℹ️ Usage: `/quote <SYMBOL>` (e.g. `/quote SPY`)", parse_mode="Markdown")
        return

    symbol = args[1].upper()

    msg = await m.answer(f"🔍 Getting quote for {symbol}...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/market/snapshot/{symbol}", headers=API_HEADERS)
            r.raise_for_status()
            data = r.json()

            # Format output
            # Symbol: SPY
            # Price: 420.50
            # Bid/Ask: 420.40 / 420.60

            out = f"📈 *Quote: {data['symbol']}*\n\n"
            out += f"💰 Price: `{data['price']:.2f}`\n"
            if data.get('bid') and data.get('ask'):
                out += f"↔️ Bid/Ask: `{data['bid']:.2f} / {data['ask']:.2f}`\n"

            # Format timestamp: 2025-12-30T16:26:52.882122Z -> 2025-12-30
            # 16:26:52
            ts_str = data['timestamp']
            if 'T' in ts_str:
                # Convert Z string to timezone aware datetime if possible, otherwise just string manipulation
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
            await msg.edit_text("❌ Error interno. Revisa los logs.")


@dp.message(Command("contract", ignore_case=True))
async def cmd_contract(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) < 2:
        await m.answer("ℹ️ Usage: `/contract <SYMBOL>`", parse_mode="Markdown")
        return

    symbol = args[1].upper()

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/contract/search?symbol={symbol}", headers=API_HEADERS)
            r.raise_for_status()
            details = r.json()

            if not details:
                await m.answer(f"❌ No contract found for {symbol}.")
                return

            out = f"📄 *Contract Details ({len(details)})*:\n\n"
            for d in details[:3]:  # Limit to 3 detailed views
                out += f"🔹 *{d['symbol']}* ({d['secType']})\n"
                out += f"   • Name: {d['longName']}\n"
                out += f"   • ID: `{d['conId']}` | Exch: {d['exchange']}\n"
                if d.get('isin'):
                    out += f"   • ISIN: `{d['isin']}`\n"
                out += "\n"

            await m.answer(out, parse_mode="Markdown")
        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /contract: {err_detail}")
            await m.answer(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /contract: {e}", exc_info=True)
            await m.answer("❌ Error interno. Revisa los logs.")


@dp.message(Command("chain", ignore_case=True))
async def cmd_chain(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) < 2:
        await m.answer("ℹ️ Usage: `/chain <SYMBOL>` (e.g. `/chain AAPL`)", parse_mode="Markdown")
        return

    symbol = args[1].upper()
    msg = await m.answer(f"🔍 Fetching option chain for {symbol}...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/options/chain/{symbol}", headers=API_HEADERS)
            r.raise_for_status()
            chains = r.json()

            if not chains:
                await msg.edit_text(f"❌ No option chain found for {symbol}.")
                return

            # Use the first chain (usually SMART exchange)
            chain = chains[0]
            expirations = chain.get('expirations', [])
            strikes = chain.get('strikes', [])

            # Format expiration dates (YYYYMMDD -> YYYY-MM-DD)
            def fmt_exp(exp):
                return f"{exp[:4]}-{exp[4:6]}-{exp[6:]}"

            # Group expirations by month for compact display
            exp_by_month = {}
            for exp in expirations[:24]:  # Limit to next 24 expirations
                month_key = exp[:6]  # YYYYMM
                if month_key not in exp_by_month:
                    exp_by_month[month_key] = []
                exp_by_month[month_key].append(exp[6:])  # Just the day

            out = f"📊 <b>Option Chain: {symbol}</b>\n"
            out += f"<code>📅 Exchange: {chain['exchange']} | Mult: {chain['multiplier']}</code>\n\n"

            out += "<b>Expirations:</b>\n"
            for month_key in sorted(exp_by_month.keys())[:6]:  # Show 6 months
                year = month_key[:4]
                month = month_key[4:6]
                # Limit days per month
                days = ", ".join(exp_by_month[month_key][:8])
                out += f"<code>{year}-{month}: {days}</code>\n"
            if len(exp_by_month) > 6:
                out += f"<code>... +{len(exp_by_month) - 6} more months</code>\n"

            # Show strike range
            if strikes:
                min_strike = min(strikes)
                max_strike = max(strikes)
                mid_idx = len(strikes) // 2
                sample_strikes = strikes[max(0, mid_idx - 3):mid_idx + 4]
                out += f"<code>Strikes: {min_strike:.0f} - {max_strike:.0f} ({len(strikes)} total)</code>\n"
                out += f"<code>Sample: {', '.join(f'{s:.0f}' for s in sample_strikes)}</code>"

            await msg.edit_text(out, parse_mode="HTML")
        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /chain: {err_detail}")
            await msg.edit_text(f"❌ API Error: {err_detail}")
        except Exception as e:
            logger.error(f"Error in /chain: {e}", exc_info=True)
            await msg.edit_text("❌ Error interno. Revisa los logs.")

# Scheduler


async def check_token_expiry():
    if not settings.IB_FLEX_TOKEN_EXPIRY:
        return

    try:
        # Expected format: "2026-02-18, 05:34:27 EST"
        expiry_str = settings.IB_FLEX_TOKEN_EXPIRY.split(',')[0].strip()
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d")
        days_left = (expiry_date - get_now().replace(tzinfo=None)).days

        if 0 <= days_left <= 10:
            await notify_admins(
                f"⚠️ *IBKR Flex Token Expiry Alert*\n\n"
                f"Your token will expire in *{days_left} days* (`{expiry_str}`).\n"
                f"Please generate a new one to avoid service interruption."
            )
        elif days_left < 0:
            await notify_admins(
                f"❌ *IBKR Flex Token EXPIRED*\n\n"
                f"Your token expired on `{expiry_str}`. Flex reports will fail until a new token is provided."
            )
    except Exception as e:
        logger.error(f"Error checking token expiry: {e}")


async def scheduled_flex_report(
        query_id=None, report_type="Daily", retry_count=0, local_date=None):
    attempt_str = f" (Attempt {retry_count + 1})" if not local_date else f" (Local: {local_date})"
    logger.info(
        f"Running scheduled {report_type} Flex Query Report{attempt_str}...")
    try:
        # Run blocking report generation in a thread
        # Now returns (html, date_range_html, date_range_subject,
        # telegram_msgs, archive_status)
        html, date_range_html, date_range_subject, telegram_msgs, archive_status = await asyncio.to_thread(
            FlexReporter.run_report,
            query_id=query_id,
            local_date=local_date,
            report_type=report_type
        )

        if not date_range_html:
            # Log warning instead of error for retries
            logger.warning(f"{report_type} Flex Query failed: {html}")

            # Retry only if it's a scheduled run (not local)
            if not local_date:
                if retry_count < 10:
                    next_run = datetime.now() + timedelta(hours=1)
                    scheduler.add_job(
                        scheduled_flex_report,
                        'date',
                        run_date=next_run,
                        args=[query_id, report_type, retry_count + 1],
                        id=f'flex_retry_{report_type}',
                        replace_existing=True,
                        max_instances=1
                    )
                    logger.info(f"Rescheduled {report_type} Flex Report retry #{retry_count + 1} for {next_run}")
                    return
                else:
                    logger.error(f"{report_type} Flex Query failed after 10 retries: {html}")
                    await notify_admins(f"⚠️ {report_type} Flex Query Report Error (Failed after 10 attempts): {html}")
                    return
            else:
                await notify_admins(f"❌ Local {report_type} Flex Query Error: {html}")
                return

        # Run blocking email sending in a thread
        project_prefix = settings.PROJECT_NAME.upper()
        if report_type == "Monthly":
            subject = f"{project_prefix} - IB Flex Query {date_range_subject}"
        else:
            subject = f"{project_prefix} - IB {report_type} Flex Query {date_range_html}"

        if local_date:
            subject += " (Local Re-run)"

        email_status = await asyncio.to_thread(FlexReporter.send_email, html, subject)

        # Send unified Telegram Message (Summary + Dividends etc)
        combined_msg = f"📅 <b>{report_type} Flex Query Date</b>: <code>{date_range_html}</code>\n\n"

        for msg in telegram_msgs:
            if msg.strip():
                # Add each section directly, since it's already HTML formatted
                combined_msg += f"{msg.strip()}\n\n"

        # Add completion status with Archiving info
        combined_msg += (
            f"📊 <b>{report_type} Report Generated</b>\n"
            f"Date: <code>{date_range_html}</code>\n"
            f"Archived: <code>{archive_status}</code>\n"
            f"Email: <code>{email_status}</code>"
        )
        
        await notify_admins(combined_msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"{report_type} Scheduler/Report Error: {e}")
        if not local_date:
            if retry_count < 10:
                next_run = datetime.now() + timedelta(hours=1)
                scheduler.add_job(
                    scheduled_flex_report,
                    'date',
                    run_date=next_run,
                    args=[query_id, report_type, retry_count + 1],
                    id=f'flex_retry_{report_type}',
                    replace_existing=True,
                    max_instances=1
                )
                logger.info(f"Rescheduled {report_type} Flex Report retry #{retry_count + 1} (due to error) for {next_run}")
            else:
                await notify_admins(f"⚠️ {report_type} Flex Query System Error (Failed after 10 attempts): {e}")
        else:
            await notify_admins(f"❌ Local {report_type} Flex Query Exception: {e}")


@dp.message(Command("flex", ignore_case=True))
async def cmd_flex(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) > 1:
        arg = args[1].lower().strip()
        if arg == "monthly":
            await m.answer("Generating Monthly Flex Query Report... ⏳")
            await scheduled_flex_report(query_id=settings.IB_FLEX_MONTHLY_QUERY_ID, report_type="Monthly")
            return

        local_date = arg
        # Basic validation
        if not (len(local_date) == 8 and local_date.isdigit()):
            await m.answer("❌ Invalid format. Use /flex YYYYMMDD (e.g. /flex 20251229) or /flex monthly")
            return
        await m.answer(f"Processing local report for {local_date}.xml ... ⏳")
        await scheduled_flex_report(local_date=local_date)
    else:
        await m.answer("Generating Daily Flex Query Report... ⏳")
        await scheduled_flex_report(query_id=settings.IB_FLEX_DAILY_QUERY_ID, report_type="Daily")




@dp.message(Command("delta", ignore_case=True))
async def cmd_delta(m: types.Message):
    """On-demand check: show all short option positions with high delta."""
    if m.from_user.id not in settings.allowed_ids_list:
        return

    await m.answer("Checking deltas for short positions... ⏳")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch positions
            r_pos = await client.get(
                f"{settings.WEB_SERVICE_URL}/account/positions",
                headers=API_HEADERS
            )
            if r_pos.status_code != 200:
                await m.answer(f"❌ Failed to fetch positions (HTTP {r_pos.status_code})")
                return

            positions = r_pos.json()
            short_options = [
                p for p in positions
                if p.get('secType') == 'OPT' and p.get('qty', 0) < 0
                and p.get('underlying', '').upper() not in settings.delta_alert_exclude_list
            ]

            if not short_options:
                await m.answer("✅ No short option positions found.")
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

                            # Capture price data for IV/TV calculation
                            raw_last = data.get('last_price', 0.0)
                            raw_und = data.get('underlying_price', 0.0)
                            if raw_last and raw_last > 0:
                                last_price = raw_last
                            if raw_und and raw_und > 0:
                                underlying_price = raw_und

                            # Calculate data age in minutes
                            last_date_str = data.get('last_date')
                            if last_date_str:
                                try:
                                    last_dt = datetime.strptime(last_date_str, "%Y-%m-%d %H:%M:%S")
                                    age_min = int((datetime.now() - last_dt).total_seconds() / 60)
                                    age_str = f"{age_min}m" if age_min >= 2 else ""
                                except (ValueError, TypeError):
                                    pass
                    except Exception as e:
                        logger.debug(
                            f"Error fetching greeks for conId={con_id}: {e}")

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
                await m.answer("✅ No short option positions found.")
                return

            # Sort: contracts with delta first (by abs desc), then None deltas at the bottom
            results.sort(key=lambda x: (x['delta'] is None, -abs(x['delta']) if x['delta'] is not None else 0))

            # Calculate max widths for alignment
            max_qty = max(len(f"{r['qty']:.0f}") for r in results)
            max_und = 5
            max_rs = max(len(r['right_strike']) for r in results)

            # Build digest message
            high_count = sum(1 for r in results if r['high'])
            no_data_count = sum(1 for r in results if r['delta'] is None)
            header = f"📊 <b>Delta Report — {len(results)} Short Position(s)</b>\n"
            if high_count:
                header += f"⚠️ <b>{high_count} above threshold ({settings.DELTA_ALERT_THRESHOLD})</b>\n"
            if no_data_count:
                header += f"⚪ <b>{no_data_count} without delta data</b>\n"

            # Calculate TV strings first to determine max_tv
            for r in results:
                r['tv_str'] = ""
                r['low_tv'] = False
                if r['intrinsic'] is not None and r['time_value'] is not None:
                    tv_val = f"{r['time_value']:+.2f}".replace("+0.", "+.").replace("-0.", "-.")
                    r['tv_str'] = f"TV{tv_val}"
                    if r['intrinsic'] > 0 and r['time_value'] <= (0.01 * r['strike']):
                        r['low_tv'] = True

            has_any_tv = any(r['tv_str'] != "" for r in results)
            max_tv = max(len(r['tv_str']) for r in results if r['tv_str'] != "") if has_any_tv else 0
            has_any_low_tv = any(r['low_tv'] for r in results)

            option_lines = []
            for r in results:
                if r['delta'] is None:
                    marker = "⚪"
                    delta_str = "  —  "
                else:
                    marker = "🔴" if r['high'] else "🟢"
                    delta_str = f"{abs(r['delta']):.2f}"
                qty_str = f"{r['qty']:.0f}".rjust(max_qty)
                und_padded = r['underlying'].ljust(max_und)
                rs_padded = r['right_strike'].ljust(max_rs)
                age = r['age']

                # Format expiry to DDMMMyy for consistent alignment (e.g. 19Jun26)
                display_expiry = r['expiry']
                if len(display_expiry) == 8 and display_expiry.isdigit():
                    try:
                        dt = datetime.strptime(display_expiry, "%Y%m%d")
                        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                        month_str = months[dt.month - 1]
                        display_expiry = f"{dt.day:02d}{month_str}{dt.year % 100:02d}"
                    except Exception:
                        pass

                # Append TV column if available
                tv_part = ""
                if r['tv_str']:
                    tv_padded = r['tv_str'].ljust(max_tv)
                    if has_any_low_tv:
                        warning_indicator = "⚠️" if r['low_tv'] else "  "
                        tv_part = f" {tv_padded}{warning_indicator}"
                    else:
                        tv_part = f" {tv_padded}"
                elif has_any_tv:
                    # Only add empty spacing if this line has an age, to keep it aligned
                    if age:
                        padding_len = max_tv + (2 if has_any_low_tv else 0) + 1
                        tv_part = " " * padding_len

                line = f"{marker} <code>{qty_str} {und_padded} {rs_padded} {display_expiry} Δ{delta_str}{tv_part} {age}</code>"
                option_lines.append(line.strip())

            options_text = "\n".join(option_lines)
            message = f"{header}\n{options_text}\n\n🔴 abs(Δ) &gt; {settings.DELTA_ALERT_THRESHOLD}  🟢 abs(Δ) ≤ {settings.DELTA_ALERT_THRESHOLD}  ⚪ no data\n⚠️ low time value (≤ 1% strike)"

            await m.answer(message, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error in /delta: {e}", exc_info=True)
        await m.answer("❌ Error interno. Revisa los logs.")


async def main():
    # 1. Schedule: Tue,Wed,Thu,Fri,Sat for Flex Query Reports
    # Parse configured time (default 07:30)
    try:
        sh, sm = map(int, settings.IB_FLEX_SCHEDULE_TIME.split(':'))
    except ValueError:
        logger.error(f"Invalid IB_FLEX_SCHEDULE_TIME format: {settings.IB_FLEX_SCHEDULE_TIME}. Defaulting to 07:30")
        sh, sm = 7, 30

    # Daily Flex Query: Tue,Wed,Thu,Fri,Sat
    scheduler.add_job(
        scheduled_flex_report,
        'cron',
        day_of_week='tue,wed,thu,fri,sat',
        hour=sh,
        minute=sm,
        args=[settings.IB_FLEX_DAILY_QUERY_ID, "Daily"]
    )

    # Monthly Flex Query: 1st of each month at 12:00
    scheduler.add_job(
        scheduled_flex_report,
        'cron',
        day='1',
        hour=12,
        minute=0,
        args=[settings.IB_FLEX_MONTHLY_QUERY_ID, "Monthly"]
    )

    # 2. Schedule: Daily check for token expiry at 09:00
    scheduler.add_job(check_token_expiry, 'cron', hour=9, minute=0)

    # Calculate intervals in minutes
    check_interval_min = max(1, settings.CASH_DIFFERENCE_CHECK_INTERVAL // 60)
    db_insert_interval_min = max(1, settings.DB_INSERT_INTERVAL // 60)

    # 3. Schedule: Periodic DB snapshots (Fixed time, e.g. :00, :30)
    # We want these to happen EXACTLY at the interval marks
    snap_mins = set(range(0, 60, db_insert_interval_min)
                    ) if db_insert_interval_min < 60 else {0}
    snap_cron = ",".join(map(str, sorted(snap_mins)))

    scheduler.add_job(
        check_and_archive,
        'cron',
        day_of_week='mon-fri',
        hour='7-23',
        minute=snap_cron,
        args=[True],  # force_insert=True
        max_instances=1,
        id='periodic_db_snapshot'
    )

    # 4. Schedule: Cash change detection
    # Run at check intervals BUT skip minutes where a snapshot (forced insert)
    # creates a redundancy
    check_mins = set(range(0, 60, check_interval_min)
                     ) if check_interval_min < 60 else {0}
    effective_check_mins = check_mins - snap_mins

    check_cron = None  # Initialize before conditional to avoid potential UnboundLocalError
    if effective_check_mins:
        check_cron = ",".join(map(str, sorted(effective_check_mins)))
        scheduler.add_job(
            check_and_archive,  # force_insert defaults to False
            'cron',
            day_of_week='mon-fri',
            hour='7-23',
            minute=check_cron,
            max_instances=1,
            id='cash_change_check'
        )

    # 5. Schedule: Weekend cash control points (Sat, Sun at 12:00)
    scheduler.add_job(
        check_and_archive,
        'cron',
        day_of_week='sat,sun',
        hour=12,
        minute=0,
        args=[True],  # force_insert=True
        max_instances=1,
        id='weekend_cash_control'
    )

    # 6. Schedule: Alert Monitoring
    DELTA_ALERT_TIMES = [t.strip() for t in settings.DELTA_ALERT_TIMES.split(",") if t.strip()]
    for idx, time_str in enumerate(DELTA_ALERT_TIMES):
        try:
            ah, am = map(int, time_str.split(':'))
            scheduler.add_job(
                monitor.check_alerts,
                'cron',
                day_of_week='mon-fri',
                hour=ah,
                minute=am,
                id=f'alert_monitoring_{idx}'
            )
        except ValueError:
            logger.error(f"Invalid alert time format: {time_str}")

    # 7. Schedule: Greeks Cache Refresh (European Hours)
    # Mon-Fri 09:00-18:00 every 15 mins
    scheduler.add_job(
        monitor.refresh_greeks_cache,
        'cron',
        day_of_week='mon-fri',
        hour='9-18',
        minute='*/15',
        id='greeks_cache_refresh'
    )

    # 8. Schedule: DB Cleanup (Daily at 04:00)
    scheduler.add_job(
        monitor.prune_old_snapshots,
        'cron',
        hour=4,
        minute=0,
        id='db_cleanup_snapshots'
    )
    scheduler.add_job(
        monitor.prune_old_market_cache,
        'cron',
        hour=4,
        minute=5,
        id='db_cleanup_market_cache'
    )

    check_cron_log = check_cron if effective_check_mins else 'None (covered by snapshots)'
    logger.info(
        f"Scheduler configured: Snapshots at mins={snap_cron}, Checks at mins={check_cron_log}, Weekend at 12:00")

    scheduler.start()

    # Initial checks (no forced DB inserts on startup to respect intervals)
    await check_token_expiry()

    await dp.start_polling(bot)

if __name__ == "__main__":
    if not settings.TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not set")
        exit(1)
    asyncio.run(main())
