from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
from sqlalchemy import Column, Float, String, DateTime, Integer, Numeric
from sqlalchemy.orm import declarative_base

# --- Pydantic Models (API Responses) ---

class AccountSummary(BaseModel):
    NetLiquidation: float
    AvailableMargin: float
    Cushion: float
    Currency: str
    BuyingPower: float
    ExcessLiquidity: float
    FullMaintMargin: float
    EquityWithLoanValue: float
    TotalCashValue: float
    UnrealizedPnL: float = 0.0
    RealizedPnL: float = 0.0
    DailyPnL: float = 0.0
    DailyRealizedPnL: float = 0.0
    StockMarketValue: float = 0.0
    # Currency breakdown
    EUR: float = 0.0
    USD: float = 0.0
    GBP: float = 0.0
    CHF: float = 0.0
    SEK: float = 0.0


class PositionItem(BaseModel):
    symbol: str  # localSymbol (OSI for options)
    qty: float
    cost: float
    secType: str = "STK"
    expiry: Optional[str] = None
    strike: Optional[float] = None
    right: Optional[str] = None
    underlying: Optional[str] = None

class CurrencyItem(BaseModel):
    currency: str
    amount: float

class OptionChainItem(BaseModel):
    exchange: str
    underlyingConId: int
    tradingClass: str
    multiplier: str
    expirations: List[str]  # List of expiration dates in YYYYMMDD format
    strikes: List[float]    # List of available strike prices


class OptionGreeks(BaseModel):

    symbol: str
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0
    implied_vol: float = 0.0
    underlying_price: float = 0.0
    volume: int = 0
    open_interest: int = 0
    last_price: float = 0.0
    last_date: Optional[str] = None


class OrderItem(BaseModel):
    orderId: int
    symbol: str
    action: str
    totalQuantity: float
    orderType: str
    lmtPrice: Optional[float] = None
    auxPrice: Optional[float] = None
    status: str

class TradeItem(BaseModel):
    executionId: str
    symbol: str
    time: datetime
    side: str
    shares: float
    price: float
    orderId: int

class ContractDetailsItem(BaseModel):
    conId: int
    symbol: str
    secType: str
    exchange: str
    currency: str
    localSymbol: str
    longName: str
    isin: Optional[str] = None


class MarketSnapshot(BaseModel):
    symbol: str
    price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    timestamp: Optional[datetime] = None




# --- SQLAlchemy Models (Database) ---

Base = declarative_base()

class CashBalance(Base):
    __tablename__ = 'balances'
    id = Column(Integer, primary_key=True)
    date = Column(DateTime, default=datetime.now)
    nav = Column(Numeric(18, 4))
    stock = Column(Numeric(18, 4))
    pnl = Column(Numeric(18, 4), default=0.0)
    base = Column(Numeric(18, 4), default=0.0)
    eur = Column(Numeric(18, 4), default=0.0)
    usd = Column(Numeric(18, 4), default=0.0)
    gbp = Column(Numeric(18, 4), default=0.0)
    chf = Column(Numeric(18, 4), default=0.0)
    sek = Column(Numeric(18, 4), default=0.0)
    cushion = Column(Float)
    buyingPower = Column(Numeric(18, 4))
    excessLiq = Column(Numeric(18, 4))
    maintMargin = Column(Numeric(18, 4))


