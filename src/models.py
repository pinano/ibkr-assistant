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
    conId: int = 0
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
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None
    implied_vol: Optional[float] = None
    underlying_price: Optional[float] = None
    bid: Optional[float] = None
    bid_size: Optional[int] = None
    ask: Optional[float] = None
    ask_size: Optional[int] = None
    mid: Optional[float] = None
    intrinsic_value: Optional[float] = None
    extrinsic_value: Optional[float] = None
    volume: int = 0
    open_interest: int = 0
    last_price: Optional[float] = None
    last_date: Optional[str] = None
    market_data_status: Optional[str] = None
    quote_status: Optional[str] = None
    greeks_status: Optional[str] = None


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
    commission: Optional[float] = None


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


class OptionQuoteItem(BaseModel):
    conId: int
    symbol: str
    right: str
    strike: float
    bid: Optional[float] = None
    bid_size: Optional[int] = None
    ask: Optional[float] = None
    ask_size: Optional[int] = None
    mid: Optional[float] = None
    last_price: Optional[float] = None
    volume: int = 0
    open_interest: int = 0
    implied_vol: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    intrinsic_value: Optional[float] = None
    extrinsic_value: Optional[float] = None
    last_date: Optional[str] = None
    market_data_status: Optional[str] = None
    quote_status: Optional[str] = None
    greeks_status: Optional[str] = None


class StrikeChainRow(BaseModel):
    strike: float
    moneyness_pct: float = 0.0
    call: Optional[OptionQuoteItem] = None
    put: Optional[OptionQuoteItem] = None


class OptionChainQuotesResponse(BaseModel):
    symbol: str
    underlying_price: float = 0.0
    expiry: str
    exchange: str
    trading_class: str
    multiplier: str
    market_data_status: Optional[str] = None
    strikes: List[StrikeChainRow]


# --- SQLAlchemy Models (Database) ---
Base = declarative_base()


class CashBalance(Base):
    __tablename__ = 'balances'
    id = Column(Integer, primary_key=True)
    date = Column(DateTime, default=datetime.now, index=True)
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





class OptionSnapshot(Base):
    __tablename__ = 'option_snapshots'

    id = Column(Integer, primary_key=True, autoincrement=True)
    conId = Column(Integer, index=True)
    symbol = Column(String(100), unique=True)  # e.g. "RMS 260220 P 1860" — one row per option (UPSERT)
    updated_at = Column(DateTime, default=datetime.now, index=True)

    # Market Data
    last_price = Column(Float, nullable=True)
    delta = Column(Float, nullable=True)
    gamma = Column(Float, nullable=True)
    theta = Column(Float, nullable=True)
    vega = Column(Float, nullable=True)
    implied_vol = Column(Float, nullable=True)
    underlying_price = Column(Float, nullable=True)
    last_trade_date = Column(DateTime, nullable=True)
    volume = Column(Integer, nullable=True)
    open_interest = Column(Integer, nullable=True)
    bid = Column(Float, nullable=True)
    ask = Column(Float, nullable=True)
    bid_size = Column(Integer, nullable=True)
    ask_size = Column(Integer, nullable=True)
    market_data_status = Column(String(20), nullable=True)
    quote_status = Column(String(20), nullable=True)
    greeks_status = Column(String(20), nullable=True)


class MarketCache(Base):
    __tablename__ = 'market_cache'

    symbol = Column(String(20), primary_key=True)  # e.g. "EURUSD", "AAPL"
    price = Column(Float)
    bid = Column(Float)
    ask = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, index=True)
