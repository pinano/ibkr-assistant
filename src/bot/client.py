import logging
import httpx
from contextlib import asynccontextmanager
from typing import Optional
from aiogram import BaseMiddleware

from src.config import settings

logger = logging.getLogger("ibkr-bot")

# Common Constants
API_HEADERS = {"X-API-Key": settings.API_KEY}

EMOJI_MAP = {
    "EUR": "💶",
    "USD": "💵",
    "GBP": "💷",
    "CHF": "🇨🇭",
    "SEK": "🇸🇪"
}


class AuthMiddleware(BaseMiddleware):
    """Rejects any message or callback from unauthorized Telegram users."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.id not in settings.allowed_ids_list:
            logger.warning(
                f"Unauthorized Telegram access dropped: user_id={getattr(user, 'id', None)} "
                f"username=@{getattr(user, 'username', 'unknown')}"
            )
            return None
        return await handler(event, data)


# ---------------------------------------------------------------------------
# Reusable HTTP client with connection pooling
# ---------------------------------------------------------------------------
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    """Return a shared httpx.AsyncClient with connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


@asynccontextmanager
async def http_client_ctx():
    """Context manager yielding the shared pooled httpx.AsyncClient without closing it."""
    yield get_http_client()


async def close_http_client():
    """Close the shared httpx.AsyncClient connection pool on shutdown."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
