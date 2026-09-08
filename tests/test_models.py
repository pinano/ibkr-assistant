"""
Unit tests for Pydantic model validation (mirrors src/models.py) and
trade deduplication logic from src/api/routes/orders.py.

Models are defined inline here to avoid importing src.models which
depends on sqlalchemy (not installed outside Docker). All field
definitions and validators are identical to the production models.

Models tested:
  - TradeItem: required fields, datetime parsing, orderId int
  - OrderItem: lmtPrice/auxPrice Optional handling
  - OptionGreeks: default zero values
  - MarketSnapshot: optional bid/ask/timestamp
  - ContractDetailsItem: optional isin

Trade dedup logic tested:
  - Duplicate execId entries filtered via seen_ids set
  - Most-recent-first sort by time
  - Symbol fallback (localSymbol or symbol)
"""
import sys
import os
from datetime import datetime
from typing import List, Optional
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Inline Pydantic models — identical to src/models.py but without sqlalchemy
try:
    from pydantic import BaseModel
except ImportError:
    pytest.skip("pydantic not available", allow_module_level=True)


class TradeItem(BaseModel):
    executionId: str
    symbol: str
    time: datetime
    side: str
    shares: float
    price: float
    orderId: int
    commission: Optional[float] = None


class OrderItem(BaseModel):
    orderId: int
    symbol: str
    action: str
    totalQuantity: float
    orderType: str
    lmtPrice: Optional[float] = None
    auxPrice: Optional[float] = None
    status: str


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


class MarketSnapshot(BaseModel):
    symbol: str
    price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    timestamp: Optional[datetime] = None


class ContractDetailsItem(BaseModel):
    conId: int
    symbol: str
    secType: str
    exchange: str
    currency: str
    localSymbol: str
    longName: str
    isin: Optional[str] = None


class PositionItem(BaseModel):
    symbol: str
    qty: float
    cost: float
    secType: str = "STK"
    conId: int = 0
    expiry: Optional[str] = None
    strike: Optional[float] = None
    right: Optional[str] = None
    underlying: Optional[str] = None


class OptionQuoteItem(BaseModel):
    conId: int
    symbol: str
    right: str
    strike: float
    bid: float = 0.0
    bid_size: int = 0
    ask: float = 0.0
    ask_size: int = 0
    mid: float = 0.0
    last_price: float = 0.0
    volume: int = 0
    open_interest: int = 0
    implied_vol: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    intrinsic_value: float = 0.0
    extrinsic_value: float = 0.0
    last_date: Optional[str] = None


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
    strikes: List[StrikeChainRow]


# ---------------------------------------------------------------------------
# TradeItem tests
# ---------------------------------------------------------------------------

class TestTradeItem:
    """Pydantic validation for TradeItem."""

    def _make(self, **overrides):
        defaults = dict(
            executionId="exec-001",
            symbol="AAPL",
            time=datetime(2026, 6, 14, 10, 30, 0),
            side="BOT",
            shares=10.0,
            price=185.50,
            orderId=42,
        )
        defaults.update(overrides)
        return TradeItem(**defaults)

    def test_basic_creation(self):
        t = self._make()
        assert t.symbol == "AAPL"
        assert t.shares == 10.0
        assert t.price == 185.50

    def test_time_parsed_from_string(self):
        t = self._make(time="2026-06-14T10:30:00")
        assert isinstance(t.time, datetime)

    def test_order_id_int(self):
        t = self._make(orderId=99)
        assert t.orderId == 99

    def test_shares_float(self):
        t = self._make(shares=5.5)
        assert t.shares == 5.5

    def test_commission_optional(self):
        t = self._make(commission=1.50)
        assert t.commission == 1.50
        
        t2 = self._make()
        assert t2.commission is None

    def test_missing_symbol_raises(self):
        with pytest.raises(Exception):
            TradeItem(executionId="x", time=datetime.now(),
                      side="SLD", shares=1.0, price=10.0, orderId=1)


# ---------------------------------------------------------------------------
# OrderItem tests
# ---------------------------------------------------------------------------

class TestOrderItem:
    def _make(self, **overrides):
        defaults = dict(
            orderId=1,
            symbol="SPY",
            action="SELL",
            totalQuantity=5.0,
            orderType="LMT",
            status="PreSubmitted",
        )
        defaults.update(overrides)
        return OrderItem(**defaults)

    def test_basic_creation(self):
        o = self._make()
        assert o.symbol == "SPY"
        assert o.action == "SELL"

    def test_lmt_price_optional(self):
        o = self._make(lmtPrice=None)
        assert o.lmtPrice is None

    def test_aux_price_optional(self):
        o = self._make(auxPrice=None)
        assert o.auxPrice is None

    def test_lmt_price_set(self):
        o = self._make(lmtPrice=420.0)
        assert o.lmtPrice == 420.0

    def test_market_order_no_limit(self):
        o = self._make(orderType="MKT", lmtPrice=None, auxPrice=None)
        assert o.orderType == "MKT"
        assert o.lmtPrice is None


# ---------------------------------------------------------------------------
# OptionGreeks tests
# ---------------------------------------------------------------------------

class TestOptionGreeks:
    def test_default_zeros(self):
        g = OptionGreeks(symbol="AAPL 20261219 200 C")
        assert g.delta == 0.0
        assert g.gamma == 0.0
        assert g.theta == 0.0
        assert g.vega == 0.0
        assert g.implied_vol == 0.0
        assert g.underlying_price == 0.0
        assert g.last_price == 0.0

    def test_all_fields(self):
        g = OptionGreeks(
            symbol="SPY 20261219 450 P",
            delta=-0.35, gamma=0.02, theta=-0.05, vega=0.15,
            implied_vol=0.22, underlying_price=460.0,
            volume=1000, open_interest=5000,
            last_price=8.50, last_date="2026-06-14 10:00:00"
        )
        assert g.delta == -0.35
        assert g.last_date == "2026-06-14 10:00:00"

    def test_volume_int(self):
        g = OptionGreeks(symbol="X", volume=42)
        assert g.volume == 42

    def test_last_date_optional(self):
        g = OptionGreeks(symbol="X")
        assert g.last_date is None


# ---------------------------------------------------------------------------
# MarketSnapshot tests
# ---------------------------------------------------------------------------

class TestMarketSnapshot:
    def test_basic(self):
        s = MarketSnapshot(symbol="EURUSD", price=1.0850)
        assert s.price == 1.0850
        assert s.bid is None
        assert s.ask is None

    def test_with_bid_ask(self):
        s = MarketSnapshot(symbol="AAPL", price=185.0, bid=184.9, ask=185.1)
        assert s.bid == 184.9
        assert s.ask == 185.1

    def test_timestamp_optional(self):
        s = MarketSnapshot(symbol="SPY", price=450.0)
        assert s.timestamp is None

    def test_timestamp_parsed(self):
        s = MarketSnapshot(symbol="SPY", price=450.0,
                           timestamp="2026-06-14T10:00:00")
        assert isinstance(s.timestamp, datetime)


# ---------------------------------------------------------------------------
# ContractDetailsItem tests
# ---------------------------------------------------------------------------

class TestContractDetailsItem:
    def test_isin_optional(self):
        c = ContractDetailsItem(
            conId=123, symbol="AAPL", secType="STK",
            exchange="SMART", currency="USD",
            localSymbol="AAPL", longName="Apple Inc."
        )
        assert c.isin is None

    def test_isin_set(self):
        c = ContractDetailsItem(
            conId=123, symbol="BATS", secType="STK",
            exchange="LSE", currency="GBP",
            localSymbol="BATS", longName="British American Tobacco",
            isin="GB0002875804"
        )
        assert c.isin == "GB0002875804"


# ---------------------------------------------------------------------------
# PositionItem tests
# ---------------------------------------------------------------------------

class TestPositionItem:
    def test_stock_defaults(self):
        p = PositionItem(symbol="AAPL", qty=10.0, cost=1800.0)
        assert p.secType == "STK"
        assert p.expiry is None
        assert p.strike is None
        assert p.right is None

    def test_option_fields(self):
        p = PositionItem(
            symbol="AAPL  261219P00200000",
            qty=-5.0, cost=500.0,
            secType="OPT",
            expiry="20261219",
            strike=200.0,
            right="P",
            underlying="AAPL"
        )
        assert p.right == "P"
        assert p.underlying == "AAPL"


# ---------------------------------------------------------------------------
# Trade deduplication logic
# ---------------------------------------------------------------------------

class TestTradeDeduplication:
    """
    Tests for the execId-based deduplication in get_trades().
    We replicate the exact loop logic from orders.py as a pure function.
    """

    class FakeFill:
        def __init__(self, exec_id, symbol, time_str, side, shares, price, order_id,
                     local_symbol=None, commission=None):
            self.execution = MagicMock()
            self.execution.execId = exec_id
            self.execution.side = side
            self.execution.shares = shares
            self.execution.price = price
            self.execution.orderId = order_id
            self.contract = MagicMock()
            self.contract.localSymbol = local_symbol or ""
            self.contract.symbol = symbol
            self.time = datetime.fromisoformat(time_str)
            if commission is not None:
                self.commissionReport = MagicMock()
                self.commissionReport.commission = commission
                self.commissionReport.execId = exec_id
            else:
                self.commissionReport = MagicMock()
                self.commissionReport.commission = 0.0
                self.commissionReport.execId = ""

    def _dedup(self, fills):
        """Mirror of dedup logic in get_trades()."""
        items = []
        seen_ids = set()
        for f in fills:
            eid = f.execution.execId
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            
            commission_val = None
            if getattr(f, 'commissionReport', None) is not None and f.commissionReport.execId == f.execution.execId:
                commission_val = float(f.commissionReport.commission)
            
            items.append(dict(
                executionId=eid,
                symbol=f.contract.localSymbol or f.contract.symbol,
                time=f.time,
                side=f.execution.side,
                shares=float(f.execution.shares),
                price=f.execution.price,
                orderId=f.execution.orderId,
                commission=commission_val,
            ))
        items.sort(key=lambda x: x['time'], reverse=True)
        return items

    def test_unique_fills_all_kept(self):
        from unittest.mock import MagicMock
        f1 = self.FakeFill("e1", "AAPL", "2026-06-14T10:00:00", "BOT", 10, 185.0, 1)
        f2 = self.FakeFill("e2", "SPY",  "2026-06-14T11:00:00", "SLD", 5,  450.0, 2)
        result = self._dedup([f1, f2])
        assert len(result) == 2

    def test_duplicate_execid_filtered(self):
        from unittest.mock import MagicMock
        f1 = self.FakeFill("e1", "AAPL", "2026-06-14T10:00:00", "BOT", 10, 185.0, 1)
        f2 = self.FakeFill("e1", "AAPL", "2026-06-14T10:00:00", "BOT", 10, 185.0, 1)
        result = self._dedup([f1, f2])
        assert len(result) == 1

    def test_sorted_most_recent_first(self):
        from unittest.mock import MagicMock
        f1 = self.FakeFill("e1", "AAPL", "2026-06-14T09:00:00", "BOT", 5, 185.0, 1)
        f2 = self.FakeFill("e2", "SPY",  "2026-06-14T11:00:00", "SLD", 3, 450.0, 2)
        result = self._dedup([f1, f2])
        assert result[0]['executionId'] == "e2"  # most recent first

    def test_local_symbol_preferred(self):
        from unittest.mock import MagicMock
        f = self.FakeFill("e1", "AAPL", "2026-06-14T10:00:00", "BOT", 10, 185.0, 1,
                          local_symbol="AAPL  261219C00200000")
        result = self._dedup([f])
        assert result[0]['symbol'] == "AAPL  261219C00200000"

    def test_symbol_fallback_when_no_local(self):
        from unittest.mock import MagicMock
        f = self.FakeFill("e1", "AAPL", "2026-06-14T10:00:00", "BOT", 10, 185.0, 1,
                          local_symbol="")
        result = self._dedup([f])
        assert result[0]['symbol'] == "AAPL"

    def test_empty_fills_returns_empty(self):
        result = self._dedup([])
        assert result == []

    def test_commission_extracted_when_present(self):
        f = self.FakeFill("e1", "AAPL", "2026-06-14T10:00:00", "BOT", 10, 185.0, 1, commission=1.50)
        result = self._dedup([f])
        assert result[0]['commission'] == 1.50

    def test_commission_none_when_missing(self):
        f = self.FakeFill("e1", "AAPL", "2026-06-14T10:00:00", "BOT", 10, 185.0, 1)
        result = self._dedup([f])
        assert result[0]['commission'] is None


# ---------------------------------------------------------------------------
# OptionChainQuotesResponse tests
# ---------------------------------------------------------------------------

class TestOptionChainQuoteModels:
    def test_option_quote_item_defaults(self):
        item = OptionQuoteItem(conId=12345, symbol="P HMI  20260220 1900 M", right="P", strike=1900.0)
        assert item.conId == 12345
        assert item.symbol == "P HMI  20260220 1900 M"
        assert item.right == "P"
        assert item.strike == 1900.0
        assert item.bid == 0.0
        assert item.ask == 0.0
        assert item.mid == 0.0
        assert item.volume == 0
        assert item.open_interest == 0
        assert item.delta == 0.0
        assert item.intrinsic_value == 0.0
        assert item.extrinsic_value == 0.0
        assert item.last_date is None

    def test_option_quote_item_full(self):
        item = OptionQuoteItem(
            conId=999,
            symbol="C HMI  20260320 2000 M",
            right="C",
            strike=2000.0,
            bid=45.5,
            bid_size=10,
            ask=48.0,
            ask_size=15,
            mid=46.75,
            last_price=47.0,
            volume=120,
            open_interest=540,
            implied_vol=0.245,
            delta=0.48,
            gamma=0.003,
            theta=-0.85,
            vega=1.2,
            intrinsic_value=10.0,
            extrinsic_value=37.0,
            last_date="2026-03-08 15:30:00"
        )
        assert item.bid == 45.5
        assert item.ask == 48.0
        assert item.mid == 46.75
        assert item.volume == 120
        assert item.open_interest == 540
        assert item.delta == 0.48
        assert item.last_date == "2026-03-08 15:30:00"

    def test_strike_chain_row_partial(self):
        row = StrikeChainRow(strike=1900.0, moneyness_pct=-2.5)
        assert row.strike == 1900.0
        assert row.moneyness_pct == -2.5
        assert row.call is None
        assert row.put is None

    def test_strike_chain_row_with_call_and_put(self):
        call_item = OptionQuoteItem(conId=1, symbol="C HMI 1900", right="C", strike=1900.0, bid=50.0, ask=52.0)
        put_item = OptionQuoteItem(conId=2, symbol="P HMI 1900", right="P", strike=1900.0, bid=30.0, ask=32.0)
        row = StrikeChainRow(strike=1900.0, moneyness_pct=0.0, call=call_item, put=put_item)
        assert row.call.bid == 50.0
        assert row.put.bid == 30.0

    def test_option_chain_quotes_response(self):
        call_item = OptionQuoteItem(conId=101, symbol="C HMI 1900", right="C", strike=1900.0, bid=50.0, ask=52.0)
        row = StrikeChainRow(strike=1900.0, moneyness_pct=0.0, call=call_item, put=None)
        resp = OptionChainQuotesResponse(
            symbol="RMS",
            underlying_price=1900.0,
            expiry="20260320",
            exchange="DTB",
            trading_class="HMI",
            multiplier="100",
            strikes=[row]
        )
        assert resp.symbol == "RMS"
        assert resp.underlying_price == 1900.0
        assert resp.exchange == "DTB"
        assert resp.trading_class == "HMI"
        assert len(resp.strikes) == 1
        assert resp.strikes[0].strike == 1900.0




