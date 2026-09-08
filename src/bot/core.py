import logging
from aiogram import Bot, Dispatcher
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import settings
from src.models import Base

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ibkr-bot")

# DB Setup
engine = create_engine(settings.DB_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

# Ensure tables exist
Base.metadata.create_all(engine)

# Validate required settings
if not settings.TELEGRAM_TOKEN:
    logger.critical("TELEGRAM_TOKEN is not set. Bot cannot start.")
    raise SystemExit("Missing required environment variable: TELEGRAM_TOKEN")
if not settings.DB_URL:
    logger.critical("DB_URL is not set. Bot cannot start.")
    raise SystemExit("Missing required environment variable: DB_URL")

# Bot Setup
bot = Bot(token=settings.TELEGRAM_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
