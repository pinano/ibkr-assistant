import asyncio
import io
import logging
from collections import defaultdict
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # headless backend — no display required
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from aiogram import types
from aiogram.types import BufferedInputFile

from src.config import settings
from src.models import CashBalance

logger = logging.getLogger("ibkr-bot")

MAX_CHART_POINTS = 150  # Target number of points after downsampling


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


def _get_period_stats_and_series(
    session, start: datetime, end: datetime
) -> tuple[
    Optional[CashBalance],
    Optional[CashBalance],
    Optional[CashBalance],
    Optional[CashBalance],
    list[tuple[datetime, float]],
]:
    """
    Fetch all CashBalance records in [start, end] in a single SQL query ordered by date asc.
    Consolidates first, last, min, max, and series lookups to eliminate redundant DB queries.
    """
    records = (
        session.query(CashBalance)
        .filter(CashBalance.date >= start, CashBalance.date <= end)
        .order_by(CashBalance.date.asc())
        .all()
    )
    if not records:
        return None, None, None, None, []

    first_rec = records[0]
    last_rec = records[-1]
    min_rec = min(records, key=lambda r: float(r.nav) if r.nav is not None else float("inf"))
    max_rec = max(records, key=lambda r: float(r.nav) if r.nav is not None else float("-inf"))
    series = [(r.date, float(r.nav)) for r in records if r.nav is not None]
    return first_rec, last_rec, min_rec, max_rec, series


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
