import hmac

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from src.config import settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_key(
    header: str = Security(api_key_header),
):
    if header and hmac.compare_digest(header, settings.API_KEY):
        return header
    raise HTTPException(status_code=403, detail="Invalid or missing API Key")
