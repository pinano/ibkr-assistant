# Database Setup - Robust resolution for Docker environments
import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.models import Base

logger = logging.getLogger("ibkr-api")

db_url = os.environ.get("DB_URL") or settings.DB_URL

if not db_url:
    db_user = os.environ.get("DB_USER")
    db_pass = os.environ.get("DB_PASS")
    db_name = os.environ.get("DB_NAME", "ibkr")
    if db_user and db_pass:
        db_url = f"mysql+pymysql://{db_user}:{db_pass}@{settings.PROJECT_NAME}-db/{db_name}"
        logger.info("Constructed DB_URL from individual components")

if not db_url:
    logger.error("DB_URL is not set and could not be reconstructed.")

engine = create_engine(db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Auto-create tables if they don't exist
Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
