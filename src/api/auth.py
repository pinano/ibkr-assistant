import hmac
from typing import Optional

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader, APIKeyQuery
from src.config import settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
api_key_query_token = APIKeyQuery(name="token", auto_error=False)
api_key_query_alt = APIKeyQuery(name="api_key", auto_error=False)


async def verify_key(
    header: Optional[str] = Security(api_key_header),
    query_token: Optional[str] = Security(api_key_query_token),
    query_key: Optional[str] = Security(api_key_query_alt),
):
    provided = header or query_token or query_key
    if provided and hmac.compare_digest(provided, settings.API_KEY):
        return provided
    raise HTTPException(status_code=403, detail="Invalid or missing API Key")
