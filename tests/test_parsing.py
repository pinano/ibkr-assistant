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
    calc_option_intrinsic,
    calc_option_extrinsic,
    calc_option_mid,
    clean_price,
    clean_size,
    clean_greek,
    calc_moneyness_pct,
    filter_strikes_window,
    select_best_option_chain,
    calc_bs_greeks,
    is_market_open_for_symbol,
    determine_market_statuses,
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


# ======================================================================
# parse_dividend_description tests
# ======================================================================

import re

def parse_dividend_description(description: str, total_amount: float):
    # Match the rate and currency
    # e.g., "CASH DIVIDEND USD 1.452 PER SHARE"
    rate_match = re.search(r"DIVIDEND\s+([A-Z]{3})\s+([0-9.]+)\s+PER\s+SHARE", description, re.IGNORECASE)
    
    currency = None
    rate = None
    qty = None
    concept = "Dividend"
    
    if rate_match:
        currency = rate_match.group(1).upper()
        rate = float(rate_match.group(2))
        if rate > 0:
            qty = round(total_amount / rate, 4)
            # If it is close to an integer, round to int
            if abs(qty - round(qty)) < 1e-4:
                qty = int(round(qty))
    
    # Extract concept: usually the text in the last set of parentheses
    # e.g., "(Ordinary Dividend)"
    concept_match = re.search(r"\(([^)]+)\)\s*$", description)
    if concept_match:
        concept = concept_match.group(1).strip()
    else:
        # Clean description by removing prefix ticker/ISIN if possible
        # e.g. "HSY(US4278661081) CASH DIVIDEND" -> "CASH DIVIDEND"
        cleaned_desc = re.sub(r"^[A-Z0-9.\s]+\([^)]+\)\s*", "", description, flags=re.IGNORECASE)
        if cleaned_desc.strip():
            concept = cleaned_desc.strip()
        else:
            concept = description.strip()
            
    return qty, rate, concept


class TestParseDividendDescription:
    """Tests for parse_dividend_description — parses dividend txn descriptions."""

    def test_ordinary_dividend_us(self):
        desc = "HSY(US4278661081) CASH DIVIDEND USD 1.452 PER SHARE (Ordinary Dividend)"
        qty, rate, concept = parse_dividend_description(desc, 58.08)
        assert qty == 40
        assert rate == 1.452
        assert concept == "Ordinary Dividend"

    def test_ordinary_dividend_european(self):
        desc = "O(US7561091049) CASH DIVIDEND USD 0.2705 PER SHARE (Ordinary Dividend)"
        qty, rate, concept = parse_dividend_description(desc, 40.575)
        assert qty == 150
        assert rate == 0.2705
        assert concept == "Ordinary Dividend"

    def test_no_parentheses_fallback(self):
        desc = "AAPL(US0378331005) CASH DIVIDEND USD 0.25 PER SHARE"
        qty, rate, concept = parse_dividend_description(desc, 25.0)
        assert qty == 100
        assert rate == 0.25
        assert concept == "CASH DIVIDEND USD 0.25 PER SHARE"

    def test_no_match(self):
        desc = "Some random text description without dividend keywords"
        qty, rate, concept = parse_dividend_description(desc, 10.0)
        assert qty is None
        assert rate is None
        assert concept == "Some random text description without dividend keywords"


# ---------------------------------------------------------------------------
# format_currency tests
# ---------------------------------------------------------------------------

def fmt_num(val, precision=2):
    try:
        f = float(val)
        # European format: comma as decimal separator
        return ('{:.' + str(precision) + 'f}').format(round(f,
                                                            precision)).replace('.', ',')
    except (ValueError, TypeError):
        return val


def format_currency(currency_code: str, val: float, precision: int = 2) -> str:
    if val is None:
        return "—"
    if not currency_code:
        return fmt_num(val, precision)
    symbols = {
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
        "CAD": "C$",
        "AUD": "A$",
        "CHF": "CHF",
        "SEK": "kr",
    }
    symbol = symbols.get(currency_code.upper(), currency_code)
    formatted_val = fmt_num(val, precision)
    if len(symbol) == 1 or symbol == "kr":
        return f"{symbol}{formatted_val}"
    else:
        return f"{symbol} {formatted_val}"


class TestFormatCurrency:
    """Tests for format_currency — formats currencies with standard symbols."""

    def test_usd_happy_path(self):
        assert format_currency("USD", 22.0, 2) == "$22,00"
        assert format_currency("USD", 1.452, 4) == "$1,4520"

    def test_eur_happy_path(self):
        assert format_currency("EUR", 25.62, 2) == "€25,62"
        assert format_currency("EUR", 0.2705, 4) == "€0,2705"

    def test_gbp_happy_path(self):
        assert format_currency("GBP", 15.5, 2) == "£15,50"

    def test_sek_happy_path(self):
        assert format_currency("SEK", 100.0, 2) == "kr100,00"

    def test_unknown_currency_code(self):
        assert format_currency("NZD", 12.34, 2) == "NZD 12,34"
        assert format_currency("CHF", 10.0, 2) == "CHF 10,00"

    def test_none_value(self):
        assert format_currency("USD", None) == "—"

    def test_empty_currency(self):
        assert format_currency("", 12.34, 2) == "12,34"


# ======================================================================
# Option calculations & strike filtering tests
# ======================================================================

class TestCalcOptionIntrinsic:
    """Tests for calc_option_intrinsic — Call and Put intrinsic math."""

    def test_call_itm(self):
        # Stock at 2000, Call strike 1900 -> intrinsic 100
        assert calc_option_intrinsic("C", 1900.0, 2000.0) == 100.0
        assert calc_option_intrinsic("CALL", 1900.0, 2000.0) == 100.0

    def test_call_otm(self):
        # Stock at 2000, Call strike 2100 -> intrinsic 0
        assert calc_option_intrinsic("C", 2100.0, 2000.0) == 0.0

    def test_call_atm(self):
        assert calc_option_intrinsic("C", 2000.0, 2000.0) == 0.0

    def test_put_itm(self):
        # Stock at 2000, Put strike 2100 -> intrinsic 100
        assert calc_option_intrinsic("P", 2100.0, 2000.0) == 100.0
        assert calc_option_intrinsic("PUT", 2100.0, 2000.0) == 100.0

    def test_put_otm(self):
        # Stock at 2000, Put strike 1900 -> intrinsic 0
        assert calc_option_intrinsic("P", 1900.0, 2000.0) == 0.0

    def test_put_atm(self):
        assert calc_option_intrinsic("P", 2000.0, 2000.0) == 0.0

    def test_zero_underlying_returns_zero(self):
        assert calc_option_intrinsic("C", 1900.0, 0.0) == 0.0
        assert calc_option_intrinsic("P", 1900.0, 0.0) == 0.0

    def test_zero_strike_returns_zero(self):
        assert calc_option_intrinsic("C", 0.0, 2000.0) == 0.0
        assert calc_option_intrinsic("P", 0.0, 2000.0) == 0.0


class TestCalcOptionExtrinsic:
    """Tests for calc_option_extrinsic — Time value math."""

    def test_extrinsic_positive(self):
        # Option market price 120.0, intrinsic 100.0 -> extrinsic 20.0
        assert calc_option_extrinsic(120.0, 100.0) == 20.0

    def test_extrinsic_when_price_below_intrinsic(self):
        # Market price slightly below theoretical intrinsic due to spread -> capped at 0.0
        assert calc_option_extrinsic(95.0, 100.0) == 0.0

    def test_extrinsic_otm(self):
        # Option market price 15.0, intrinsic 0.0 -> extrinsic 15.0
        assert calc_option_extrinsic(15.0, 0.0) == 15.0

    def test_zero_price_returns_zero(self):
        assert calc_option_extrinsic(0.0, 10.0) == 0.0

    def test_none_price_or_intrinsic_returns_none(self):
        assert calc_option_extrinsic(None, 10.0) is None
        assert calc_option_extrinsic(15.0, None) is None
        assert calc_option_extrinsic(None, None) is None

    def test_rms_put_scenario(self):
        # RMS 1580P scenario: strike 1580, spot 1420 -> intrinsic 160.0
        # If mid is 151.75 -> extrinsic is max(151.75 - 160.0, 0) == 0.0 (never negative)
        assert calc_option_extrinsic(151.75, 160.0) == 0.0


class TestCleanPrice:
    """Tests for clean_price helper."""

    def test_none_returns_none(self):
        assert clean_price(None) is None

    def test_negative_returns_none(self):
        assert clean_price(-1) is None
        assert clean_price(-1.0) is None
        assert clean_price(-0.01) is None

    def test_nan_returns_none(self):
        assert clean_price(float("nan")) is None

    def test_zero_returns_zero(self):
        # Must distinguish real price 0.0 from missing data (None)
        assert clean_price(0) == 0.0
        assert clean_price(0.0) == 0.0
        assert clean_price("0.0") == 0.0

    def test_valid_positive_price(self):
        assert clean_price(147.0) == 147.0
        assert clean_price("156.5") == 156.5

    def test_invalid_string_returns_none(self):
        assert clean_price("invalid") is None
        assert clean_price("") is None


class TestCleanSize:
    """Tests for clean_size helper."""

    def test_none_returns_none(self):
        assert clean_size(None) is None

    def test_negative_returns_none(self):
        assert clean_size(-1) is None

    def test_nan_returns_none(self):
        assert clean_size(float("nan")) is None

    def test_zero_returns_zero(self):
        assert clean_size(0) == 0
        assert clean_size("0") == 0

    def test_valid_size(self):
        assert clean_size(10) == 10
        assert clean_size(15.0) == 15
        assert clean_size("25") == 25


class TestCleanGreek:
    """Tests for clean_greek helper."""

    def test_none_returns_none(self):
        assert clean_greek(None) is None

    def test_nan_returns_none(self):
        assert clean_greek(float("nan")) is None

    def test_valid_greeks(self):
        assert clean_greek(0.48) == 0.48
        assert clean_greek(-0.35) == -0.35
        assert clean_greek(0.0) == 0.0


class TestCalcOptionMid:
    """Tests for calc_option_mid — only calculated when both bid and ask are valid."""

    def test_valid_bid_and_ask(self):
        # User example: bid 147.0, ask 156.5 -> mid 151.75
        assert calc_option_mid(147.0, 156.5) == 151.75

    def test_missing_bid_returns_none(self):
        assert calc_option_mid(None, 156.5) is None

    def test_missing_ask_returns_none(self):
        assert calc_option_mid(147.0, None) is None

    def test_both_missing_returns_none(self):
        assert calc_option_mid(None, None) is None

    def test_negative_bid_or_ask_returns_none(self):
        assert calc_option_mid(-1.0, 156.5) is None
        assert calc_option_mid(147.0, -1.0) is None

    def test_nan_returns_none(self):
        assert calc_option_mid(float("nan"), 156.5) is None
        assert calc_option_mid(147.0, float("nan")) is None

    def test_zero_bid_with_positive_ask(self):
        assert calc_option_mid(0.0, 0.10) == 0.05

    def test_zero_bid_and_ask(self):
        assert calc_option_mid(0.0, 0.0) == 0.0


class TestCalcMoneynessPct:
    """Tests for calc_moneyness_pct — percentage deviation from spot."""

    def test_strike_above_spot(self):
        # Strike 2100, spot 2000 -> +5.0%
        assert calc_moneyness_pct(2100.0, 2000.0) == 5.0

    def test_strike_below_spot(self):
        # Strike 1900, spot 2000 -> -5.0%
        assert calc_moneyness_pct(1900.0, 2000.0) == -5.0

    def test_strike_at_spot(self):
        assert calc_moneyness_pct(2000.0, 2000.0) == 0.0

    def test_zero_spot_returns_zero(self):
        assert calc_moneyness_pct(2000.0, 0.0) == 0.0


class TestFilterStrikesWindow:
    """Tests for filter_strikes_window — range and ATM window filtering."""

    ALL_STRIKES = [1700.0, 1750.0, 1800.0, 1850.0, 1900.0, 1950.0, 2000.0, 2050.0, 2100.0, 2150.0, 2200.0, 2250.0]

    def test_empty_strikes(self):
        assert filter_strikes_window([]) == []

    def test_default_strikes_below_and_above(self):
        # Spot at 1980 -> closest strike is 2000. 5 below + ATM + 5 above = 11 strikes
        res = filter_strikes_window(self.ALL_STRIKES, underlying_price=1980.0, strikes_below=5, strikes_above=5)
        assert len(res) == 11
        assert res == [1750.0, 1800.0, 1850.0, 1900.0, 1950.0, 2000.0, 2050.0, 2100.0, 2150.0, 2200.0, 2250.0]

    def test_asymmetric_strikes_below_and_above(self):
        # 3 below, 1 above -> [1850, 1900, 1950, 2000, 2050]
        res = filter_strikes_window(self.ALL_STRIKES, underlying_price=2000.0, strikes_below=3, strikes_above=1)
        assert res == [1850.0, 1900.0, 1950.0, 2000.0, 2050.0]

    def test_explicit_min_and_max(self):
        res = filter_strikes_window(self.ALL_STRIKES, min_strike=1850.0, max_strike=2050.0)
        assert res == [1850.0, 1900.0, 1950.0, 2000.0, 2050.0]

    def test_explicit_min_only(self):
        res = filter_strikes_window(self.ALL_STRIKES, min_strike=2100.0)
        assert res == [2100.0, 2150.0, 2200.0, 2250.0]

    def test_explicit_max_only(self):
        res = filter_strikes_window(self.ALL_STRIKES, max_strike=1800.0)
        assert res == [1700.0, 1750.0, 1800.0]

    def test_atm_near_bottom_boundary(self):
        # Spot at 1650 -> below lowest strike (1700). 3 below + 3 above -> capped at start
        res = filter_strikes_window(self.ALL_STRIKES, underlying_price=1650.0, strikes_below=3, strikes_above=3)
        assert res == [1700.0, 1750.0, 1800.0, 1850.0]

    def test_atm_near_top_boundary(self):
        # Spot at 2300 -> above highest strike (2250). 3 below + 3 above -> capped at end
        res = filter_strikes_window(self.ALL_STRIKES, underlying_price=2300.0, strikes_below=3, strikes_above=3)
        assert res == [2100.0, 2150.0, 2200.0, 2250.0]

    def test_excessive_range_centers_around_atm(self):
        # Range with 50 strikes from 1000 to 1490. Spot at 1250. max_limit=20
        large_strikes = [float(x) for x in range(1000, 1500, 10)]  # 50 strikes
        res = filter_strikes_window(large_strikes, min_strike=1000.0, max_strike=1500.0, underlying_price=1250.0, max_limit=20)
        assert len(res) == 20
        # Check that 1250 is in the middle, not clipped to the first 20 starting at 1000
        assert 1250.0 in res
        assert min(res) > 1000.0  # Not starting from bottom

    def test_unsorted_and_duplicate_input(self):
        raw = [2000.0, 1800.0, 2000.0, 1900.0, 1800.0]
        res = filter_strikes_window(raw, min_strike=1800.0, max_strike=2000.0)
        assert res == [1800.0, 1900.0, 2000.0]


class TestSelectBestOptionChain:
    """Tests for select_best_option_chain — smart trading class ranking."""

    def test_prefers_unadjusted_hmi_over_hmi2(self):
        # Raw list has HMI2 first (corporate action adjusted) and HMI second (standard)
        chains = [
            {
                "exchange": "DTB",
                "tradingClass": "HMI2",
                "multiplier": "10",
                "expirations": ["20261016"],
                "strikes": [1400.0, 1500.0, 1600.0],
            },
            {
                "exchange": "DTB",
                "tradingClass": "HMI",
                "multiplier": "10",
                "expirations": ["20261016", "20261218"],
                "strikes": [1200.0, 1300.0, 1400.0, 1500.0, 1600.0, 1700.0, 1800.0],
            },
        ]
        # Should pick HMI automatically based on no-digits and richer strike count
        best = select_best_option_chain(chains, target_exchange="DTB", expiry="20261016")
        assert best["tradingClass"] == "HMI"

    def test_explicit_trading_class_override(self):
        chains = [
            {"exchange": "DTB", "tradingClass": "HMI", "expirations": ["20261016"], "strikes": [1500.0]},
            {"exchange": "DTB", "tradingClass": "HMI2", "expirations": ["20261016"], "strikes": [1500.0]},
        ]
        best = select_best_option_chain(chains, target_exchange="DTB", expiry="20261016", trading_class="HMI2")
        assert best["tradingClass"] == "HMI2"

    def test_case_insensitive_trading_class(self):
        chains = [
            {"exchange": "DTB", "tradingClass": "HMI", "expirations": ["20261016"], "strikes": [1500.0]},
        ]
        best = select_best_option_chain(chains, target_exchange="DTB", expiry="20261016", trading_class="hmi")
        assert best["tradingClass"] == "HMI"

    def test_prefers_richer_strike_grid_when_both_have_clean_names(self):
        chains = [
            {"exchange": "DTB", "tradingClass": "CLASS_A", "expirations": ["20261016"], "strikes": [100.0, 200.0]},
            {"exchange": "DTB", "tradingClass": "CLASS_B", "expirations": ["20261016"], "strikes": [100.0, 150.0, 200.0, 250.0, 300.0]},
        ]
        best = select_best_option_chain(chains, target_exchange="DTB", expiry="20261016")
        assert best["tradingClass"] == "CLASS_B"

    def test_fallback_exchange_when_target_missing(self):
        chains = [
            {"exchange": "SMART", "tradingClass": "AAPL", "expirations": ["20261016"], "strikes": [200.0]},
        ]
        # Target DTB not found, but SMART has the expiry -> fallback to SMART
        best = select_best_option_chain(chains, target_exchange="DTB", expiry="20261016")
        assert best["exchange"] == "SMART"

    def test_non_existent_expiry_returns_none(self):
        chains = [
            {"exchange": "DTB", "tradingClass": "HMI", "expirations": ["20261016"], "strikes": [1500.0]},
        ]
        best = select_best_option_chain(chains, target_exchange="DTB", expiry="20291231")
        assert best is None


class TestCalcBsGreeks:
    """Tests for calc_bs_greeks — Black-Scholes Greeks calculation."""

    def test_put_delta_atm(self):
        # S=1450, K=1450, 38 days, IV=32%
        res = calc_bs_greeks("P", 1450.0, 1450.0, "20261016", 0.32, eval_date="20260908")
        # ATM Put delta should be close to -0.47 to -0.50
        assert -0.55 < res["delta"] < -0.45
        assert res["gamma"] > 0.0
        assert res["theta"] < 0.0
        assert res["vega"] > 0.0

    def test_put_delta_otm_and_itm(self):
        # OTM Put: strike 1300 < spot 1450 -> delta closer to 0 (e.g. -0.15 to -0.20)
        otm_res = calc_bs_greeks("P", 1450.0, 1300.0, "20261016", 0.32, eval_date="20260908")
        assert -0.25 < otm_res["delta"] < -0.05

        # ITM Put: strike 1600 > spot 1450 -> delta closer to -1.0 (e.g. -0.75 to -0.85)
        itm_res = calc_bs_greeks("P", 1450.0, 1600.0, "20261016", 0.32, eval_date="20260908")
        assert -0.90 < itm_res["delta"] < -0.70

    def test_call_delta_atm(self):
        res = calc_bs_greeks("C", 1450.0, 1450.0, "20261016", 0.32, eval_date="20260908")
        # ATM Call delta should be close to 0.50 to 0.55
        assert 0.45 < res["delta"] < 0.55
        assert res["gamma"] > 0.0
        assert res["theta"] < 0.0
        assert res["vega"] > 0.0

    def test_zero_or_negative_inputs(self):
        assert calc_bs_greeks("P", 0.0, 1450.0, "20261016", 0.32) == {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        assert calc_bs_greeks("P", 1450.0, 0.0, "20261016", 0.32) == {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        assert calc_bs_greeks("P", 1450.0, 1450.0, "20261016", 0.0) == {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    def test_invalid_expiry(self):
        res = calc_bs_greeks("P", 1450.0, 1450.0, "invalid_date", 0.32)
        assert res == {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}


class TestIsMarketOpenForSymbol:
    """Tests for is_market_open_for_symbol across US, EU, and UK sessions."""

    def test_us_market_open_hours(self):
        from datetime import datetime, timezone
        # Wednesday 2026-09-09 14:00 UTC = 10:00 AM EDT (Open)
        dt_open = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
        assert is_market_open_for_symbol(symbol="AAPL", currency="USD", ref_dt=dt_open) is True

        # Wednesday 2026-09-09 04:00 UTC = Midnight EDT (Closed)
        dt_closed = datetime(2026, 9, 9, 4, 0, tzinfo=timezone.utc)
        assert is_market_open_for_symbol(symbol="AAPL", currency="USD", ref_dt=dt_closed) is False

        # Saturday 2026-09-12 14:00 UTC (Weekend: Closed)
        dt_weekend = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
        assert is_market_open_for_symbol(symbol="AAPL", currency="USD", ref_dt=dt_weekend) is False

    def test_eu_market_open_hours(self):
        from datetime import datetime, timezone
        # Wednesday 2026-09-09 10:00 UTC = 12:00 CEST (Open)
        dt_open = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
        assert is_market_open_for_symbol(symbol="RMS", exchange="EUREX", currency="EUR", ref_dt=dt_open) is True

        # Wednesday 2026-09-09 18:00 UTC = 20:00 CEST (Closed: closes at 17:30 CEST)
        dt_closed = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)
        assert is_market_open_for_symbol(symbol="RMS", exchange="EUREX", currency="EUR", ref_dt=dt_closed) is False

    def test_uk_market_open_hours(self):
        from datetime import datetime, timezone
        # Wednesday 2026-09-09 10:00 UTC = 11:00 BST (Open)
        dt_open = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
        assert is_market_open_for_symbol(symbol="BATS.L", exchange="LSE", currency="GBP", ref_dt=dt_open) is True

        # Wednesday 2026-09-09 17:00 UTC = 18:00 BST (Closed: closes at 16:30 BST)
        dt_closed = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)
        assert is_market_open_for_symbol(symbol="BATS.L", exchange="LSE", currency="GBP", ref_dt=dt_closed) is False


class TestDetermineMarketStatuses:
    """Tests for determine_market_statuses mapping."""

    def test_market_closed_no_quotes_with_greeks(self):
        # e.g. RMS 1580P after close: bid/ask null, greeks present from model
        res = determine_market_statuses(is_open=False, has_bid_ask=False, has_greeks=True)
        assert res["market_data_status"] == "CLOSED"
        assert res["quote_status"] == "CLOSED"
        assert res["greeks_status"] == "FROZEN"

    def test_market_closed_with_quotes_and_greeks(self):
        # e.g. Contract after close with frozen bid/ask
        res = determine_market_statuses(is_open=False, has_bid_ask=True, has_greeks=True)
        assert res["market_data_status"] == "CLOSED"
        assert res["quote_status"] == "FROZEN"
        assert res["greeks_status"] == "FROZEN"

    def test_market_closed_both_null(self):
        res = determine_market_statuses(is_open=False, has_bid_ask=False, has_greeks=False)
        assert res["market_data_status"] == "CLOSED"
        assert res["quote_status"] == "CLOSED"
        assert res["greeks_status"] == "CLOSED"

    def test_market_open_live_full(self):
        res = determine_market_statuses(is_open=True, has_bid_ask=True, has_greeks=True, market_data_type=1)
        assert res["market_data_status"] == "LIVE"
        assert res["quote_status"] == "LIVE"
        assert res["greeks_status"] == "LIVE"

    def test_market_open_live_quotes_frozen_greeks(self):
        # User example: quote_status: LIVE, greeks_status: FROZEN
        res = determine_market_statuses(is_open=True, has_bid_ask=True, has_greeks=True, greeks_are_frozen=True)
        assert res["market_data_status"] == "LIVE"
        assert res["quote_status"] == "LIVE"
        assert res["greeks_status"] == "FROZEN"

    def test_market_open_missing_quotes(self):
        res = determine_market_statuses(is_open=True, has_bid_ask=False, has_greeks=True)
        assert res["market_data_status"] == "LIVE"
        assert res["quote_status"] == "UNAVAILABLE"
        assert res["greeks_status"] == "LIVE"

    def test_market_open_missing_greeks(self):
        res = determine_market_statuses(is_open=True, has_bid_ask=True, has_greeks=False)
        assert res["market_data_status"] == "LIVE"
        assert res["quote_status"] == "LIVE"
        assert res["greeks_status"] == "UNAVAILABLE"

    def test_market_open_delayed(self):
        res = determine_market_statuses(is_open=True, has_bid_ask=True, has_greeks=True, market_data_type=3)
        assert res["market_data_status"] == "DELAYED"
        assert res["quote_status"] == "DELAYED"
        assert res["greeks_status"] == "DELAYED"

    def test_db_source_market_closed(self):
        res = determine_market_statuses(is_open=False, has_bid_ask=False, has_greeks=True, source="db")
        assert res["market_data_status"] == "CLOSED"
        assert res["quote_status"] == "CLOSED"
        assert res["greeks_status"] == "FROZEN"

    def test_cboe_source_market_open(self):
        res = determine_market_statuses(is_open=True, has_bid_ask=True, has_greeks=True, source="cboe")
        assert res["market_data_status"] == "DELAYED"
        assert res["quote_status"] == "DELAYED"
        assert res["greeks_status"] == "FROZEN"




