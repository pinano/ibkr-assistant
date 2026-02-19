# Market suffix mappings for international stocks
MARKET_SUFFIXES = {
    ".L": ("LSE", "GBP"),      # London Stock Exchange
    ".DE": ("IBIS", "EUR"),    # Germany (Xetra)
    ".PA": ("SBF", "EUR"),     # France (Euronext Paris)
    ".AS": ("AEB", "EUR"),     # Netherlands (Amsterdam)
    ".SW": ("EBS", "CHF"),     # Switzerland
    ".MC": ("BM", "EUR"),      # Spain (Madrid)
    ".MI": ("BVME", "EUR"),    # Italy (Milan)
}

# Exchange Prefix Mapping (Google Finance / Yahoo Finance style -> IBKR)
EXCHANGE_PREFIXES = {
    "EPA": ("MONEP", "EUR"),    # Paris -> MONEP
    "AMS": ("FTA", "EUR"),      # Amsterdam -> FTA
    "ETR": ("DTB", "EUR"),      # Xetra -> DTB (Eurex)
    "FRA": ("DTB", "EUR"),      # Frankfurt -> DTB
    "LON": ("LSE", "GBP"),      # London
    "SWX": ("EBS", "CHF"),      # SWX -> EBS (Swiss)
    "MC":  ("MEFF", "EUR"),     # Madrid -> MEFF
    "MCE": ("MEFF", "EUR"),     # Madrid (alternative)
}
