from aiogram import Router

from src.bot.routers.account import router as account_router
from src.bot.routers.common import router as common_router
from src.bot.routers.flex import router as flex_router
from src.bot.routers.market import router as market_router
from src.bot.routers.orders import router as orders_router
from src.bot.routers.periods import router as periods_router


def get_all_routers() -> list[Router]:
    """Return all aiogram routers for the bot in order of registration."""
    return [
        account_router,
        market_router,
        periods_router,
        orders_router,
        flex_router,
        common_router,
    ]
