import logging
from datetime import datetime, timedelta
import httpx
from aiogram import Router, types
from aiogram.filters import Command

from src.config import settings
from src.models import CashBalance
from src.bot.core import SessionLocal
from src.bot.client import API_HEADERS, http_client_ctx
from src.bot.charts import (
    get_now,
    format_nav_date,
    _get_period_stats_and_series,
    _query_nav_series,
    _send_nav_chart,
)

logger = logging.getLogger("ibkr-bot")

router = Router()


@router.message(Command("max", ignore_case=True))
async def cmd_max(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with http_client_ctx() as client:
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

                if curr_val is not None and curr_val > max_val:
                    max_val = curr_val
                    max_date_str = "Now (Real-time)"
                else:
                    max_date_str = max_rec.date.strftime("%Y-%m-%d %H:%M:%S")

                if curr_val is not None:
                    drawdown = ((curr_val - max_val) / max_val * 100) if max_val > 0 else 0
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


@router.message(Command("today", "day", ignore_case=True))
async def cmd_today(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with http_client_ctx() as client:
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

                first_rec, last_db_rec, min_rec, max_rec, series = _get_period_stats_and_series(
                    session, today_start, today_end
                )

                if not first_rec and curr_val is None:
                    await m.answer(f"📭 No records found for {period_name}.")
                    return

                include_year = n_days is not None and n_days > 365
                start_nav = float(first_rec.nav) if first_rec else curr_val
                start_date = format_nav_date(first_rec.date, now, include_year) if first_rec else "Now"

                end_nav = curr_val if curr_val is not None else (float(last_db_rec.nav) if last_db_rec else start_nav)
                end_date = "Now" if curr_val is not None else (format_nav_date(last_db_rec.date, now, include_year) if last_db_rec else start_date)

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

                # Attach chart (series fetched in _get_period_stats_and_series)
                if curr_val is not None:
                    series.append((datetime.now(), curr_val))
                sent = await _send_nav_chart(m, series, period_name, msg)
                if not sent:
                    await m.answer(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in cmd_today: {e}", exc_info=True)
            await m.answer("❌ Internal error. Check logs.")


@router.message(Command("year", ignore_case=True))
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

    async with http_client_ctx() as client:
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

                # Period Start, End, Min, Max, and Series in a single query
                first_rec, last_db_rec, min_rec, max_rec, series = _get_period_stats_and_series(
                    session, year_start, year_end
                )

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
                    end_nav = float(last_db_rec.nav) if last_db_rec else start_nav
                    end_date = format_nav_date(last_db_rec.date, now, include_y) if last_db_rec else start_date

                period_var = ((end_nav - start_nav) / start_nav * 100) if start_nav else 0

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

                # Attach chart (series fetched in _get_period_stats_and_series)
                if curr_val is not None and is_now:
                    series.append((datetime.now(), curr_val))
                sent = await _send_nav_chart(m, series, period_name, msg)
                if not sent:
                    await m.answer(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in cmd_year: {e}", exc_info=True)
            await m.answer("❌ Internal error. Check logs.")


@router.message(Command("month", ignore_case=True))
async def cmd_month(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with http_client_ctx() as client:
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
                    year = now.year - (n_months // 12)
                    month = now.month - (n_months % 12)
                    if month <= 0:
                        month += 12
                        year -= 1
                    try:
                        month_start = now.replace(year=year, month=month).replace(tzinfo=None)
                    except ValueError:
                        import calendar
                        _, last_day = calendar.monthrange(year, month)
                        month_start = now.replace(year=year, month=month, day=last_day).replace(tzinfo=None)

                    period_name = f"Last {n_months} Month{'s' if n_months > 1 else ''}"
                else:
                    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
                    month_name = now.strftime("%B %Y")
                    period_name = month_name

                month_end = now.replace(tzinfo=None)

                first_rec, last_db_rec, min_rec, max_rec, series = _get_period_stats_and_series(
                    session, month_start, month_end
                )

                if not first_rec and curr_val is None:
                    await m.answer(f"📭 No records found for {period_name}.")
                    return

                include_year = (n_months is not None and n_months > 12) or (n_months is None)
                start_nav = float(first_rec.nav) if first_rec else curr_val
                start_date = format_nav_date(first_rec.date, now, include_year) if first_rec else "Now"

                end_nav = curr_val if curr_val is not None else (float(last_db_rec.nav) if last_db_rec else start_nav)
                end_date = "Now" if curr_val is not None else (format_nav_date(last_db_rec.date, now, include_year) if last_db_rec else start_date)

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

                # Attach chart (series fetched in _get_period_stats_and_series)
                if curr_val is not None:
                    series.append((datetime.now(), curr_val))
                sent = await _send_nav_chart(m, series, period_name, msg)
                if not sent:
                    await m.answer(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in cmd_month: {e}", exc_info=True)
            await m.answer("❌ Internal error. Check logs.")


@router.message(Command("week", ignore_case=True))
async def cmd_week(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    async with http_client_ctx() as client:
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

                first_rec, last_db_rec, min_rec, max_rec, series = _get_period_stats_and_series(
                    session, week_start, week_end
                )

                if not first_rec and curr_val is None:
                    await m.answer(f"📭 No records found for {period_name}.")
                    return

                include_year = n_weeks is not None and n_weeks > 52
                start_nav = float(first_rec.nav) if first_rec else curr_val
                start_date = format_nav_date(first_rec.date, now, include_year) if first_rec else "Now"

                end_nav = curr_val if curr_val is not None else (float(last_db_rec.nav) if last_db_rec else start_nav)
                end_date = "Now" if curr_val is not None else (format_nav_date(last_db_rec.date, now, include_year) if last_db_rec else start_date)

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

                # Attach chart (series fetched in _get_period_stats_and_series)
                if curr_val is not None:
                    series.append((datetime.now(), curr_val))
                sent = await _send_nav_chart(m, series, period_name, msg)
                if not sent:
                    await m.answer(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in cmd_week: {e}", exc_info=True)
            await m.answer("❌ Internal error. Check logs.")
