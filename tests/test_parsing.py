"""
Unit tests for pure helper functions in src/parsing.py:
- parse_symbol: Market suffix and exchange prefix resolution
- greeks_are_valid: Greeks validation logic
- snap_is_valid: OptionSnapshot cache validation
- parse_osi_symbol: OSI-format option symbol parsing
- parse_european_symbol: European IBKR localSymbol parsing

All functions are imported directly from the production module — no inline copies.
"""
import sys
import os
import pytest

# Allow importing from src/ without installing as a package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.parsing import (
    parse_symbol,
    greeks_are_valid,
    snap_is_valid,
    parse_osi_symbol,
    parse_european_symbol,
)


# ---------------------------------------------------------------------------
# Helpers for test data
# ---------------------------------------------------------------------------

class FakeGreeks:
    """Mimics ib_async OptionGreeks with named attributes."""
    def __init__(self, delta=0.0, gamma=0.0, theta=0.0, vega=0.0, undPrice=0.0):
        self.delta = delta
        self.gamma = gamma
        self.theta = theta
        self.vega = vega
        self.undPrice = undPrice


class FakeSnap:
    """Mimics OptionSnapshot ORM model."""
    def __init__(self, delta=0.0, gamma=0.0, theta=0.0, vega=0.0, underlying_price=0.0):
        self.delta = delta
        self.gamma = gamma
        self.theta = theta
        self.vega = vega
        self.underlying_price = underlying_price


# ======================================================================
# parse_symbol tests
# ======================================================================

class TestParseSymbol:
    """Tests for parse_symbol — resolves ticker + exchange + currency."""

    def test_us_stock_defaults(self):
        assert parse_symbol("AAPL") == ("AAPL", "SMART", "USD")

    def test_us_stock_lowercase(self):
        assert parse_symbol("aapl") == ("AAPL", "SMART", "USD")

    def test_us_stock_whitespace(self):
        assert parse_symbol("  AAPL  ") == ("AAPL", "SMART", "USD")

    def test_london_suffix(self):
        assert parse_symbol("BATS.L") == ("BATS", "LSE", "GBP")

    def test_paris_suffix(self):
        assert parse_symbol("RMS.PA") == ("RMS", "SBF", "EUR")

    def test_amsterdam_suffix(self):
        assert parse_symbol("ASML.AS") == ("ASML", "AEB", "EUR")

    def test_germany_suffix(self):
        assert parse_symbol("SAP.DE") == ("SAP", "IBIS", "EUR")

    def test_switzerland_suffix(self):
        assert parse_symbol("NESN.SW") == ("NESN", "EBS", "CHF")

    def test_spain_suffix(self):
        assert parse_symbol("SAN.MC") == ("SAN", "BM", "EUR")

    def test_italy_suffix(self):
        assert parse_symbol("UCG.MI") == ("UCG", "BVME", "EUR")

    def test_suffix_case_insensitive(self):
        assert parse_symbol("rms.pa") == ("RMS", "SBF", "EUR")

    def test_unknown_suffix_treated_as_us(self):
        """Unknown suffixes should NOT be stripped — treated as part of ticker."""
        ticker, exchange, currency = parse_symbol("XYZ.JP")
        assert exchange == "SMART"
        assert currency == "USD"
        assert ticker == "XYZ.JP"

    def test_empty_string(self):
        assert parse_symbol("") == ("", "SMART", "USD")

    def test_single_char(self):
        assert parse_symbol("A") == ("A", "SMART", "USD")

    def test_ticker_with_dot_not_matching_suffix(self):
        """BRK.B doesn't match any suffix, should be treated as US."""
        ticker, exchange, currency = parse_symbol("BRK.B")
        assert exchange == "SMART"
        assert currency == "USD"


# ======================================================================
# greeks_are_valid tests
# ======================================================================

class TestGreeksAreValid:
    """Tests for greeks_are_valid — validates market data quality."""

    def test_none_is_invalid(self):
        assert greeks_are_valid(None) is False

    def test_all_zero_is_invalid(self):
        g = FakeGreeks(delta=0, gamma=0, theta=0, vega=0)
        assert greeks_are_valid(g) is False

    def test_only_delta_is_valid(self):
        g = FakeGreeks(delta=0.5)
        assert greeks_are_valid(g) is True

    def test_only_theta_is_valid(self):
        g = FakeGreeks(theta=-0.05)
        assert greeks_are_valid(g) is True

    def test_only_gamma_is_valid(self):
        g = FakeGreeks(gamma=0.01)
        assert greeks_are_valid(g) is True

    def test_only_vega_is_valid(self):
        g = FakeGreeks(vega=0.1)
        assert greeks_are_valid(g) is True

    def test_full_greeks_valid(self):
        g = FakeGreeks(delta=0.45, gamma=0.02, theta=-0.03, vega=0.15)
        assert greeks_are_valid(g) is True

    def test_nan_delta_treated_as_zero(self):
        g = FakeGreeks(delta=float('nan'), gamma=0, theta=0, vega=0)
        assert greeks_are_valid(g) is False

    def test_nan_with_valid_gamma(self):
        g = FakeGreeks(delta=float('nan'), gamma=0.01)
        assert greeks_are_valid(g) is True

    def test_none_values_treated_as_zero(self):
        g = FakeGreeks()
        g.delta = None
        g.gamma = None
        g.theta = None
        g.vega = None
        assert greeks_are_valid(g) is False

    def test_mixed_none_and_valid(self):
        g = FakeGreeks()
        g.delta = None
        g.gamma = 0.02
        assert greeks_are_valid(g) is True


# ======================================================================
# snap_is_valid tests
# ======================================================================

class TestSnapIsValid:
    """Tests for snap_is_valid — validates cached snapshot quality."""

    def test_none_is_invalid(self):
        assert snap_is_valid(None) is False

    def test_all_zero_is_invalid(self):
        s = FakeSnap(delta=0, gamma=0, theta=0, vega=0)
        assert snap_is_valid(s) is False

    def test_has_delta_is_valid(self):
        s = FakeSnap(delta=-0.3)
        assert snap_is_valid(s) is True

    def test_none_values_treated_as_zero(self):
        s = FakeSnap()
        s.delta = None
        s.gamma = None
        s.theta = None
        s.vega = None
        assert snap_is_valid(s) is False

    def test_mixed_valid_and_none(self):
        s = FakeSnap()
        s.delta = None
        s.vega = 0.1
        assert snap_is_valid(s) is True


# ======================================================================
# parse_osi_symbol tests
# ======================================================================

class TestOSISymbolParsing:
    """Tests for parse_osi_symbol — OSI-format option symbols."""

    def test_basic_osi(self):
        r = parse_osi_symbol("ASTS  260109P00065000")
        assert r["ticker"] == "ASTS"
        assert r["expiry"] == "20260109"
        assert r["strike"] == 65.0
        assert r["right"] == "P"

    def test_osi_call(self):
        r = parse_osi_symbol("AAPL  251219C00200000")
        assert r["ticker"] == "AAPL"
        assert r["expiry"] == "20251219"
        assert r["strike"] == 200.0
        assert r["right"] == "C"

    def test_osi_fractional_strike(self):
        r = parse_osi_symbol("SPY   260320P00450500")
        assert r["ticker"] == "SPY"
        assert r["strike"] == 450.5

    def test_osi_low_strike(self):
        r = parse_osi_symbol("SIRI  260115C00005000")
        assert r["strike"] == 5.0

    def test_osi_high_strike(self):
        r = parse_osi_symbol("SPX   260220P05000000")
        assert r["strike"] == 5000.0


# ======================================================================
# parse_european_symbol tests
# ======================================================================

class TestEuropeanSymbolParsing:
    """Tests for parse_european_symbol — IBKR localSymbol format."""

    def test_basic_put(self):
        r = parse_european_symbol("P HMI  20260220 1900 M")
        assert r["right"] == "P"
        assert r["ticker"] == "HMI"
        assert r["expiry"] == "20260220"
        assert r["strike"] == 1900.0

    def test_basic_call(self):
        r = parse_european_symbol("C RMS 20260320 1500")
        assert r["right"] == "C"
        assert r["ticker"] == "RMS"
        assert r["expiry"] == "20260320"
        assert r["strike"] == 1500.0

    def test_too_few_parts_raises(self):
        with pytest.raises(ValueError):
            parse_european_symbol("P HMI")

    def test_fractional_strike(self):
        r = parse_european_symbol("P SAP 20260220 250.5")
        assert r["strike"] == 250.5
