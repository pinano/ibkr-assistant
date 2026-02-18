// Global variable to cache API data during a single execution.
const _CBOE_CACHE = {};

// --- IBKR API Configuration ---
const IBKR_API_URL = 'https://ib1.pinano.org';
const IBKR_API_KEY = '6c75b860e9736d08018f70f1c2b2d2ceb23f99b64f9b025c4c764b541d457e0f'; // <-- Set your API_KEY here

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
 * @return {Array} Row with [delta, gamma, theta, iv, open_interest, volume, last_trade_price, last_trade_time].
 * @customfunction
 */
function GETOPTIONDATA(param1, param2, param3, param4) {
    let ticker, expDate, type, strike, formattedStrike;

    try {
        if (arguments.length === 1) {
            // Case 1: Full OCC search string.
            const regex = /^([A-Z\.]+)(\d{6})([CP])(\d{8})$/;
            const match = param1.match(regex);

            if (!match) return [['Error: Invalid string format.']];

            ticker = match[1];
            expDate = match[2];
            type = match[3];
            formattedStrike = match[4];

        } else if (arguments.length === 4) {
            // Case 2: 4 separate parameters.
            ticker = param1;
            expDate = param2;
            type = param3;
            strike = param4;

            const strikeValue = Math.round(strike * 1000);
            formattedStrike = String(strikeValue).padStart(8, '0');

        } else {
            return [['Error: Incorrect number of parameters.']];
        }

        // --- Try CBOE first ---
        const cboeResult = _fetchFromCBOE(ticker, expDate, type, formattedStrike);
        if (cboeResult) return [cboeResult];

        // --- Fallback: IBKR API for European options ---
        const ibkrResult = _fetchFromIBKR(ticker, expDate, type, formattedStrike);
        if (ibkrResult) return [ibkrResult];

        return [['Option not found on CBOE or IBKR.']];

    } catch (e) {
        return [['Error: ' + e.message]];
    }
}


/**
 * Tries to fetch data from CBOE. Returns null if the ticker is not available on CBOE.
 */
function _fetchFromCBOE(ticker, expDate, type, formattedStrike) {
    try {
        let json;

        if (_CBOE_CACHE[ticker]) {
            json = _CBOE_CACHE[ticker];
        } else {
            const INDICES = ['XSP', 'SPX', 'VIX', 'DJX', 'RUT', 'NDX', 'OEX'];
            let urlTicker = ticker;
            if (INDICES.includes(ticker)) {
                urlTicker = '_' + ticker;
            }

            const url = `https://cdn.cboe.com/api/global/delayed_quotes/options/${urlTicker}.json`;
            const response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });

            if (response.getResponseCode() !== 200) {
                return null; // Ticker not available on CBOE
            }

            json = JSON.parse(response.getContentText());
            _CBOE_CACHE[ticker] = json;
        }

        if (!json || !json.data || !Array.isArray(json.data.options)) {
            return null;
        }

        const cleanTicker = ticker.replace(/\./g, '');
        const optionId = `${cleanTicker}${expDate}${type}${formattedStrike}`;
        const optionData = json.data.options.find(option => option.option === optionId);

        if (!optionData) return null;

        const desiredKeys = ['delta', 'gamma', 'theta', 'iv', 'open_interest', 'volume', 'last_trade_price', 'last_trade_time'];

        return desiredKeys.map(key => {
            let value = optionData[key];

            if (key === 'last_trade_time' && value) {
                return new Date(value);
            }

            if ((key === 'delta' || key === 'theta') && typeof value === 'number' && value < 0) {
                return Math.abs(value);
            }

            return typeof value === 'string' && !isNaN(parseFloat(value)) ? parseFloat(value) : value;
        });

    } catch (e) {
        // If CBOE fails for any reason, return null to try IBKR
        return null;
    }
}


/**
 * Fetches data from the IBKR API. Returns null on failure.
 *
 * Converts expDate from YYMMDD (OCC format) to YYYYMMDD (IBKR API format).
 */
function _fetchFromIBKR(ticker, expDate, type, formattedStrike) {
    try {
        if (!IBKR_API_KEY) {
            return null; // API key not configured
        }

        // Convert expDate YYMMDD -> YYYYMMDD
        const year = '20' + expDate.substring(0, 2);
        const month = expDate.substring(2, 4);
        const day = expDate.substring(4, 6);
        const expiryYYYYMMDD = year + month + day;

        // Convert formattedStrike (8 digits, x1000) -> float
        const strikeFloat = parseInt(formattedStrike, 10) / 1000;

        // Suffix mapping for European tickers (e.g. RMS.PA, DGE.L)
        // The incoming ticker may or may not include the suffix.
        // The API accepts the ticker with suffix and resolves it via parse_symbol.
        const right = type; // C or P

        const params = [
            `underlying=${encodeURIComponent(ticker)}`,
            `expiry=${expiryYYYYMMDD}`,
            `strike=${strikeFloat}`,
            `right=${right}`
        ].join('&');

        const url = `${IBKR_API_URL}/option/greeks?${params}`;

        const response = UrlFetchApp.fetch(url, {
            method: 'get',
            headers: { 'X-API-Key': IBKR_API_KEY },
            muteHttpExceptions: true
        });

        if (response.getResponseCode() !== 200) {
            return null;
        }

        const data = JSON.parse(response.getContentText());

        // Map the IBKR response to the same order as CBOE:
        // [delta, gamma, theta, iv, open_interest, volume, last_trade_price, last_trade_time]
        return [
            Math.abs(data.delta || 0),       // delta (absolute value, same as CBOE)
            data.gamma || 0,                  // gamma
            Math.abs(data.theta || 0),        // theta (absolute value)
            data.implied_vol || 0,            // iv (implied volatility)
            data.open_interest || 0,          // open_interest
            data.volume || 0,                 // volume
            data.last_price || 0,             // last_trade_price
            data.last_date ? new Date(data.last_date) : null  // last_trade_time
        ];

    } catch (e) {
        return null;
    }
}
