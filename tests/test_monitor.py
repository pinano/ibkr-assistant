"""
Unit tests for monitor.py alert logic.

Tests cover pure-logic components that do NOT require a live IBKR
connection, a DB, or an actual Telegram bot:
  - Intrinsic value and time value computation for puts and calls
  - display_expiry date formatting (YYYYMMDD -> DDMMMyyy)
  - Delta alert filtering: qty >= 0 skipped, excluded underlyings skipped,
    abs(delta) <= threshold skipped
  - Alert throttling (< 4 hours since last alert)
  - Alert cache stale key pruning
"""
import sys
import os
import datetime
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# Pure helpers replicated from monitor.py for isolation
# ---------------------------------------------------------------------------

def compute_intrinsic_and_tv(right: str, strike: float,
                               underlying_price: float, last_price: float):
    """
    Mirror of the intrinsic/time-value block in check_alerts().
    Returns (intrinsic, time_value) or (None, None) if inputs missing.
    """
    if not (last_price and last_price > 0 and underlying_price and underlying_price > 0 and strike):
        return None, None
    if right.upper() == 'P':
        intrinsic = max(0.0, strike - underlying_price)
    else:  # Call
        intrinsic = max(0.0, underlying_price - strike)
    time_value = last_price - intrinsic
    return intrinsic, time_value


def format_display_expiry(expiry_str: str) -> str:
    """
    Mirror of the display_expiry formatting in check_alerts().
    Converts YYYYMMDD -> DDMMMyyy (e.g. '20260619' -> '19Jun26').
    """
    display_expiry = expiry_str
    if len(display_expiry) == 8 and display_expiry.isdigit():
        try:
            dt = datetime.datetime.strptime(display_expiry, "%Y%m%d")
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            month_str = months[dt.month - 1]
            display_expiry = f"{dt.day:02d}{month_str}{dt.year % 100:02d}"
        except Exception:
            pass
    return display_expiry


def should_alert(qty: float, delta: float, threshold: float,
                 underlying: str, exclude_list: list) -> bool:
    """
    Mirror of the per-contract alert decision logic in check_alerts().
    Returns True if this contract should trigger an alert.
    """
    # Skip long positions
    if qty >= 0:
        return False
    # Skip excluded tickers
    base_und = underlying.split(':')[-1].upper()
    if underlying.upper() in [x.upper() for x in exclude_list]:
        return False
    if base_und in [x.upper() for x in exclude_list]:
        return False
    # Skip near-zero delta (no data)
    if abs(delta) < 0.0001:
        return False
    # Skip below threshold
    if abs(delta) <= threshold:
        return False
    return True


# ---------------------------------------------------------------------------
# Intrinsic / Time Value Tests
# ---------------------------------------------------------------------------

class TestIntrinsicAndTimeValue:
    """Tests for put/call intrinsic & time value computation."""

    def test_itm_put(self):
        """Put with underlying < strike: intrinsic = strike - underlying."""
        intrinsic, tv = compute_intrinsic_and_tv('P', 100.0, 90.0, 12.0)
        assert intrinsic == pytest.approx(10.0)
        assert tv == pytest.approx(2.0)

    def test_otm_put(self):
        """OTM put: intrinsic=0, TV=last_price."""
        intrinsic, tv = compute_intrinsic_and_tv('P', 100.0, 110.0, 3.0)
        assert intrinsic == pytest.approx(0.0)
        assert tv == pytest.approx(3.0)

    def test_itm_call(self):
        """Call with underlying > strike: intrinsic = underlying - strike."""
        intrinsic, tv = compute_intrinsic_and_tv('C', 100.0, 115.0, 18.0)
        assert intrinsic == pytest.approx(15.0)
        assert tv == pytest.approx(3.0)

    def test_otm_call(self):
        """OTM call: intrinsic=0, TV=last_price."""
        intrinsic, tv = compute_intrinsic_and_tv('C', 100.0, 90.0, 4.0)
        assert intrinsic == pytest.approx(0.0)
        assert tv == pytest.approx(4.0)

    def test_atm_put(self):
        """ATM put: underlying == strike, intrinsic=0."""
        intrinsic, tv = compute_intrinsic_and_tv('P', 100.0, 100.0, 5.0)
        assert intrinsic == pytest.approx(0.0)
        assert tv == pytest.approx(5.0)

    def test_missing_last_price(self):
        intrinsic, tv = compute_intrinsic_and_tv('P', 100.0, 90.0, 0.0)
        assert intrinsic is None
        assert tv is None

    def test_missing_underlying(self):
        intrinsic, tv = compute_intrinsic_and_tv('P', 100.0, 0.0, 10.0)
        assert intrinsic is None
        assert tv is None

    def test_case_insensitive_right(self):
        """Right is case-insensitive."""
        intrinsic_p, _ = compute_intrinsic_and_tv('p', 100.0, 90.0, 12.0)
        intrinsic_P, _ = compute_intrinsic_and_tv('P', 100.0, 90.0, 12.0)
        assert intrinsic_p == intrinsic_P

    def test_negative_time_value_deep_itm(self):
        """Deep ITM: TV can be small or even slightly negative due to pricing."""
        intrinsic, tv = compute_intrinsic_and_tv('P', 100.0, 50.0, 49.5)
        assert intrinsic == pytest.approx(50.0)
        assert tv == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# Display Expiry Formatting Tests
# ---------------------------------------------------------------------------

class TestDisplayExpiryFormat:
    """Tests for YYYYMMDD -> DDMMMyyy date display formatting."""

    def test_standard_date(self):
        assert format_display_expiry("20260619") == "19Jun26"

    def test_january(self):
        assert format_display_expiry("20260109") == "09Jan26"

    def test_december(self):
        assert format_display_expiry("20261219") == "19Dec26"

    def test_all_months(self):
        months = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"]
        for i, mon in enumerate(months, 1):
            date_str = f"2026{i:02d}15"
            result = format_display_expiry(date_str)
            assert result == f"15{mon}26"

    def test_short_string_passthrough(self):
        """Non-8-digit strings are returned as-is."""
        assert format_display_expiry("260619") == "260619"

    def test_non_digit_passthrough(self):
        assert format_display_expiry("20260619X") == "20260619X"

    def test_invalid_date_passthrough(self):
        """Invalid dates that fail strptime are returned unchanged."""
        assert format_display_expiry("20261340") == "20261340"

    def test_single_digit_day(self):
        """Day < 10 should be zero-padded."""
        assert format_display_expiry("20260602") == "02Jun26"


# ---------------------------------------------------------------------------
# Alert Filtering Tests
# ---------------------------------------------------------------------------

class TestAlertFiltering:
    """Tests for per-contract delta alert decision logic."""

    THRESHOLD = 0.25

    def test_short_position_above_threshold_alerts(self):
        assert should_alert(-10, -0.50, self.THRESHOLD, "AAPL", []) is True

    def test_long_position_not_alerted(self):
        assert should_alert(5, -0.80, self.THRESHOLD, "AAPL", []) is False

    def test_flat_position_not_alerted(self):
        assert should_alert(0, -0.80, self.THRESHOLD, "AAPL", []) is False

    def test_below_threshold_not_alerted(self):
        assert should_alert(-5, -0.20, self.THRESHOLD, "AAPL", []) is False

    def test_exactly_at_threshold_not_alerted(self):
        """abs(delta) == threshold is NOT above — no alert."""
        assert should_alert(-5, -0.25, self.THRESHOLD, "AAPL", []) is False

    def test_just_above_threshold_alerts(self):
        assert should_alert(-5, -0.251, self.THRESHOLD, "AAPL", []) is True

    def test_zero_delta_not_alerted(self):
        assert should_alert(-5, 0.0, self.THRESHOLD, "AAPL", []) is False

    def test_near_zero_delta_not_alerted(self):
        """abs(delta) < 0.0001 is treated as no-data."""
        assert should_alert(-5, 0.00009, self.THRESHOLD, "AAPL", []) is False

    def test_excluded_underlying_direct_match(self):
        assert should_alert(-5, -0.80, self.THRESHOLD, "BOX", ["BOX"]) is False

    def test_excluded_underlying_prefix_match(self):
        """EPA:MC — the part after ':' is 'MC', which should be excluded."""
        assert should_alert(-5, -0.80, self.THRESHOLD, "EPA:MC", ["MC"]) is False

    def test_excluded_list_case_insensitive(self):
        assert should_alert(-5, -0.80, self.THRESHOLD, "box", ["BOX"]) is False

    def test_different_ticker_not_excluded(self):
        assert should_alert(-5, -0.80, self.THRESHOLD, "AAPL", ["BOX", "SPX"]) is True

    def test_positive_delta_call_above_threshold_alerts(self):
        """Short calls have positive delta; abs check covers both sides."""
        assert should_alert(-3, 0.60, self.THRESHOLD, "AAPL", []) is True


# ---------------------------------------------------------------------------
# Alert Throttle Tests
# ---------------------------------------------------------------------------

class TestAlertThrottle:
    """Tests for the 4-hour throttle logic in check_alerts()."""

    def _is_throttled(self, cache: dict, con_id: int, now: datetime.datetime) -> bool:
        """Mirror of the throttle check in check_alerts()."""
        last_alert = cache.get(con_id)
        if last_alert and (now - last_alert) <= datetime.timedelta(hours=4):
            return True
        return False

    def test_no_prior_alert_not_throttled(self):
        now = datetime.datetime(2026, 6, 14, 12, 0)
        assert self._is_throttled({}, 1001, now) is False

    def test_alert_3h59m_ago_throttled(self):
        now = datetime.datetime(2026, 6, 14, 12, 0)
        last = now - datetime.timedelta(hours=3, minutes=59)
        assert self._is_throttled({1001: last}, 1001, now) is True

    def test_alert_exactly_4h_ago_throttled(self):
        """Exactly 4h is still within the throttle window (<=)."""
        now = datetime.datetime(2026, 6, 14, 12, 0)
        last = now - datetime.timedelta(hours=4)
        assert self._is_throttled({1001: last}, 1001, now) is True

    def test_alert_4h1m_ago_not_throttled(self):
        now = datetime.datetime(2026, 6, 14, 12, 0)
        last = now - datetime.timedelta(hours=4, minutes=1)
        assert self._is_throttled({1001: last}, 1001, now) is False

    def test_different_con_id_not_throttled(self):
        now = datetime.datetime(2026, 6, 14, 12, 0)
        last = now - datetime.timedelta(minutes=10)
        assert self._is_throttled({1001: last}, 9999, now) is False


# ---------------------------------------------------------------------------
# Stale alert cache pruning
# ---------------------------------------------------------------------------

class TestAlertCachePruning:
    """Tests for stale alert cache key removal after option leaves portfolio."""

    def _prune(self, cache: dict, active_con_ids: set) -> dict:
        """Mirror of the stale-key pruning block at end of check_alerts()."""
        stale_keys = [k for k in cache if k not in active_con_ids]
        for k in stale_keys:
            del cache[k]
        return cache

    def test_active_keys_kept(self):
        cache = {1: "t", 2: "t"}
        result = self._prune(cache, {1, 2})
        assert set(result.keys()) == {1, 2}

    def test_stale_key_removed(self):
        cache = {1: "t", 2: "t", 3: "t"}
        result = self._prune(cache, {1, 2})
        assert 3 not in result

    def test_all_stale_cleared(self):
        cache = {10: "t", 20: "t"}
        result = self._prune(cache, set())
        assert result == {}

    def test_empty_cache_no_error(self):
        result = self._prune({}, {1, 2})
        assert result == {}
