import asyncio
import logging

from src.bot.client import AuthMiddleware, close_http_client
from src.bot.core import SessionLocal, bot, dp, scheduler
from src.bot.routers import get_all_routers
from src.bot.scheduler import check_token_expiry, setup_scheduler
from src.config import settings
from src.monitor import Monitor

logger = logging.getLogger("ibkr-bot")


async def main():
    monitor = Monitor(SessionLocal)

    # Register authentication middleware
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    # Register all routers
    for router in get_all_routers():
        dp.include_router(router)

    # Setup and start background scheduler
    setup_scheduler(scheduler, monitor)
    scheduler.start()

    # Initial checks (no forced DB inserts on startup to respect intervals)
    await check_token_expiry()

    try:
        await dp.start_polling(bot)
    finally:
        await close_http_client()


if __name__ == "__main__":
    if not settings.TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not set")
        exit(1)
    asyncio.run(main())
