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

# ---------------------------------------------------------------------------
# Telegram Bot API 10.1 Rich Messages Helpers
# ---------------------------------------------------------------------------

def text_plain(t: str) -> dict:
    return {"type": "plain", "text": t}

def text_bold(t) -> dict:
    if isinstance(t, str):
        t = text_plain(t)
    return {"type": "bold", "text": t}

def text_italic(t) -> dict:
    if isinstance(t, str):
        t = text_plain(t)
    return {"type": "italic", "text": t}

def text_code(t) -> dict:
    if isinstance(t, str):
        t = text_plain(t)
    return {"type": "code", "text": t}

def text_url(t, url: str) -> dict:
    if isinstance(t, str):
        t = text_plain(t)
    return {"type": "url", "text": t, "url": url}

def text_concat(*args) -> dict:
    processed = []
    for arg in args:
        if isinstance(arg, str):
            processed.append(text_plain(arg))
        elif isinstance(arg, dict):
            processed.append(arg)
        elif isinstance(arg, list):
            processed.extend([text_plain(x) if isinstance(x, str) else x for x in arg])
    if not processed:
        return text_plain("")
    if len(processed) == 1:
        return processed[0]
    return {"type": "texts", "texts": processed}

def html_to_rich(html_text: str) -> dict:
    import re
    pattern = re.compile(r'(<b>.*?</b>|<i>.*?</i>|<code>.*?</code>|[^<]+|<)')
    tokens = pattern.findall(html_text)
    spans = []
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("<b>") and tok.endswith("</b>"):
            spans.append(text_bold(tok[3:-4]))
        elif tok.startswith("<i>") and tok.endswith("</i>"):
            spans.append(text_italic(tok[3:-4]))
        elif tok.startswith("<code>") and tok.endswith("</code>"):
            spans.append(text_code(tok[6:-7]))
        else:
            spans.append(text_plain(tok))
    return text_concat(*spans)

def block_paragraph(text_obj) -> dict:
    if isinstance(text_obj, str):
        text_obj = text_plain(text_obj)
    return {"type": "paragraph", "text": text_obj}

def block_heading(text_obj) -> dict:
    if isinstance(text_obj, str):
        text_obj = text_plain(text_obj)
    return {"type": "sectionHeading", "text": text_obj}

def block_thinking() -> dict:
    return {"type": "thinking"}

def cell(text_obj=None, is_header: bool = False, align: str = None, valign: str = None, colspan: int = None, rowspan: int = None) -> dict:
    res = {}
    if text_obj is not None:
        if isinstance(text_obj, str):
            text_obj = text_plain(text_obj)
        res["text"] = text_obj
    if is_header:
        res["is_header"] = True
    if align:
        res["align"] = align
    if valign:
        res["valign"] = valign
    if colspan and colspan > 1:
        res["colspan"] = colspan
    if rowspan and rowspan > 1:
        res["rowspan"] = rowspan
    return res

def block_table(cells: list[list[dict]], is_bordered: bool = False, is_striped: bool = False, caption=None) -> dict:
    res = {
        "type": "table",
        "cells": cells
    }
    if is_bordered:
        res["is_bordered"] = True
    if is_striped:
        res["is_striped"] = True
    if caption:
        if isinstance(caption, str):
            caption = text_plain(caption)
        res["caption"] = caption
    return res

def block_details(title, blocks: list[dict], is_open: bool = False) -> dict:
    if isinstance(title, str):
        title = text_plain(title)
    res = {
        "type": "details",
        "title": title,
        "blocks": blocks
    }
    if is_open:
        res["is_open"] = True
    return res

from typing import Union, Optional, Any
from aiogram.methods.base import TelegramMethod
from pydantic import Field

class SendRichMessage(TelegramMethod[types.Message]):
    __returning__ = types.Message
    __api_method__ = "sendRichMessage"

    chat_id: Union[int, str] = Field(..., alias="chat_id")
    rich_message: dict = Field(..., alias="rich_message")
    reply_markup: Optional[Any] = Field(None, alias="reply_markup")

class EditMessageTextRich(TelegramMethod[Union[types.Message, bool]]):
    __returning__ = Union[types.Message, bool]
    __api_method__ = "editMessageText"

    chat_id: Optional[Union[int, str]] = Field(None, alias="chat_id")
    message_id: Optional[int] = Field(None, alias="message_id")
    inline_message_id: Optional[str] = Field(None, alias="inline_message_id")
    rich_message: dict = Field(..., alias="rich_message")
    reply_markup: Optional[Any] = Field(None, alias="reply_markup")

def text_to_html(text_obj) -> str:
    import html
    if isinstance(text_obj, str):
        return html.escape(text_obj)
    if isinstance(text_obj, dict):
        t_type = text_obj.get("type")
        if t_type == "plain":
            return html.escape(text_obj.get("text", ""))
        elif t_type == "bold":
            return f"<b>{text_to_html(text_obj.get('text'))}</b>"
        elif t_type == "italic":
            return f"<i>{text_to_html(text_obj.get('text'))}</i>"
        elif t_type == "code":
            return f"<code>{text_to_html(text_obj.get('text'))}</code>"
        elif t_type == "url":
            url = html.escape(text_obj.get("url", ""))
            return f'<a href="{url}">{text_to_html(text_obj.get("text"))}</a>'
        elif t_type == "texts":
            return "".join(text_to_html(x) for x in text_obj.get("texts", []))
    if isinstance(text_obj, list):
        return "".join(text_to_html(x) for x in text_obj)
    return ""

def block_to_html(block_obj) -> str:
    if isinstance(block_obj, dict):
        b_type = block_obj.get("type")
        if b_type == "paragraph":
            return f"<p>{text_to_html(block_obj.get('text'))}</p>"
        elif b_type == "sectionHeading":
            return f"<h1>{text_to_html(block_obj.get('text'))}</h1>"
        elif b_type == "thinking":
            return "<tg-thinking></tg-thinking>"
        elif b_type == "table":
            attrs = []
            if block_obj.get("is_bordered"):
                attrs.append('border="1"')
            attrs_str = " " + " ".join(attrs) if attrs else ""
            
            caption_html = ""
            if "caption" in block_obj:
                caption_html = f"<caption>{text_to_html(block_obj.get('caption'))}</caption>"
            
            rows_html = []
            for row in block_obj.get("cells", []):
                row_cells = []
                for c in row:
                    tag = "th" if c.get("is_header") else "td"
                    c_attrs = []
                    if c.get("align"):
                        c_attrs.append(f'align="{c.get("align")}"')
                    if c.get("valign"):
                        c_attrs.append(f'valign="{c.get("valign")}"')
                    if c.get("colspan"):
                        c_attrs.append(f'colspan="{c.get("colspan")}"')
                    if c.get("rowspan"):
                        c_attrs.append(f'rowspan="{c.get("rowspan")}"')
                    c_attrs_str = " " + " ".join(c_attrs) if c_attrs else ""
                    cell_text = text_to_html(c.get("text")) if "text" in c else ""
                    row_cells.append(f"<{tag}{c_attrs_str}>{cell_text}</{tag}>")
                rows_html.append(f"<tr>{''.join(row_cells)}</tr>")
            
            return f"<table{attrs_str}>{caption_html}{''.join(rows_html)}</table>"
        elif b_type == "details":
            open_attr = " open" if block_obj.get("is_open") else ""
            summary_html = f"<summary>{text_to_html(block_obj.get('title'))}</summary>"
            content_html = "".join(block_to_html(b) for b in block_obj.get("blocks", []))
            return f"<details{open_attr}>{summary_html}{content_html}</details>"
    return ""

async def send_rich_message(chat_id: int, blocks: list[dict], reply_markup=None) -> types.Message:
    html_content = "".join(block_to_html(b) for b in blocks)
    return await bot(
        SendRichMessage(
            chat_id=chat_id,
            rich_message={"html": html_content},
            reply_markup=reply_markup
        )
    )

async def edit_message_to_rich(chat_id: int, message_id: int, blocks: list[dict], reply_markup=None) -> types.Message:
    html_content = "".join(block_to_html(b) for b in blocks)
    return await bot(
        EditMessageTextRich(
            chat_id=chat_id,
            message_id=message_id,
            rich_message={"html": html_content},
            reply_markup=reply_markup
        )
    )

async def notify_admins_rich(blocks: list[dict], reply_markup=None):
    for chat_id in settings.allowed_ids_list:
        try:
            await send_rich_message(chat_id, blocks, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Failed to notify admin {chat_id} with rich message: {e}")

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
                    alerts_data = []
                    cash_changed = False
                    if last_record:
                        for curr in ['eur', 'usd', 'gbp', 'chf', 'sek']:
                            old_val = float(getattr(last_record, curr) or 0.0)
                            new_val = float(getattr(new_record, curr) or 0.0)

                            if new_val != old_val:
                                cash_changed = True
                                diff = new_val - old_val
                                curr_upper = curr.upper()
                                emoji = EMOJI_MAP.get(curr_upper, "💰")
                                alerts_data.append({
                                    "currency": curr_upper,
                                    "emoji": emoji,
                                    "diff": diff,
                                    "old": old_val,
                                    "new": new_val
                                })

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

                    if alerts_data:
                        rows = [
                            [
                                cell("Currency", is_header=True),
                                cell("Icon", is_header=True),
                                cell("Change", is_header=True, align="right"),
                                cell("Old Balance", is_header=True, align="right"),
                                cell("New Balance", is_header=True, align="right")
                            ]
                        ]
                        for a in alerts_data:
                            rows.append([
                                cell(a["currency"]),
                                cell(a["emoji"]),
                                cell(f"{a['diff']:+.4f}", align="right"),
                                cell(f"{a['old']:.4f}", align="right"),
                                cell(f"{a['new']:.4f}", align="right")
                            ])
                        
                        blocks = [
                            block_heading("💰 Cash Balance Change Alert"),
                            block_table(rows, is_bordered=True, is_striped=True)
                        ]
                        await notify_admins_rich(blocks)

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
            await m.answer("❌ Internal error. Check logs.")


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
            await m.answer("❌ Internal error. Check logs.")


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
            await m.answer("❌ Internal error. Check logs.")



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
            await m.answer("❌ Internal error. Check logs.")


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
            await m.answer("❌ Internal error. Check logs.")


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
            await m.answer("❌ Internal error. Check logs.")


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

            rows = [
                [
                    cell("Time", is_header=True),
                    cell("Side", is_header=True),
                    cell("Qty", is_header=True, align="right"),
                    cell("Symbol", is_header=True),
                    cell("Price", is_header=True, align="right")
                ]
            ]
            for t in trades[:15]:
                time_str = t['time'].split('T')[1].split('.')[0] if 'T' in t['time'] else t['time']
                
                price_val = float(t['price']) if t.get('price') is not None else 0.0
                shares_val = float(t['shares']) if t.get('shares') is not None else 0.0
                shares_str = f"{shares_val:.0f}" if shares_val.is_integer() else f"{shares_val:.2f}"
                
                rows.append([
                    cell(time_str),
                    cell(t['side']),
                    cell(shares_str, align="right"),
                    cell(t['symbol']),
                    cell(f"{price_val:.2f}", align="right")
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


@dp.message(Command("quote", ignore_case=True))
async def cmd_quote(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) < 2:
        await m.answer("ℹ️ Usage: `/quote <SYMBOL>` (e.g. `/quote SPY`)", parse_mode="Markdown")
        return

    symbol = args[1].upper()

    status_msg = None
    msg_id = None
    try:
        status_msg = await send_rich_message(
            m.chat.id,
            [
                block_heading("Market Quote"),
                block_paragraph(f"Fetching snapshot for {symbol}..."),
                block_thinking()
            ]
        )
        if status_msg:
            msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else getattr(status_msg, "message_id", None)
    except Exception as e:
        logger.warning(f"Could not send thinking status message: {e}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/market/snapshot/{symbol}", headers=API_HEADERS)
            r.raise_for_status()
            data = r.json()

            rows = [
                [cell("Metric", is_header=True), cell("Value", is_header=True, align="right")]
            ]
            rows.append([cell("Price"), cell(f"{data['price']:.2f}", align="right")])
            if data.get('bid') is not None:
                rows.append([cell("Bid"), cell(f"{data['bid']:.2f}", align="right")])
            if data.get('ask') is not None:
                rows.append([cell("Ask"), cell(f"{data['ask']:.2f}", align="right")])

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

            blocks = [
                block_heading(f"📈 Quote: {data['symbol']}"),
                block_table(rows, is_bordered=True),
                block_paragraph(f"⏱ {ts_formatted}")
            ]

            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, blocks)
            else:
                await send_rich_message(m.chat.id, blocks)

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /quote: {err_detail}")
            err_msg = f"❌ API Error: {err_detail}"
            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
            else:
                await m.answer(err_msg)
        except Exception as e:
            logger.error(f"Error in /quote: {e}", exc_info=True)
            err_msg = "❌ Internal error. Check logs."
            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
            else:
                await m.answer(err_msg)


@dp.message(Command("contract", ignore_case=True))
async def cmd_contract(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) < 2:
        await m.answer("ℹ️ Usage: `/contract <SYMBOL>`", parse_mode="Markdown")
        return

    symbol = args[1].upper()
    status_msg = None
    msg_id = None
    try:
        status_msg = await send_rich_message(
            m.chat.id,
            [
                block_heading("Contract Search"),
                block_paragraph(f"Searching contract for {symbol}..."),
                block_thinking()
            ]
        )
        if status_msg:
            msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else getattr(status_msg, "message_id", None)
    except Exception as e:
        logger.warning(f"Could not send thinking status message: {e}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{settings.WEB_SERVICE_URL}/contract/search?symbol={symbol}", headers=API_HEADERS)
            r.raise_for_status()
            details = r.json()

            if not details:
                err_msg = f"❌ No contract found for {symbol}."
                if msg_id:
                    await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
                else:
                    await m.answer(err_msg)
                return

            blocks = [
                block_heading(f"📄 Contract Details ({len(details)})")
            ]
            for d in details[:5]:  # Limit to 5 detailed views
                title = f"🔹 {d['symbol']} ({d['secType']}) - {d.get('longName') or 'No Name'}"
                rows = [
                    [cell("Field", is_header=True), cell("Value", is_header=True)]
                ]
                rows.append([cell("conId"), cell(str(d['conId']))])
                rows.append([cell("Exchange"), cell(d['exchange'])])
                if d.get('isin'):
                    rows.append([cell("ISIN"), cell(d['isin'])])
                
                block_t = block_table(rows, is_bordered=True)
                blocks.append(block_details(title, [block_t], is_open=False))

            if len(details) > 5:
                blocks.append(block_paragraph(f"... and {len(details) - 5} more contracts found."))

            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, blocks)
            else:
                await send_rich_message(m.chat.id, blocks)

        except httpx.HTTPStatusError as e:
            err_detail = e.response.text or str(e)
            logger.error(f"HTTP Error in /contract: {err_detail}")
            err_msg = f"❌ API Error: {err_detail}"
            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
            else:
                await m.answer(err_msg)
        except Exception as e:
            logger.error(f"Error in /contract: {e}", exc_info=True)
            err_msg = "❌ Internal error. Check logs."
            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
            else:
                await m.answer(err_msg)


@dp.message(Command("chain", ignore_case=True))
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

    async with httpx.AsyncClient(timeout=30.0) as client:
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
            blocks = [
                block_heading("⚠️ IBKR Flex Token Expiry Alert"),
                block_paragraph(text_concat(
                    "Your token will expire in ", text_bold(f"{days_left} days"), 
                    " (", text_code(expiry_str), ").\nPlease generate a new one to avoid service interruption."
                ))
            ]
            await notify_admins_rich(blocks)
        elif days_left < 0:
            blocks = [
                block_heading("❌ IBKR Flex Token EXPIRED"),
                block_paragraph(text_concat(
                    "Your token expired on ", text_code(expiry_str), 
                    ". Flex reports will fail until a new token is provided."
                ))
            ]
            await notify_admins_rich(blocks)
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
        blocks = [
            block_heading(f"📅 {report_type} Flex Query Report"),
            block_paragraph(f"Period: {date_range_html}")
        ]

        for msg in telegram_msgs:
            if not msg.strip():
                continue
            
            lines = [line.strip() for line in msg.split('\n') if line.strip()]
            if not lines:
                continue
            
            first_line = lines[0]
            # Detect Cash Report
            if "Cash Report" in first_line:
                cash_rows = [[cell("Currency", is_header=True), cell("Ending Cash", is_header=True, align="right")]]
                for line in lines[1:]:
                    try:
                        cur = line.split("<b>")[1].split("</b>")[0]
                        val = line.split("<code>")[1].split("</code>")[0]
                        cash_rows.append([cell(cur), cell(val, align="right")])
                    except Exception:
                        pass
                
                blocks.append(block_details("💰 Cash Summary", [
                    block_table(cash_rows, is_bordered=True, is_striped=True)
                ], is_open=True))
            
            # Detect Dividends
            elif "Dividends" in first_line:
                div_blocks = []
                for line in lines[1:]:
                    div_blocks.append(block_paragraph(html_to_rich(line)))
                blocks.append(block_details("💸 Dividends Received", div_blocks))
                
            # Fallback for any other section
            else:
                section_blocks = []
                for line in lines[1:]:
                    section_blocks.append(block_paragraph(html_to_rich(line)))
                blocks.append(block_details(first_line, section_blocks))

        # Add completion status with Archiving info
        meta_spans = [
            text_bold(f"{report_type} Report Generated\n"),
            text_plain("Date: "), text_code(date_range_html), text_plain("\n"),
            text_plain("Archived: "), text_code(archive_status), text_plain("\n"),
            text_plain("Email: "), text_code(email_status)
        ]
        blocks.append(block_paragraph(text_concat(*meta_spans)))
        
        await notify_admins_rich(blocks)
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

    status_msg = None
    msg_id = None
    try:
        status_msg = await send_rich_message(
            m.chat.id,
            [
                block_heading("Delta Report"),
                block_paragraph("Checking deltas for short positions..."),
                block_thinking()
            ]
        )
        if status_msg:
            msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else getattr(status_msg, "message_id", None)
    except Exception as e:
        logger.warning(f"Could not send thinking status message: {e}")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch positions
            r_pos = await client.get(
                f"{settings.WEB_SERVICE_URL}/account/positions",
                headers=API_HEADERS
            )
            if r_pos.status_code != 200:
                err_msg = f"❌ Failed to fetch positions (HTTP {r_pos.status_code})"
                if msg_id:
                    await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
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
                if msg_id:
                    await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(empty_msg)])
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
                empty_msg = "✅ No short option positions found."
                if msg_id:
                    await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(empty_msg)])
                else:
                    await m.answer(empty_msg)
                return

            # Sort: contracts with delta first (by abs desc), then None deltas at the bottom
            results.sort(key=lambda x: (x['delta'] is None, -abs(x['delta']) if x['delta'] is not None else 0))

            # Build digest message
            high_count = sum(1 for r in results if r['high'])
            no_data_count = sum(1 for r in results if r['delta'] is None)

            # Calculate TV strings
            for r in results:
                r['tv_str'] = ""
                r['low_tv'] = False
                if r['intrinsic'] is not None and r['time_value'] is not None:
                    tv_val = f"{r['time_value']:+.2f}".replace("+0.", "+.").replace("-0.", "-.")
                    r['tv_str'] = f"TV{tv_val}"
                    if r['intrinsic'] > 0 and r['time_value'] <= (0.01 * r['strike']):
                        r['low_tv'] = True

            rows = [
                [
                    cell("", is_header=True),
                    cell("Qty", is_header=True, align="right"),
                    cell("Und.", is_header=True),
                    cell("Opt.", is_header=True),
                    cell("Expiry", is_header=True),
                    cell("Delta", is_header=True, align="right"),
                    cell("TV", is_header=True, align="right"),
                    cell("Age", is_header=True, align="right")
                ]
            ]

            for r in results:
                display_expiry = r['expiry']
                if len(display_expiry) == 8 and display_expiry.isdigit():
                    try:
                        dt = datetime.strptime(display_expiry, "%Y%m%d")
                        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                        month_str = months[dt.month - 1]
                        display_expiry = f"{dt.day:02d}{month_str}{dt.year % 100:02d}"
                    except Exception:
                        pass

                marker = "⚪" if r['delta'] is None else ("🔴" if r['high'] else "🟢")
                delta_val = "—" if r['delta'] is None else f"{abs(r['delta']):.2f}"
                tv_val = r['tv_str']
                if r['low_tv']:
                    tv_val += " ⚠️"

                rows.append([
                    cell(marker),
                    cell(f"{r['qty']:.0f}", align="right"),
                    cell(r['underlying']),
                    cell(r['right_strike']),
                    cell(display_expiry),
                    cell(delta_val, align="right"),
                    cell(tv_val, align="right"),
                    cell(r['age'], align="right")
                ])

            blocks = [
                block_heading(f"📊 Delta Report — {len(results)} Short Position(s)"),
                block_table(rows, is_bordered=True, is_striped=True, caption="🔴 abs(Δ) > {}  🟢 abs(Δ) <= {}  ⚪ no data\n⚠️ low time value (≤ 1% strike)".format(settings.DELTA_ALERT_THRESHOLD, settings.DELTA_ALERT_THRESHOLD))
            ]

            alert_lines = []
            if high_count:
                alert_lines.append(f"⚠️ {high_count} above threshold ({settings.DELTA_ALERT_THRESHOLD})")
            if no_data_count:
                alert_lines.append(f"⚪ {no_data_count} without delta data")

            if alert_lines:
                blocks.insert(1, block_paragraph("\n".join(alert_lines)))

            if msg_id:
                await edit_message_to_rich(m.chat.id, msg_id, blocks)
            else:
                await send_rich_message(m.chat.id, blocks)

    except Exception as e:
        logger.error(f"Error in /delta: {e}", exc_info=True)
        err_msg = "❌ Internal error. Check logs."
        if msg_id:
            try:
                await edit_message_to_rich(m.chat.id, msg_id, [block_paragraph(err_msg)])
            except Exception:
                await m.answer(err_msg)
        else:
            await m.answer(err_msg)


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
