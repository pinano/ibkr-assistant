import asyncio
import logging
from ib_async import Option

logger = logging.getLogger("ibkr-api")


async def _qualify_option_contract(
    client,
    ticker: str,
    expiry: str,
    strike: float,
    right: str,
    currency: str = "USD",
    exchange: str = "SMART"
):
    """
    Attempt to qualify an option contract using multiple strategies:
    1. Exact match with provided currency.
    2. Exact match with alternative currencies (USD, EUR, GBP, CHF).
    3. Dynamic symbol search (reqMatchingSymbols) to find correct exchange/currency.

    Returns:
        List of qualified contracts (or empty/None if failed).
    """
    # 1. Try with provided currency (default or parsed)
    contract = Option(
        ticker,
        expiry,
        strike,
        right,
        exchange,
        currency=currency)

    qualified = None  # Initialize to prevent UnboundLocalError if try block raises
    try:
        qualified = await client.qualifyContractsAsync(contract)
        if qualified and qualified[0]:
            return qualified
    except Exception:
        pass

    # 1b. Retry with multiplier='100' if initial failed (resolves ambiguity for EU options)
    if not qualified or not qualified[0]:
        try:
            contract_100 = Option(
                ticker,
                expiry,
                strike,
                right,
                exchange,
                currency=currency,
                multiplier='100'
            )
            qualified = await client.qualifyContractsAsync(contract_100)
            if qualified and qualified[0]:
                logger.info(f"Qualified option {ticker} via multiplier='100'")
                return qualified
        except Exception:
            pass

    # 2. Try alternative currencies in parallel
    alt_currencies = [
        c for c in ['USD', 'EUR', 'GBP', 'CHF']
        if c != currency
    ]

    tasks = []
    for alt in alt_currencies:
        c = Option(
            ticker,
            expiry,
            strike,
            right,
            'SMART',
            currency=alt)
        tasks.append(client.qualifyContractsAsync(c))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, list) and res and res[0]:
                logger.info(f"Qualified option {ticker} via fallback currency={res[0].currency}")
                return res

    # 3. Dynamic Fallback: Search for the underlying symbol
    logger.info(f"Performing dynamic symbol search for {ticker}...")
    try:
        descriptions = await client.reqMatchingSymbolsAsync(ticker)

        best_match = None
        for d in descriptions:
            if d.contract.secType == 'STK' and d.contract.symbol == ticker:
                best_match = d.contract
                break

        if best_match:
            logger.info(f"Found match: {best_match.symbol} on {best_match.primaryExchange or best_match.exchange} ({best_match.currency})")
            c = Option(
                ticker,
                expiry,
                strike,
                right,
                'SMART',
                currency=best_match.currency)
            q = await client.qualifyContractsAsync(c)
            if q and q[0]:
                return q
    except Exception as e:
        logger.warning(f"Dynamic lookup failed for {ticker}: {e}")

    return None
