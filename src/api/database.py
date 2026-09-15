# Database Setup - Robust resolution for Docker environments
import os
import logging
from urllib.parse import quote_plus
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
        db_url = f"mysql+pymysql://{quote_plus(db_user)}:{quote_plus(db_pass)}@{settings.PROJECT_NAME}-db/{quote_plus(db_name)}"
        logger.info("Constructed DB_URL from individual components")

if not db_url:
    logger.error("DB_URL is not set and could not be reconstructed.")

engine = create_engine(db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Auto-create tables if they don't exist
Base.metadata.create_all(bind=engine)

# Ensure new columns exist in option_snapshots table for existing DBs
try:
    from sqlalchemy import text
    with engine.connect() as conn:
        for col, col_type in [
            ("bid", "FLOAT NULL"),
            ("ask", "FLOAT NULL"),
            ("bid_size", "INT NULL"),
            ("ask_size", "INT NULL"),
            ("market_data_status", "VARCHAR(20) NULL"),
            ("quote_status", "VARCHAR(20) NULL"),
            ("greeks_status", "VARCHAR(20) NULL"),
        ]:
            try:
                conn.execute(text(f"ALTER TABLE option_snapshots ADD COLUMN IF NOT EXISTS {col} {col_type}"))
                conn.commit()
            except Exception:
                pass
except Exception as e:
    logger.warning(f"Could not verify/migrate option_snapshots schema: {e}")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
