import asyncio
import logging
from fastapi import HTTPException
from ib_async import IB
from src.config import settings

logger = logging.getLogger("ibkr-api")

ib = IB()
_ib_lock = asyncio.Lock()  # Protects concurrent reconnection attempts


def _on_disconnected():
    """Called when the IB Gateway connection drops unexpectedly.
    Logs the event so that the next get_ib() call triggers a reconnect."""
    logger.warning("IBKR Gateway disconnected. Next request will trigger reconnection.")


ib.disconnectedEvent += _on_disconnected


async def get_ib():
    if ib.isConnected():
        return ib
    async with _ib_lock:
        # Double-check after acquiring the lock (another coroutine may have reconnected)
        if ib.isConnected():
            return ib
        retries = 3
        delay = 2
        for i in range(retries):
            try:
                logger.info(f"Connecting to IBKR Gateway (Attempt {i + 1}/{retries})...")
                await ib.connectAsync(
                    settings.IB_HOST,
                    settings.IB_PORT,
                    clientId=settings.IB_CLIENT_ID
                )
                logger.info("Connected to IBKR Gateway")
                return ib
            except Exception as e:
                logger.warning(f"Connection attempt {i + 1} failed: {e}")
                if i < retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error("All connection attempts failed.")
                    raise HTTPException(
                        status_code=503, detail="Could not connect to IBKR")
    return ib
