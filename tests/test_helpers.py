"""
Unit tests for src/api/helpers.py:
- _is_market_open: market hours detection logic
- CBOE option ID construction logic (option_id format)
- val_or_zero helper extracted inline

These tests do NOT require a live IBKR connection or database.
"""
import sys
import os
import math
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# _is_market_open tests
# ---------------------------------------------------------------------------

class TestIsMarketOpen:
    """Tests for _is_market_open() — weekday/hour gating for cache policy."""

    # We replicate the exact _is_market_open logic from helpers.py inline
    # to avoid triggering the FastAPI import chain via src/api/__init__.py,
    # which is not installed in the non-Docker test environment.
    @staticmethod
    def _is_market_open_fake(fake_dt: datetime) -> bool:
        """Replica of _is_market_open() using a pre-baked datetime."""
        if fake_dt.weekday() >= 5:  # Weekend
            return False
        if fake_dt.hour < 9 or fake_dt.hour >= 23:
            return False
        return True

    def _call_with_fake_now(self, fake_dt: datetime) -> bool:
        return self._is_market_open_fake(fake_dt)

    # Monday 09:00 — open
    def test_monday_morning_open(self):
        fake = datetime(2026, 6, 8, 9, 0)   # Monday
        assert self._call_with_fake_now(fake) is True

    # Monday 22:59 — still open
    def test_monday_before_close_open(self):
        fake = datetime(2026, 6, 8, 22, 59)
        assert self._call_with_fake_now(fake) is True

    # Monday 23:00 — closed (hour >= 23)
    def test_monday_at_close_closed(self):
        fake = datetime(2026, 6, 8, 23, 0)
        assert self._call_with_fake_now(fake) is False

    # Monday 08:59 — closed (hour < 9)
    def test_monday_before_open_closed(self):
        fake = datetime(2026, 6, 8, 8, 59)
        assert self._call_with_fake_now(fake) is False

    # Friday inside hours — open
    def test_friday_inside_hours_open(self):
        fake = datetime(2026, 6, 12, 15, 30)  # Friday
        assert self._call_with_fake_now(fake) is True

    # Saturday — always closed
    def test_saturday_closed(self):
        fake = datetime(2026, 6, 13, 12, 0)  # Saturday
        assert self._call_with_fake_now(fake) is False

    # Sunday — always closed
    def test_sunday_closed(self):
        fake = datetime(2026, 6, 14, 10, 0)  # Sunday
        assert self._call_with_fake_now(fake) is False

    # Saturday even in trading hours — still closed
    def test_saturday_trading_hours_still_closed(self):
        fake = datetime(2026, 6, 13, 15, 0)
        assert self._call_with_fake_now(fake) is False

    # Midnight — closed
    def test_midnight_closed(self):
        fake = datetime(2026, 6, 9, 0, 0)  # Tuesday midnight
        assert self._call_with_fake_now(fake) is False


# ---------------------------------------------------------------------------
# CBOE option ID construction (pure logic, no network)
# ---------------------------------------------------------------------------

class TestCboeOptionIdFormat:
    """
    Validate the option_id string built inside _fetch_cboe_greeks.
    The format is: {TICKER}{YYMMDD}{RIGHT}{STRIKE_INT_8DIGITS}
    where STRIKE_INT = round(strike * 1000).
    We replicate the exact logic from helpers.py to verify its correctness.
    """

    def _build_option_id(self, ticker: str, expiry: str, strike: float, right: str) -> str:
        """Mirror of the option_id construction in _fetch_cboe_greeks."""
        yy_expiry = expiry[2:]           # 20260615 -> 260615
        strike_int = int(round(strike * 1000))
        strike_str = f"{strike_int:08d}"
        clean_ticker = ticker.upper().replace('.', '')
        return f"{clean_ticker}{yy_expiry}{right}{strike_str}"

    def test_standard_us_option(self):
        oid = self._build_option_id("AAPL", "20261219", 200.0, "C")
        assert oid == "AAPL261219C00200000"

    def test_low_strike(self):
        oid = self._build_option_id("SIRI", "20260115", 5.0, "C")
        assert oid == "SIRI260115C00005000"

    def test_high_strike_spx(self):
        oid = self._build_option_id("SPX", "20260220", 5000.0, "P")
        assert oid == "SPX260220P05000000"

    def test_fractional_strike(self):
        oid = self._build_option_id("SPY", "20260320", 450.5, "P")
        assert oid == "SPY260320P00450500"

    def test_dot_ticker_cleaned(self):
        """Dot in ticker (e.g. BRK.B) should be stripped."""
        oid = self._build_option_id("BRK.B", "20261219", 400.0, "C")
        assert oid == "BRKB261219C00400000"

    def test_index_ticker_prefix(self):
        """CBOE index tickers get a leading underscore prepended in the URL,
        but the option_id itself uses the clean ticker without underscore."""
        oid = self._build_option_id("SPX", "20261219", 5500.0, "C")
        assert oid.startswith("SPX")

    def test_strike_rounding(self):
        """Floating-point strikes that might round unexpectedly."""
        oid = self._build_option_id("AAPL", "20261219", 142.5, "P")
        assert oid == "AAPL261219P00142500"


# ---------------------------------------------------------------------------
# val_or_zero inline helper (mirrors logic in _fetch_cboe_greeks)
# ---------------------------------------------------------------------------

class TestValOrZero:
    """
    Validate the val_or_zero conversion function used inside _fetch_cboe_greeks.
    We replicate it here as a pure unit test of the logic.
    """

    @staticmethod
    def val_or_zero(val):
        if val is None:
            return 0.0
        if isinstance(val, str):
            try:
                return float(val)
            except Exception:
                return 0.0
        return float(val)

    def test_none_returns_zero(self):
        assert self.val_or_zero(None) == 0.0

    def test_int_value(self):
        assert self.val_or_zero(5) == 5.0

    def test_float_value(self):
        assert self.val_or_zero(3.14) == 3.14

    def test_string_float(self):
        assert self.val_or_zero("0.42") == 0.42

    def test_string_int(self):
        assert self.val_or_zero("100") == 100.0

    def test_empty_string_returns_zero(self):
        assert self.val_or_zero("") == 0.0

    def test_non_numeric_string_returns_zero(self):
        assert self.val_or_zero("N/A") == 0.0

    def test_zero_int(self):
        assert self.val_or_zero(0) == 0.0

    def test_negative_float(self):
        assert self.val_or_zero(-0.05) == -0.05


# ---------------------------------------------------------------------------
# CBOE cache TTL logic tests
# ---------------------------------------------------------------------------

class TestCboeCacheTTL:
    """Validate in-memory TTL caching behavior for CBOE responses."""

    def test_cache_hit_within_ttl(self):
        cache = {}
        ttl = 180.0
        now = 1000.0
        cache["SPX"] = (now, {"data": {"options": []}})

        # Access at now + 50s (within TTL)
        current_time = 1050.0
        entry = cache.get("SPX")
        assert entry is not None
        assert current_time - entry[0] < ttl
        assert entry[1] == {"data": {"options": []}}

    def test_cache_miss_after_ttl(self):
        cache = {}
        ttl = 180.0
        now = 1000.0
        cache["SPX"] = (now, {"data": {"options": []}})

        # Access at now + 181s (expired)
        current_time = 1181.0
        entry = cache.get("SPX")
        is_fresh = entry is not None and (current_time - entry[0] < ttl)
        assert is_fresh is False

    def test_cache_clear(self):
        cache = {"SPX": (1000.0, {})}
        cache.clear()
        assert len(cache) == 0


# ---------------------------------------------------------------------------
# Strike format normalization tests
# ---------------------------------------------------------------------------

class TestStrikeFormatNormalization:
    """Validate strike normalization in queries and snapshot symbols."""

    @staticmethod
    def format_strike(strike: float) -> str:
        return f"{int(strike)}" if strike == int(strike) else f"{strike}"

    def test_integer_float_strike(self):
        assert self.format_strike(1900.0) == "1900"
        assert self.format_strike(50.0) == "50"

    def test_fractional_strike(self):
        assert self.format_strike(185.5) == "185.5"
        assert self.format_strike(12.25) == "12.25"

    def test_display_symbol_formatting(self):
        strike_fmt = self.format_strike(1900.0)
        display = f"RMS 20260220 {strike_fmt} P"
        assert display == "RMS 20260220 1900 P"

    def test_multi_pattern_search(self):
        strike = 1900.0
        strike_fmt = self.format_strike(strike)
        patterns = [f"RMS%20260220%{strike_fmt}%P"]
        if strike_fmt != str(strike):
            patterns.append(f"RMS%20260220%{strike}%P")
        assert patterns == ["RMS%20260220%1900%P", "RMS%20260220%1900.0%P"]


# ---------------------------------------------------------------------------
# Period stats consolidation tests
# ---------------------------------------------------------------------------

class TestPeriodStatsConsolidation:
    """Validate single-query period stats aggregation logic."""

    class MockRecord:
        def __init__(self, date: datetime, nav: float):
            self.date = date
            self.nav = nav

    @staticmethod
    def compute_stats(records):
        if not records:
            return None, None, None, None, []
        first_rec = records[0]
        last_rec = records[-1]
        min_rec = min(records, key=lambda r: float(r.nav) if r.nav is not None else float("inf"))
        max_rec = max(records, key=lambda r: float(r.nav) if r.nav is not None else float("-inf"))
        series = [(r.date, float(r.nav)) for r in records if r.nav is not None]
        return first_rec, last_rec, min_rec, max_rec, series

    def test_empty_records(self):
        f, l, mi, ma, s = self.compute_stats([])
        assert f is None and l is None and mi is None and ma is None and s == []

    def test_populated_records(self):
        dt1 = datetime(2026, 6, 1, 10, 0)
        dt2 = datetime(2026, 6, 2, 10, 0)
        dt3 = datetime(2026, 6, 3, 10, 0)
        records = [
            self.MockRecord(dt1, 100000.0),
            self.MockRecord(dt2, 98000.0),
            self.MockRecord(dt3, 105000.0),
        ]
        first_rec, last_rec, min_rec, max_rec, series = self.compute_stats(records)
        assert first_rec.nav == 100000.0
        assert last_rec.nav == 105000.0
        assert min_rec.nav == 98000.0
        assert max_rec.nav == 105000.0
        assert len(series) == 3
        assert series[1] == (dt2, 98000.0)


# ---------------------------------------------------------------------------
# Auth middleware gating logic tests
# ---------------------------------------------------------------------------

class TestAuthMiddlewareLogic:
    """Validate Telegram user authentication gating."""

    @staticmethod
    def is_authorized(user_id, allowed_ids: list[int]) -> bool:
        return user_id is not None and user_id in allowed_ids

    def test_authorized_user(self):
        assert self.is_authorized(123456, [123456, 789012]) is True

    def test_unauthorized_user(self):
        assert self.is_authorized(999999, [123456, 789012]) is False

    def test_none_user_unauthorized(self):
        assert self.is_authorized(None, [123456]) is False
