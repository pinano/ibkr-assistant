import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Force database initialization on import
from src.api.database import get_db  # noqa: F401

from src.api.routes.account import router as account_router
from src.api.routes.options import router as options_router
from src.api.routes.market import router as market_router
from src.api.routes.orders import router as orders_router
from src.api.routes.search import router as search_router

# Logging Setup
logging.basicConfig(level=logging.INFO)

# Rate Limiting
limiter = Limiter(key_func=get_remote_address, default_limits=["30/minute"])

app = FastAPI(title="IBKR API", version="1.0.0")
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Try again later."}
    )


# Mount all route modules
app.include_router(account_router)
app.include_router(options_router)
app.include_router(market_router)
app.include_router(orders_router)
app.include_router(search_router)
