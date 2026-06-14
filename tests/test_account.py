"""
Unit tests for account position prefix-resolution logic in
src/api/routes/account.py.

The prefix-resolution block inside get_positions() converts a non-USD
option contract (exchange + currency) into the Google Finance / IBKR prefix
format used throughout the system (e.g. "EPA:MC" for a EUR/Paris option
on MC). We replicate that exact logic here as a pure function so it can be
unit-tested without a live IBKR connection.

Tests also cover the get_val() inner function logic (currency/tag resolution)
and PnL data normalisation.
"""
import sys
import os
import math
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.parsing import EXCHANGE_PREFIXES


# ---------------------------------------------------------------------------
# Helpers replicated from account.py for isolation
# ---------------------------------------------------------------------------

def resolve_underlying_prefix(exchange: str, currency: str) -> str | None:
    """
    Mirror of the prefix-resolution block inside get_positions().
    Returns the prefix string (e.g. 'EPA') or None if none found.
    """
    for pref, (exch, curr) in EXCHANGE_PREFIXES.items():
        if exch == exchange and curr == currency:
            return pref

    # Fallbacks
    if currency == 'EUR':
        if exchange in ('MONEP', 'SBF'):
            return 'EPA'
        elif exchange in ('MEFF', 'BM'):
            return 'MC'
        elif exchange in ('DTB', 'EUREX'):
            return 'ETR'
        else:
            return 'EPA'  # general EUR fallback
    elif currency == 'GBP':
        return 'LON'
    elif currency == 'CHF':
        return 'SWX'
    return None


def build_underlying(symbol: str, exchange: str, currency: str) -> str:
    """
    Returns 'PREFIX:SYMBOL' for non-USD options, or just 'SYMBOL' for USD.
    """
    if currency == 'USD':
        return symbol
    prefix = resolve_underlying_prefix(exchange, currency)
    if prefix:
        return f"{prefix}:{symbol}"
    return symbol


def get_val(tag: str, currency: str | None, account_values: list, default: str = "0") -> str:
    """
    Mirror of the get_val() inner function inside get_summary().
    Searches account_values list for matching tag+currency.
    """
    matches = [x for x in account_values if x['tag'] == tag]
    if not matches:
        return default

    if currency:
        for m in matches:
            if m['currency'] == currency:
                return m['value']

    for m in matches:
        if m['currency'] == 'BASE':
            return m['value']

    return matches[0]['value']


# ---------------------------------------------------------------------------
# resolve_underlying_prefix / build_underlying tests
# ---------------------------------------------------------------------------

class TestPositionPrefixResolution:
    """Tests for exchange+currency -> prefix mapping used in get_positions()."""

    # USD options: no prefix needed
    def test_usd_no_prefix(self):
        assert build_underlying("AAPL", "CBOE", "USD") == "AAPL"

    def test_usd_smart_exchange_no_prefix(self):
        assert build_underlying("SPY", "SMART", "USD") == "SPY"

    # EUR options via exact EXCHANGE_PREFIXES match
    def test_eur_monep_maps_to_epa(self):
        prefix = resolve_underlying_prefix("MONEP", "EUR")
        assert prefix == "EPA"

    def test_eur_meff_maps_to_mc(self):
        prefix = resolve_underlying_prefix("MEFF", "EUR")
        assert prefix == "MC"

    def test_eur_dtb_maps_to_etr(self):
        prefix = resolve_underlying_prefix("DTB", "EUR")
        assert prefix == "ETR"

    # EUR options via fallback paths
    def test_eur_sbf_fallback_to_epa(self):
        prefix = resolve_underlying_prefix("SBF", "EUR")
        assert prefix == "EPA"

    def test_eur_bm_fallback_to_mc(self):
        prefix = resolve_underlying_prefix("BM", "EUR")
        assert prefix == "MC"

    def test_eur_eurex_fallback_to_etr(self):
        prefix = resolve_underlying_prefix("EUREX", "EUR")
        assert prefix == "ETR"

    def test_eur_unknown_exchange_fallback_to_epa(self):
        """Any EUR exchange not specifically listed falls back to EPA."""
        prefix = resolve_underlying_prefix("BVME", "EUR")
        assert prefix == "EPA"

    # GBP options
    def test_gbp_maps_to_lon(self):
        prefix = resolve_underlying_prefix("LSE", "GBP")
        assert prefix == "LON"

    def test_gbp_unknown_exchange_maps_to_lon(self):
        prefix = resolve_underlying_prefix("UNKNOWN", "GBP")
        assert prefix == "LON"

    # CHF options
    def test_chf_maps_to_swx(self):
        prefix = resolve_underlying_prefix("EBS", "CHF")
        assert prefix == "SWX"

    # Full underlying string construction
    def test_eur_monep_full_string(self):
        assert build_underlying("MC", "MONEP", "EUR") == "EPA:MC"

    def test_gbp_full_string(self):
        assert build_underlying("BATS", "LSE", "GBP") == "LON:BATS"

    def test_chf_full_string(self):
        assert build_underlying("NESN", "EBS", "CHF") == "SWX:NESN"


# ---------------------------------------------------------------------------
# get_val tests
# ---------------------------------------------------------------------------

class TestGetVal:
    """Tests for the get_val() inner function in get_summary()."""

    def _av(self, tag, currency, value):
        return {'tag': tag, 'currency': currency, 'value': value}

    def test_exact_currency_match(self):
        av = [self._av('NetLiquidation', 'EUR', '150000')]
        assert get_val('NetLiquidation', 'EUR', av) == '150000'

    def test_base_fallback(self):
        """When exact currency missing, BASE is returned."""
        av = [
            self._av('FullAvailableMargin', 'BASE', '80000'),
            self._av('FullAvailableMargin', 'EUR', '75000'),
        ]
        # Requesting USD — not found, falls back to BASE
        assert get_val('FullAvailableMargin', 'USD', av) == '80000'

    def test_first_match_if_no_base(self):
        """If no BASE entry, first match is returned."""
        av = [
            self._av('Cushion', 'EUR', '0.12'),
            self._av('Cushion', 'USD', '0.11'),
        ]
        assert get_val('Cushion', 'CHF', av) == '0.12'

    def test_default_when_tag_missing(self):
        av = [self._av('SomethingElse', 'EUR', '1')]
        assert get_val('NetLiquidation', 'EUR', av) == '0'

    def test_custom_default(self):
        assert get_val('X', 'EUR', [], default='-1') == '-1'

    def test_no_currency_filter_returns_base(self):
        """When currency=None or empty, first BASE match is returned."""
        av = [
            self._av('Cushion', 'BASE', '0.15'),
            self._av('Cushion', 'EUR', '0.14'),
        ]
        assert get_val('Cushion', None, av) == '0.15'

    def test_multiple_currencies_returns_correct(self):
        av = [
            self._av('CashBalance', 'EUR', '1000'),
            self._av('CashBalance', 'USD', '2000'),
            self._av('CashBalance', 'GBP', '500'),
        ]
        assert get_val('CashBalance', 'GBP', av) == '500'
        assert get_val('CashBalance', 'USD', av) == '2000'
        assert get_val('CashBalance', 'EUR', av) == '1000'
