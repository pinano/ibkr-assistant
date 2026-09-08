import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.bot.charts import get_now
from src.bot.client import API_HEADERS, EMOJI_MAP, http_client_ctx
from src.bot.core import SessionLocal
from src.bot.rich import (
    block_heading,
    block_paragraph,
    notify_admins,
    notify_admins_rich,
    text_bold,
    text_code,
    text_concat,
)
from src.bot.routers.flex import scheduled_flex_report
from src.config import settings
from src.models import CashBalance
from src.monitor import Monitor

logger = logging.getLogger(__name__)


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
        async with http_client_ctx() as client:
            # 1. Fetch Summary (includes NetLiquidation, TotalCashValue, and EUR/USD/GBP/CHF/SEK)
            r_sum = await client.get(
                f"{settings.WEB_SERVICE_URL}/account/summary",
                headers=API_HEADERS,
            )
            if r_sum.status_code != 200:
                logger.warning(f"Failed to fetch summary: {r_sum.status_code}")
                return

            summary = r_sum.json()

            # 2. DB Operations
            try:
                with SessionLocal() as session:
                    # Get previous cash balance for change detection
                    last_record = (
                        session.query(CashBalance)
                        .order_by(CashBalance.date.desc())
                        .first()
                    )

                    # Create new record (but don't add to session yet)
                    new_record = CashBalance(
                        nav=summary["NetLiquidation"],
                        stock=summary["StockMarketValue"],
                        pnl=summary["UnrealizedPnL"],
                        base=summary["TotalCashValue"],
                        eur=summary.get("EUR", 0.0),
                        usd=summary.get("USD", 0.0),
                        gbp=summary.get("GBP", 0.0),
                        chf=summary.get("CHF", 0.0),
                        sek=summary.get("SEK", 0.0),
                        cushion=summary["Cushion"],
                        buyingPower=summary["BuyingPower"],
                        excessLiq=summary["ExcessLiquidity"],
                        maintMargin=summary["FullMaintMargin"],
                    )

                    # Check for alerts using the previous record
                    alerts = []
                    cash_changed = False
                    if last_record:
                        for curr in ["eur", "usd", "gbp", "chf", "sek"]:
                            old_val = float(getattr(last_record, curr) or 0.0)
                            new_val = float(getattr(new_record, curr) or 0.0)

                            if new_val != old_val:
                                cash_changed = True
                                diff = new_val - old_val
                                sign = "+" if diff > 0 else "-"
                                abs_diff = abs(diff)
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
                                "DB record inserted due to cash balance change"
                            )
                        else:
                            logger.info("DB record inserted (periodic snapshot)")
                    else:
                        logger.debug("No DB insert - no cash changes detected")

                    if alerts:
                        await notify_admins(
                            "💰 <b>Cash balance change:</b>\n" + "\n".join(alerts),
                            parse_mode="HTML",
                        )

            except Exception as e:
                logger.error(f"DB/Logic Error: {e}")

    except Exception as e:
        logger.error(f"Monitoring Job Error: {e}")


async def check_token_expiry():
    """Verify IBKR Flex Token expiry date and alert if expired or expiring soon."""
    if not settings.IB_FLEX_TOKEN_EXPIRY:
        return

    try:
        # Expected format: "2026-02-18, 05:34:27 EST"
        expiry_str = settings.IB_FLEX_TOKEN_EXPIRY.split(",")[0].strip()
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d")
        days_left = (expiry_date - get_now().replace(tzinfo=None)).days

        if 0 <= days_left <= 10:
            blocks = [
                block_heading("⚠️ IBKR Flex Token Expiry Alert"),
                block_paragraph(
                    text_concat(
                        "Your token will expire in ",
                        text_bold(f"{days_left} days"),
                        " (",
                        text_code(expiry_str),
                        ").\nPlease generate a new one to avoid service interruption.",
                    )
                ),
            ]
            await notify_admins_rich(blocks)
        elif days_left < 0:
            blocks = [
                block_heading("❌ IBKR Flex Token EXPIRED"),
                block_paragraph(
                    text_concat(
                        "Your token expired on ",
                        text_code(expiry_str),
                        ". Flex reports will fail until a new token is provided.",
                    )
                ),
            ]
            await notify_admins_rich(blocks)
    except Exception as e:
        logger.error(f"Error checking token expiry: {e}")


def setup_scheduler(scheduler: AsyncIOScheduler, monitor: Monitor):
    """Configure all recurring cron and interval jobs on the scheduler."""
    # 1. Schedule: Tue,Wed,Thu,Fri,Sat for Flex Query Reports
    # Parse configured time (default 07:30)
    try:
        sh, sm = map(int, settings.IB_FLEX_SCHEDULE_TIME.split(":"))
    except ValueError:
        logger.error(
            f"Invalid IB_FLEX_SCHEDULE_TIME format: {settings.IB_FLEX_SCHEDULE_TIME}. Defaulting to 07:30"
        )
        sh, sm = 7, 30

    # Daily Flex Query: Tue,Wed,Thu,Fri,Sat
    scheduler.add_job(
        scheduled_flex_report,
        "cron",
        day_of_week="tue,wed,thu,fri,sat",
        hour=sh,
        minute=sm,
        args=[settings.IB_FLEX_DAILY_QUERY_ID, "Daily"],
    )

    # Monthly Flex Query: 1st of each month at 12:00
    scheduler.add_job(
        scheduled_flex_report,
        "cron",
        day="1",
        hour=12,
        minute=0,
        args=[settings.IB_FLEX_MONTHLY_QUERY_ID, "Monthly"],
    )

    # 2. Schedule: Daily check for token expiry at 09:00
    scheduler.add_job(check_token_expiry, "cron", hour=9, minute=0)

    # Calculate intervals in minutes
    check_interval_min = max(1, settings.CASH_DIFFERENCE_CHECK_INTERVAL // 60)
    db_insert_interval_min = max(1, settings.DB_INSERT_INTERVAL // 60)

    # 3. Schedule: Periodic DB snapshots (Fixed time, e.g. :00, :30)
    # We want these to happen EXACTLY at the interval marks
    snap_mins = (
        set(range(0, 60, db_insert_interval_min))
        if db_insert_interval_min < 60
        else {0}
    )
    snap_cron = ",".join(map(str, sorted(snap_mins)))

    scheduler.add_job(
        check_and_archive,
        "cron",
        day_of_week="mon-fri",
        hour="7-23",
        minute=snap_cron,
        args=[True],  # force_insert=True
        max_instances=1,
        id="periodic_db_snapshot",
    )

    # 4. Schedule: Cash change detection
    # Run at check intervals BUT skip minutes where a snapshot (forced insert)
    # creates a redundancy
    check_mins = (
        set(range(0, 60, check_interval_min))
        if check_interval_min < 60
        else {0}
    )
    effective_check_mins = check_mins - snap_mins

    check_cron = None
    if effective_check_mins:
        check_cron = ",".join(map(str, sorted(effective_check_mins)))
        scheduler.add_job(
            check_and_archive,  # force_insert defaults to False
            "cron",
            day_of_week="mon-fri",
            hour="7-23",
            minute=check_cron,
            max_instances=1,
            id="cash_change_check",
        )

    # 5. Schedule: Weekend cash control points (Sat, Sun at 12:00)
    scheduler.add_job(
        check_and_archive,
        "cron",
        day_of_week="sat,sun",
        hour=12,
        minute=0,
        args=[True],  # force_insert=True
        max_instances=1,
        id="weekend_cash_control",
    )

    # 6. Schedule: Alert Monitoring
    DELTA_ALERT_TIMES = [
        t.strip() for t in settings.DELTA_ALERT_TIMES.split(",") if t.strip()
    ]
    for idx, time_str in enumerate(DELTA_ALERT_TIMES):
        try:
            ah, am = map(int, time_str.split(":"))
            scheduler.add_job(
                monitor.check_alerts,
                "cron",
                day_of_week="mon-fri",
                hour=ah,
                minute=am,
                id=f"alert_monitoring_{idx}",
            )
        except ValueError:
            logger.error(f"Invalid alert time format: {time_str}")

    # 7. Schedule: Greeks Cache Refresh (European Hours)
    # Mon-Fri 09:00-18:00 every 15 mins
    scheduler.add_job(
        monitor.refresh_greeks_cache,
        "cron",
        day_of_week="mon-fri",
        hour="9-18",
        minute="*/15",
        id="greeks_cache_refresh",
    )

    # 8. Schedule: DB Cleanup (Daily at 04:00)
    scheduler.add_job(
        monitor.prune_old_snapshots,
        "cron",
        hour=4,
        minute=0,
        id="db_cleanup_snapshots",
    )
    scheduler.add_job(
        monitor.prune_old_market_cache,
        "cron",
        hour=4,
        minute=5,
        id="db_cleanup_market_cache",
    )

    check_cron_log = (
        check_cron if effective_check_mins else "None (covered by snapshots)"
    )
    logger.info(
        f"Scheduler configured: Snapshots at mins={snap_cron}, Checks at mins={check_cron_log}, Weekend at 12:00"
    )
