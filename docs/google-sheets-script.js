// --- IBKR API Configuration ---
const IBKR_API_URL = 'API_URL';
const IBKR_API_KEY = 'API_KEY'; // <-- Set your API_KEY here

// --- Cache Configuration ---
// CacheService persists across custom function invocations (up to 6 hours).
// A global `const` object does NOT persist because each custom function call
// runs in a fresh execution context in Google Apps Script.
const CACHE_TTL = 300; // seconds (5 minutes)
const IBKR_API_TIMEOUT = 25; // seconds (API has internal 20s timeout)

/**
 * GETOPTIONDATA
 * @author pinano (farroyo@gmail.com)
 *
 * Fetches option data from CBOE (US options) or from
 * the IBKR API (European options not listed on CBOE).
 *
 * @param {string} param1 - Full OCC string or underlying ticker.
 * @param {string} param2 - Expiration date (YYMMDD) when passing 4 params.
 * @param {string} param3 - Type (C or P) when passing 4 params.
 * @param {number} param4 - Strike when passing 4 params.
 * @return {Array} Row with [delta, gamma, theta, vega, iv, open_interest, volume, last_trade_price, last_trade_time].
 * @customfunction
 */
function GETOPTIONDATA(param1, param2, param3, param4) {
    let ticker, expDate, type, strike, formattedStrike;

    try {
        if (arguments.length === 1) {
            // Case 1: Full OCC search string.
            // Supports tickers with dots (e.g. BRK.B) and exchange prefixes (e.g. EPA:MC)
            const regex = /^([A-Z][A-Z0-9.:]+?)(\d{6})([CP])(\d{8})$/;
            const match = param1.match(regex);

            if (!match) return [['Error: Invalid string format.']];

            ticker = match[1];
            expDate = match[2];
            type = match[3];
            formattedStrike = match[4];

        } else if (arguments.length === 4) {
            // Case 2: 4 separate parameters.
            ticker = String(param1).trim();
            expDate = String(param2).trim();
            type = String(param3).trim().toUpperCase();
            strike = Number(param4);

            if (type !== 'C' && type !== 'P') {
                return [['Error: Type must be C or P.']];
            }
            if (isNaN(strike) || strike <= 0) {
                return [['Error: Invalid strike value.']];
            }

            const strikeValue = Math.round(strike * 1000);
            formattedStrike = String(strikeValue).padStart(8, '0');

        } else {
            return [['Error: Use 1 param (OCC string) or 4 params (ticker, expDate, type, strike).']];
        }

        // --- Try CBOE first (US options) ---
        const cboeResult = _fetchFromCBOE(ticker, expDate, type, formattedStrike);
        if (cboeResult) return [cboeResult];

        // --- Fallback: IBKR API (European & other options) ---
        const ibkrResult = _fetchFromIBKR(ticker, expDate, type, formattedStrike);
        if (ibkrResult) return [ibkrResult];

        return [['Option not found on CBOE or IBKR.']];

    } catch (e) {
        return [['Error: ' + e.message]];
    }
}


/**
 * GETOPTIONBATCH
 * @author pinano
 *
 * Fetches option data for multiple options in a single call.
 * More efficient than calling GETOPTIONDATA once per row because it
 * reuses the cached CBOE JSON across tickers.
 *
 * @param {Array} range - A Nx4 range where each row is [ticker, expDate, type, strike].
 * @return {Array} Nx9 array of results.
 * @customfunction
 */
function GETOPTIONBATCH(range) {
    if (!Array.isArray(range) || range.length === 0) {
        return [['Error: Pass a Nx4 range.']];
    }

    return range.map(function (row) {
        if (!row || row.length < 4 || !row[0]) {
            return ['', '', '', '', '', '', '', '', ''];
        }
        try {
            var result = GETOPTIONDATA(row[0], row[1], row[2], row[3]);
            return result[0];
        } catch (e) {
            return ['Error: ' + e.message, '', '', '', '', '', '', '', ''];
        }
    });
}


/**
 * Tries to fetch data from CBOE. Returns null if the ticker is not available on CBOE.
 * Uses CacheService to persist CBOE JSON across custom function invocations.
 */
function _fetchFromCBOE(ticker, expDate, type, formattedStrike) {
    try {
        // Tickers with dots, colons, or exchange prefixes are not US — skip CBOE
        if (ticker.indexOf(':') !== -1) return null;

        let json;
        const cacheKey = 'cboe_' + ticker;
        const cache = CacheService.getScriptCache();
        const cached = cache.get(cacheKey);

        if (cached) {
            json = JSON.parse(cached);
        } else {
            const INDICES = ['XSP', 'SPX', 'VIX', 'DJX', 'RUT', 'NDX', 'OEX'];
            let urlTicker = ticker;
            if (INDICES.includes(ticker)) {
                urlTicker = '_' + ticker;
            }

            const url = 'https://cdn.cboe.com/api/global/delayed_quotes/options/' + urlTicker + '.json';
            const response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });

            if (response.getResponseCode() !== 200) {
                // Cache the "not found" result briefly to avoid repeated 404s
                cache.put(cacheKey, 'null', 60);
                return null;
            }

            json = JSON.parse(response.getContentText());

            // Cache the full JSON. CacheService has a 100KB limit per key.
            // For very large chains, this may fail silently — that's OK, we just skip caching.
            try {
                cache.put(cacheKey, response.getContentText(), CACHE_TTL);
            } catch (e) {
                // Value too large for cache — ignore
            }
        }

        if (!json || json === 'null' || !json.data || !Array.isArray(json.data.options)) {
            return null;
        }

        // Build the OCC option ID: remove dots from ticker for matching
        const cleanTicker = ticker.replace(/\./g, '');
        const optionId = cleanTicker + expDate + type + formattedStrike;
        const optionData = json.data.options.find(function (option) {
            return option.option === optionId;
        });

        if (!optionData) return null;

        // Return: [delta, gamma, theta, vega, iv, open_interest, volume, last_trade_price, last_trade_time]
        // Note: CBOE returns negative delta for puts and negative theta.
        // We return absolute values for consistency with portfolio usage.
        return [
            _absNum(optionData.delta),         // delta (absolute)
            _toNum(optionData.gamma),          // gamma
            _absNum(optionData.theta),         // theta (absolute)
            //_toNum(optionData.vega),           // vega
            _toNum(optionData.iv),             // implied volatility
            _toNum(optionData.open_interest),  // open interest
            _toNum(optionData.volume),         // volume
            _toNum(optionData.last_trade_price), // last trade price
            optionData.last_trade_time ? new Date(optionData.last_trade_time) : null // last trade time
        ];

    } catch (e) {
        // If CBOE fails for any reason, return null to try IBKR
        return null;
    }
}


/**
 * Fetches data from the IBKR API. Returns null on failure.
 * Converts expDate from YYMMDD (OCC format) to YYYYMMDD (IBKR API format).
 */
function _fetchFromIBKR(ticker, expDate, type, formattedStrike) {
    try {
        if (!IBKR_API_KEY || IBKR_API_KEY === 'API_KEY') {
            return null; // API key not configured
        }
        if (!IBKR_API_URL || IBKR_API_URL === 'API_URL') {
            return null; // API URL not configured
        }

        // Generate cache key
        const cacheKey = 'ibkr_' + ticker + '_' + expDate + '_' + type + '_' + formattedStrike;
        const cache = CacheService.getScriptCache();
        const cached = cache.get(cacheKey);

        if (cached) {
            if (cached === 'null') return null;

            const parsed = JSON.parse(cached);
            // Restore Date object from JSON string (index 7 is lastDate)
            if (parsed && parsed.length > 7 && parsed[7]) {
                parsed[7] = new Date(parsed[7]);
            }
            return parsed;
        }

        // Convert expDate YYMMDD -> YYYYMMDD
        const year = '20' + expDate.substring(0, 2);
        const month = expDate.substring(2, 4);
        const day = expDate.substring(4, 6);
        const expiryYYYYMMDD = year + month + day;

        // Convert formattedStrike (8 digits, x1000) -> float
        const strikeFloat = parseInt(formattedStrike, 10) / 1000;

        const right = type; // C or P

        const params = [
            'underlying=' + encodeURIComponent(ticker),
            'expiry=' + expiryYYYYMMDD,
            'strike=' + strikeFloat,
            'right=' + right
        ].join('&');

        const url = IBKR_API_URL + '/option/greeks?' + params;

        const response = UrlFetchApp.fetch(url, {
            method: 'get',
            headers: { 'X-API-Key': IBKR_API_KEY },
            muteHttpExceptions: true,
            // UrlFetchApp timeout is in seconds (Apps Script max is 30s)
            // The API itself has a 20s internal timeout, so 25s gives enough margin.
            // Note: UrlFetchApp doesn't support a `timeout` option natively,
            // but calls will timeout at ~30s by default (Apps Script URL fetch limit).
        });

        if (response.getResponseCode() !== 200) {
            try { cache.put(cacheKey, 'null', 60); } catch (e) { }
            return null;
        }

        let data;
        try {
            data = JSON.parse(response.getContentText());
        } catch (e) {
            try { cache.put(cacheKey, 'null', 60); } catch (err) { }
            return null; // Invalid JSON
        }

        // Parse last_date safely — the API returns "YYYY-MM-DD HH:MM:SS" or null
        let lastDate = null;
        if (data.last_date) {
            try {
                // Apps Script's Date parser handles "YYYY-MM-DD HH:MM:SS" well
                lastDate = new Date(data.last_date);
                if (isNaN(lastDate.getTime())) lastDate = null;
            } catch (e) {
                lastDate = null;
            }
        }

        // Map the IBKR response to the same column order as CBOE:
        // [delta, gamma, theta, vega, iv, open_interest, volume, last_trade_price, last_trade_time]
        const result = [
            Math.abs(data.delta || 0),       // delta (absolute value, same as CBOE)
            data.gamma || 0,                  // gamma
            Math.abs(data.theta || 0),        // theta (absolute value)
            //data.vega || 0,                   // vega
            data.implied_vol || 0,            // iv (implied volatility)
            data.open_interest || 0,          // open_interest
            data.volume || 0,                 // volume
            data.last_price || 0,             // last_trade_price
            lastDate                          // last_trade_time
        ];

        try {
            cache.put(cacheKey, JSON.stringify(result), CACHE_TTL);
        } catch (e) {
            // Ignore cache size errors
        }

        return result;

    } catch (e) {
        return null;
    }
}


/**
 * GETFXRATE
 * @author pinano (farroyo@gmail.com)
 *
 * Fetches the live exchange rate for a currency pair from the IBKR API.
 * Useful as a fallback when GOOGLEFINANCE("EURUSD") fails.
 *
 * @param {string} pair - The currency pair (e.g., "EURUSD" or "GBPUSD").
 * @return {number|string} The exchange rate or an error message.
 * @customfunction
 */
function GETFXRATE(pair) {
    try {
        if (!IBKR_API_KEY || IBKR_API_KEY === 'API_KEY') {
            return 'Error: API key not configured.';
        }

        const cleanPair = String(pair).replace(/[^A-Z]/ig, "").toUpperCase();
        if (cleanPair.length !== 6) {
            return 'Error: Invalid pair format. Use "EURUSD".';
        }

        const url = IBKR_API_URL + '/market/snapshot/' + cleanPair;

        const response = UrlFetchApp.fetch(url, {
            method: 'get',
            headers: { 'X-API-Key': IBKR_API_KEY },
            muteHttpExceptions: true
        });

        if (response.getResponseCode() !== 200) {
            let errorMsg = 'API request failed';
            try {
                const errorData = JSON.parse(response.getContentText());
                errorMsg = errorData.detail || errorMsg;
            } catch (e) {
                errorMsg = 'API Error (' + response.getResponseCode() + ')';
            }
            return 'Error: ' + errorMsg;
        }

        let data;
        try {
            data = JSON.parse(response.getContentText());
        } catch (e) {
            return 'Error: Invalid JSON response from API';
        }
        return (data.price !== null && data.price !== undefined)
            ? data.price
            : 'Error: No price data available';

    } catch (e) {
        return 'Error: ' + e.message;
    }
}


// --- Helper functions ---

/** Convert value to number, returning 0 if not numeric. */
function _toNum(value) {
    if (value === null || value === undefined) return 0;
    const n = typeof value === 'string' ? parseFloat(value) : value;
    return isNaN(n) ? 0 : n;
}

/** Convert to absolute number, returning 0 if not numeric. */
function _absNum(value) {
    return Math.abs(_toNum(value));
}
