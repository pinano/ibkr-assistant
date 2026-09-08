import logging

from aiogram import Router, types
from aiogram.filters import Command

from src.config import settings

logger = logging.getLogger(__name__)

router = Router(name="common")


@router.message(Command("help", "start", ignore_case=True))
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
