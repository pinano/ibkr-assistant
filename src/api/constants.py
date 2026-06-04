# src/api/constants.py
# Re-exports from src.parsing for backward compatibility.
# The authoritative definitions live in src/parsing.py.
from src.parsing import MARKET_SUFFIXES, EXCHANGE_PREFIXES

__all__ = ["MARKET_SUFFIXES", "EXCHANGE_PREFIXES"]
