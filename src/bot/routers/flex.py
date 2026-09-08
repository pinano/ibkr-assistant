import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Router, types
from aiogram.filters import Command

from src.bot.core import scheduler
from src.bot.rich import (
    block_details,
    block_heading,
    block_paragraph,
    block_table,
    cell,
    html_to_rich,
    notify_admins,
    notify_admins_rich,
    text_bold,
    text_code,
    text_concat,
    text_plain,
)
from src.config import settings
from src.flex import FlexReporter

logger = logging.getLogger(__name__)

router = Router(name="flex")


async def scheduled_flex_report(
    query_id=None, report_type="Daily", retry_count=0, local_date=None
):
    attempt_str = f" (Attempt {retry_count + 1})" if not local_date else f" (Local: {local_date})"
    logger.info(f"Running scheduled {report_type} Flex Query Report{attempt_str}...")
    try:
        # Run blocking report generation in a thread
        # Returns (html, date_range_html, date_range_subject, telegram_msgs, archive_status)
        html, date_range_html, date_range_subject, telegram_msgs, archive_status = await asyncio.to_thread(
            FlexReporter.run_report,
            query_id=query_id,
            local_date=local_date,
            report_type=report_type,
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
                        "date",
                        run_date=next_run,
                        args=[query_id, report_type, retry_count + 1],
                        id=f"flex_retry_{report_type}",
                        replace_existing=True,
                        max_instances=1,
                    )
                    logger.info(
                        f"Rescheduled {report_type} Flex Report retry #{retry_count + 1} for {next_run}"
                    )
                    await notify_admins(
                        f"⚠️ <b>{report_type} Flex Query Failed:</b> {html}\n"
                        f"It will be retried at {next_run.strftime('%Y-%m-%d %H:%M:%S')} (Retry #{retry_count + 1}/10).",
                        parse_mode="HTML",
                    )
                    return
                else:
                    logger.error(
                        f"{report_type} Flex Query failed after 10 retries: {html}"
                    )
                    await notify_admins(
                        f"⚠️ {report_type} Flex Query Report Error (Failed after 10 attempts): {html}"
                    )
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
            block_paragraph(f"Period: {date_range_html}"),
        ]

        for msg in telegram_msgs:
            if isinstance(msg, dict):
                if msg.get("type") == "dividends":
                    div_headers = [
                        cell("Ticker", is_header=True),
                        cell("Shares", is_header=True, align="right"),
                        cell("Div/Share", is_header=True, align="right"),
                        cell("Total", is_header=True, align="right"),
                        cell("Concept", is_header=True),
                    ]
                    div_rows = [div_headers]
                    for item in msg["data"]:
                        div_rows.append([
                            cell(item["symbol"]),
                            cell(item["qty"], align="right"),
                            cell(item["rate"], align="right"),
                            cell(item["amount"], align="right"),
                            cell(item["concept"]),
                        ])
                    blocks.append(
                        block_details(
                            msg["title"],
                            [block_table(div_rows, is_bordered=True, is_striped=True)],
                            is_open=True,
                        )
                    )
                continue

            if not msg.strip():
                continue

            lines = [line.strip() for line in msg.split("\n") if line.strip()]
            if not lines:
                continue

            first_line = lines[0]
            # Detect Cash Report
            if "Cash Report" in first_line:
                cash_rows = [
                    [
                        cell("Currency", is_header=True),
                        cell("Ending Cash", is_header=True, align="right"),
                    ]
                ]
                for line in lines[1:]:
                    try:
                        cur = line.split("<b>")[1].split("</b>")[0]
                        val = line.split("<code>")[1].split("</code>")[0]
                        cash_rows.append([cell(cur), cell(val, align="right")])
                    except Exception:
                        pass

                blocks.append(
                    block_details(
                        "💰 Cash Summary",
                        [block_table(cash_rows, is_bordered=True, is_striped=True)],
                        is_open=True,
                    )
                )

            # Detect Dividends (fallback)
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
            text_plain("Date: "),
            text_code(date_range_html),
            text_plain("\n"),
            text_plain("Archived: "),
            text_code(archive_status),
            text_plain("\n"),
            text_plain("Email: "),
            text_code(email_status),
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
                    "date",
                    run_date=next_run,
                    args=[query_id, report_type, retry_count + 1],
                    id=f"flex_retry_{report_type}",
                    replace_existing=True,
                    max_instances=1,
                )
                logger.info(
                    f"Rescheduled {report_type} Flex Report retry #{retry_count + 1} (due to error) for {next_run}"
                )
                await notify_admins(
                    f"⚠️ <b>{report_type} Flex Query Error:</b> {e}\n"
                    f"It will be retried at {next_run.strftime('%Y-%m-%d %H:%M:%S')} (Retry #{retry_count + 1}/10).",
                    parse_mode="HTML",
                )
            else:
                await notify_admins(
                    f"⚠️ {report_type} Flex Query System Error (Failed after 10 attempts): {e}"
                )
        else:
            await notify_admins(f"❌ Local {report_type} Flex Query Exception: {e}")


@router.message(Command("flex", ignore_case=True))
async def cmd_flex(m: types.Message):
    if m.from_user.id not in settings.allowed_ids_list:
        return

    args = m.text.split()
    if len(args) > 1:
        arg = args[1].lower().strip()
        if arg == "monthly":
            await m.answer("Generating Monthly Flex Query Report... ⏳")
            await scheduled_flex_report(
                query_id=settings.IB_FLEX_MONTHLY_QUERY_ID, report_type="Monthly"
            )
            return

        local_date = arg
        # Basic validation
        if not (len(local_date) == 8 and local_date.isdigit()):
            await m.answer(
                "❌ Invalid format. Use /flex YYYYMMDD (e.g. /flex 20251229) or /flex monthly"
            )
            return
        await m.answer(f"Processing local report for {local_date}.xml ... ⏳")
        await scheduled_flex_report(local_date=local_date)
    else:
        await m.answer("Generating Daily Flex Query Report... ⏳")
        await scheduled_flex_report(
            query_id=settings.IB_FLEX_DAILY_QUERY_ID, report_type="Daily"
        )
