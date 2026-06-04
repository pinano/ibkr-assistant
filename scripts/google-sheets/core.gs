/**
 * core.gs
 * Unified engine for fetching data (APIs) and batch updating the sheet.
 * Includes Greeks, Quotes, and FX logic.
 * @author pinano
 */

// --- 1. GLOBAL CONFIGURATIONS ---

const IBKR_CONFIG = {
  API_URL: 'https://ib.mydomain.com',
  // IMPORTANT: If API_KEY is empty, the script will skip fetching European 
  // options (tickers containing ':') and mark them as unavailable ('--').
  API_KEY: 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
  CACHE_TTL: 300 // Seconds (5 minutes)
};

const CBOE_CONFIG = {
  CACHE_TTL: 60, // Seconds (1 minute)
  // Tickers that require an underscore prefix in the CBOE API endpoint
  INDEX_TICKERS: ['XSP', 'SPX', 'VIX', 'DJX', 'RUT', 'NDX', 'OEX'],
  TZ_OFFSET: (() => {
    // Detect NY offset dynamically to handle DST (Daylight Saving Time) changes automatically.
    const offset = Utilities.formatDate(new Date(), "America/New_York", "Z");
    return `${offset.slice(0, 3)}:${offset.slice(3)}`;
  })()
};

const GREEKS_CONFIG = {
  START_ROW:     3,
  COL_OCC:       36, // Column AJ (Input: OCC String)
  COL_IS_CLOSED: 38, // Column AL (Filter: Boolean)
  COL_RESULT:    16, // Column P (Output: 8 columns from P to W)
  NUM_COLS:      8,  // [Delta, Gamma, Theta, IV, OI, Vol, Price, Time]
};

const QUOTE_CONFIG = {
  START_ROW:      3,
  COL_OPERATION:  5,   // Column E (Input: Operation type)
  COL_TICKER:     6,   // Column F (Input: Underlying ticker)
  COL_IS_CLOSED:  38,  // Column AL (Filter: Boolean)
  COL_RESULT:     8,   // Column H (Output: Last Price)
};

const FX_CONFIG = {
  START_ROW:      3,
  COL_CURRENCY:   12, // Column L (Input: Currency symbol $, £, kr, €)
  COL_CLOSE_DATE: 3,  // Column C (Input: Historical date for closed trades)
  COL_IS_CLOSED:  38, // Column AL (Filter: Boolean)
  COL_RESULT:     13, // Column M (Output: FX Rate)
  
  // BEHAVIOR FOR CLOSED POSITIONS:
  // -------------------------------------------------------------------------
  // When false (default): Protects data for closed positions.
  //   - If a rate is already present in Column M, it is preserved.
  //   - If Column M is empty, it fetches the historical rate once and stores it permanently.
  //
  // When true (backfill mode): Re-calculates rates for all closed positions.
  //   - Always fetches the historical rate based on the close date (Column C).
  //   - Historical rates are stored in permanent cache (PropertiesService) to avoid 
  //     redundant API calls. Use this to fill gaps in old records.
  BACKFILL_CLOSED: false,
};

// Global constants for standardized row outputs
const EMPTY_ROW    = ['', '', '', '', '', '', '', ''];
const EUROPEAN_ROW = ['--', '', '', '', '', '', '', ''];


// --- 2. BATCH UPDATER FUNCTIONS ---

/**
 * updateGreeks
 * High-performance updater for option Greeks.
 * Filters out closed positions and leverages parallel API fetching.
 */
function updateGreeks() {
  const startTime = new Date();
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(UI_CONFIG.SHEET_NAME);
  if (!sheet) return;

  try {
    const lastRow = sheet.getLastRow();
    const numRows = lastRow - GREEKS_CONFIG.START_ROW + 1;

    if (numRows > 0) {
      // Batch read inputs
      const occValues     = sheet.getRange(GREEKS_CONFIG.START_ROW, GREEKS_CONFIG.COL_OCC,      numRows, 1).getValues();
      const closedValues  = sheet.getRange(GREEKS_CONFIG.START_ROW, GREEKS_CONFIG.COL_IS_CLOSED, numRows, 1).getValues();
      const currentValues = sheet.getRange(GREEKS_CONFIG.START_ROW, GREEKS_CONFIG.COL_RESULT,   numRows, GREEKS_CONFIG.NUM_COLS).getValues();

      // Fetch fresh data in a single parallel operation
      const freshResults = GETOPTIONBATCH(occValues, closedValues);

      // Process results: handle closed trades and API failures
      const finalResults = freshResults.map((newRow, i) => {
        const isClosed = closedValues[i][0] === true;
        if (isClosed) return EMPTY_ROW.slice();

        const occStr = String(occValues[i][0] || '').trim();
        if (!occStr || occStr === '--') return newRow;

        // If the fetch returned empty data (likely API timeout), keep the existing value in the sheet
        const fetchFailed = newRow.every(v => v === '' || v === 0 || v === null);
        return fetchFailed ? currentValues[i] : newRow;
      });

      // Batch write results back to the sheet
      sheet.getRange(GREEKS_CONFIG.START_ROW, GREEKS_CONFIG.COL_RESULT, numRows, GREEKS_CONFIG.NUM_COLS).setValues(finalResults);
    }
  } catch (e) {
    Logger.log(`Error in updateGreeks: ${e.message}`);
    SpreadsheetApp.getActive().toast(`Greeks Error: ${e.message}`, '❌ Error');
  } finally {
    // Always update execution timestamp in Column P
    sheet.getRange('P2').setValue(_formatTimestamp(startTime));
  }
}

/**
 * updateQuotes
 * Updates underlying stock prices using Google Finance formulas.
 */
function updateQuotes() {
  const startTime = new Date();
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(UI_CONFIG.SHEET_NAME);
  if (!sheet) return;

  try {
    const lastRow = sheet.getLastRow();
    const numRows = lastRow - QUOTE_CONFIG.START_ROW + 1;

    if (numRows > 0) {
      const data = sheet.getRange(QUOTE_CONFIG.START_ROW, 1, numRows, QUOTE_CONFIG.COL_IS_CLOSED).getValues();

      // Optimize: Only fetch prices for active short positions (VENTA)
      const tickersToFetch = [...new Set(
        data
          .filter(row => {
            const isClosed  = row[QUOTE_CONFIG.COL_IS_CLOSED - 1];
            const operation = String(row[QUOTE_CONFIG.COL_OPERATION - 1]).trim().toUpperCase();
            const ticker    = String(row[QUOTE_CONFIG.COL_TICKER - 1]).trim();
            return !isClosed && operation.includes('VENTA') && ticker !== '';
          })
          .map(row => String(row[QUOTE_CONFIG.COL_TICKER - 1]).trim())
      )];

      const priceMap = tickersToFetch.length > 0 ? _fetchPricesViaGoogleFinance(tickersToFetch) : {};

      const results = data.map(row => {
        const isClosed    = row[QUOTE_CONFIG.COL_IS_CLOSED - 1];
        const operation   = String(row[QUOTE_CONFIG.COL_OPERATION - 1]).trim().toUpperCase();
        const ticker      = String(row[QUOTE_CONFIG.COL_TICKER - 1]).trim();
        const currentPrice = row[QUOTE_CONFIG.COL_RESULT - 1];

        if (isClosed)                    return ['--'];
        if (operation.includes('VENTA')) return [priceMap[ticker] || currentPrice || '--'];
        return ['--'];
      });

      sheet.getRange(QUOTE_CONFIG.START_ROW, QUOTE_CONFIG.COL_RESULT, numRows, 1).setValues(results);
    }
  } catch (e) {
    Logger.log(`Error in updateQuotes: ${e.message}`);
    SpreadsheetApp.getActive().toast(`Quotes Error: ${e.message}`, '❌ Error');
  } finally {
    // Always update execution timestamp in Column H
    sheet.getRange('H2').setValue(_formatTimestamp(startTime));
  }
}

/**
 * updateFX
 * Updates exchange rates with historical awareness and permanent caching.
 */
function updateFX() {
  const startTime = new Date();
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(UI_CONFIG.SHEET_NAME);
  if (!sheet) return;

  try {
    const lastRow = sheet.getLastRow();
    const numRows = lastRow - FX_CONFIG.START_ROW + 1;

    if (numRows > 0) {
      const data = sheet.getRange(FX_CONFIG.START_ROW, 1, numRows, FX_CONFIG.COL_IS_CLOSED).getValues();

      const results = data.map(row => {
        const currency   = String(row[FX_CONFIG.COL_CURRENCY - 1]).trim();
        const closeDate  = row[FX_CONFIG.COL_CLOSE_DATE - 1];
        const isClosed   = row[FX_CONFIG.COL_IS_CLOSED - 1];
        const currentVal = row[FX_CONFIG.COL_RESULT - 1];
        const closeDateObj = closeDate instanceof Date ? closeDate : null;

        // EUR to EUR is always 1
        if (!currency || currency === '' || currency === '1') return [1];

        if (isClosed === true) {
          if (!FX_CONFIG.BACKFILL_CLOSED) {
            // Mode: Preserve - If value exists, don't touch it.
            if (typeof currentVal === 'number' && currentVal > 0) return [currentVal];
            return [_getHistoricalFxRate(currency, closeDateObj) || 1];
          }
          // Mode: Backfill - Always re-fetch historical rate for the close date.
          return [_getHistoricalFxRate(currency, closeDateObj) || currentVal || 1];
        }

        // Live rate for open positions
        return [GETFXRATE_OPTIMIZED(currency, null) || currentVal || 1];
      });

      sheet.getRange(FX_CONFIG.START_ROW, FX_CONFIG.COL_RESULT, numRows, 1).setValues(results);
    }
  } catch (e) {
    Logger.log(`Error in updateFX: ${e.message}`);
    SpreadsheetApp.getActive().toast(`FX Error: ${e.message}`, '❌ Error');
  } finally {
    // Always update execution timestamp in Column L (Header/Info)
    sheet.getRange('L2').setValue(_formatTimestamp(startTime));
  }
}


// --- 3. CORE API LOGIC (GETOPTIONDATA / GETOPTIONBATCH) ---

/**
 * GETOPTIONDATA
 * Convenience function for single option lookup.
 * @customfunction
 */
function GETOPTIONDATA(param1, param2, param3, param4) {
  try {
    let ticker, expDate, type, formattedStrike;

    // Handle full OCC string (single argument)
    if (arguments.length === 1) {
      const regex = /^([A-Z][A-Z0-9.:]*)(\d{6})([CP])(\d{8})$/;
      const match = String(param1).trim().match(regex);
      if (!match) return [['Error: Invalid OCC format']];
      [ , ticker, expDate, type, formattedStrike] = match;
    } 
    // Handle separate parameters
    else if (arguments.length === 4) {
      ticker = String(param1).trim();
      expDate = String(param2).trim();
      type = String(param3).trim().toUpperCase();
      const strike = Number(param4);
      if (!['C', 'P'].includes(type) || isNaN(strike) || strike <= 0) return [['Error: Invalid params']];
      formattedStrike = String(Math.round(strike * 1000)).padStart(8, '0');
    } else {
      return [['Error: Invalid args']];
    }

    const result = _fetchOption(ticker, expDate, type, formattedStrike);
    return result ? [result] : [EMPTY_ROW.slice()];
  } catch (e) {
    return [[`Error: ${e.message}`]];
  }
}

/**
 * GETOPTIONBATCH
 * Central intelligence for fetching multiple options efficiently.
 * Handles caching, parallel fetching, and routing (CBOE vs IBKR).
 * @customfunction
 */
function GETOPTIONBATCH(occRange, closedRange) {
  if (!Array.isArray(occRange)) return [['Error: Range required']];

  const hasClosedFilter = Array.isArray(closedRange) && closedRange.length === occRange.length;
  const cache = CacheService.getScriptCache();
  const OCC_REGEX = /^([A-Z][A-Z0-9.:]*)(\d{6})([CP])(\d{8})$/;

  // Parse input rows into option descriptors
  const descriptors = occRange.map((row, idx) => {
    if (hasClosedFilter) {
      const closedVal = Array.isArray(closedRange[idx]) ? closedRange[idx][0] : closedRange[idx];
      if (closedVal === true) return { isEmpty: true, rowIndex: idx };
    }
    const val = String(row[0] || row || '').trim();
    if (!val || val === '--') return { isEmpty: true, rowIndex: idx };

    const match = val.match(OCC_REGEX);
    if (!match) return { isEmpty: true, rowIndex: idx };

    return {
      isEmpty: false, rowIndex: idx,
      ticker: match[1], expDate: match[2],
      type: match[3], formattedStrike: match[4]
    };
  });

  const results = occRange.map(() => EMPTY_ROW.slice());
  const active = descriptors.filter(d => !d.isEmpty);
  if (!active.length) return results;

  const ibkrMisses  = [];
  const cboeMisses  = {};

  // Routing and Cache Check
  active.forEach(d => {
    const isEuropean = d.ticker.includes(':');
    if (isEuropean) {
      const key = `ibkr_${d.ticker}_${d.expDate}_${d.type}_${d.formattedStrike}`;
      const cached = cache.get(key);
      if (cached) {
        if (cached !== 'null') {
          const parsed = JSON.parse(cached);
          if (parsed[7]) parsed[7] = new Date(parsed[7]);
          results[d.rowIndex] = parsed;
        } else {
          results[d.rowIndex] = EUROPEAN_ROW.slice();
        }
      } else if (IBKR_CONFIG.API_KEY) {
        ibkrMisses.push(d);
      } else {
        // No IBKR API key provided: mark European options as unavailable
        results[d.rowIndex] = EUROPEAN_ROW.slice();
      }
    } else {
      // American options: queue for CBOE batch fetch
      if (!cboeMisses[d.ticker]) cboeMisses[d.ticker] = [];
      cboeMisses[d.ticker].push(d);
    }
  });

  // Execute IBKR requests in parallel
  if (ibkrMisses.length > 0) {
    const requests = ibkrMisses.map(d => _buildIBKRRequest(d.ticker, d.expDate, d.type, d.formattedStrike));
    let responses;
    try { responses = UrlFetchApp.fetchAll(requests); } catch (e) { responses = ibkrMisses.map(() => null); }

    responses.forEach((resp, i) => {
      const d = ibkrMisses[i];
      const key = `ibkr_${d.ticker}_${d.expDate}_${d.type}_${d.formattedStrike}`;
      if (resp && resp.getResponseCode() === 200) {
        const data = _parseIBKRData(JSON.parse(resp.getContentText()));
        try { cache.put(key, JSON.stringify(data), IBKR_CONFIG.CACHE_TTL); } catch (e) {}
        results[d.rowIndex] = data;
      } else {
        try { cache.put(key, 'null', 60); } catch (e) {}
        results[d.rowIndex] = EUROPEAN_ROW.slice();
      }
    });
  }

  // Execute CBOE requests in parallel (one request per unique ticker)
  const cboeTickers = Object.keys(cboeMisses);
  if (cboeTickers.length > 0) {
    const toFetch = [];
    const requests = [];

    cboeTickers.forEach(ticker => {
      const key = `cboe_${ticker}`;
      const cached = cache.get(key);
      if (cached && cached !== 'null') {
        try {
          const json = JSON.parse(cached);
          cboeMisses[ticker].forEach(d => {
            const r = _parseCBOEJson(json, d.ticker, d.expDate, d.type, d.formattedStrike);
            if (r) results[d.rowIndex] = r;
          });
        } catch (e) {}
      } else {
        const urlTicker = CBOE_CONFIG.INDEX_TICKERS.includes(ticker) ? `_${ticker}` : ticker;
        toFetch.push(ticker);
        requests.push({
          url: `https://cdn.cboe.com/api/global/delayed_quotes/options/${urlTicker}.json?_ts=${Date.now()}`,
          muteHttpExceptions: true
        });
      }
    });

    if (requests.length > 0) {
      let cboeResponses;
      try { cboeResponses = UrlFetchApp.fetchAll(requests); } catch (e) { cboeResponses = requests.map(() => null); }

      cboeResponses.forEach((resp, i) => {
        const ticker = toFetch[i];
        const key = `cboe_${ticker}`;
        if (resp && resp.getResponseCode() === 200) {
          const text = resp.getContentText();
          let json;
          try { json = JSON.parse(text); } catch (e) { cache.put(key, 'null', 60); return; }
          try { cache.put(key, text, CBOE_CONFIG.CACHE_TTL); } catch (e) {}
          cboeMisses[ticker].forEach(d => {
            const r = _parseCBOEJson(json, d.ticker, d.expDate, d.type, d.formattedStrike);
            if (r) results[d.rowIndex] = r;
          });
        } else {
          try { cache.put(key, 'null', 60); } catch (e) {}
        }
      });
    }
  }

  return results;
}


// --- 4. INTERNAL HELPERS ---

function _fetchOption(ticker, expDate, type, formattedStrike) {
  const occ = ticker + expDate + type + formattedStrike;
  const results = GETOPTIONBATCH([[occ]]);
  const row = results[0];
  return row.every(v => v === '' || v === 0 || v === null) ? null : row;
}

function _buildIBKRRequest(ticker, expDate, type, formattedStrike) {
  const expiry = `20${expDate}`;
  const strike = parseInt(formattedStrike, 10) / 1000;
  const params = `underlying=${encodeURIComponent(ticker)}&expiry=${expiry}&strike=${strike}&right=${type}`;
  return {
    url: `${IBKR_CONFIG.API_URL}/option/greeks?${params}`,
    method: 'GET',
    headers: { 'X-API-Key': IBKR_CONFIG.API_KEY, 'User-Agent': 'Mozilla/5.0' },
    muteHttpExceptions: true
  };
}

function _parseIBKRData(data) {
  const lastDate = data.last_date ? new Date(data.last_date) : null;
  return [
    Math.abs(data.delta || 0),
    data.gamma || 0,
    Math.abs(data.theta || 0),
    data.implied_vol || 0,
    data.open_interest || 0,
    data.volume || 0,
    data.last_price || 0,
    (lastDate && !isNaN(lastDate.getTime())) ? lastDate : null
  ];
}

function _parseCBOEJson(json, ticker, expDate, type, formattedStrike) {
  if (!json || !json.data || !Array.isArray(json.data.options)) return null;
  const id = ticker.replace(/\./g, '') + expDate + type + formattedStrike;
  const opt = json.data.options.find(o => o.option === id);
  if (!opt) return null;

  return [
    Math.abs(Number(opt.delta) || 0),
    Number(opt.gamma) || 0,
    Math.abs(Number(opt.theta) || 0),
    Number(opt.iv) || 0,
    Number(opt.open_interest) || 0,
    Number(opt.volume) || 0,
    Number(opt.last_trade_price) || 0,
    opt.last_trade_time ? new Date(opt.last_trade_time + CBOE_CONFIG.TZ_OFFSET) : null
  ];
}

function _fetchPricesViaGoogleFinance(tickers) {
  const prices = {};
  const cache  = CacheService.getScriptCache();
  const missing = [];

  tickers.forEach(t => {
    const cached = cache.get(`price_${t}`);
    if (cached) prices[t] = parseFloat(cached);
    else missing.push(t);
  });

  if (missing.length > 0) {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sysSheet = ss.getSheetByName('_SYS_CALC_');
    if (!sysSheet) {
      sysSheet = ss.insertSheet('_SYS_CALC_');
      sysSheet.hideSheet();
    }
    sysSheet.clear();
    sysSheet.getRange(1, 1, missing.length, 1).setFormulas(missing.map(t => [`=GOOGLEFINANCE("${t}")`]));
    SpreadsheetApp.flush();
    Utilities.sleep(1500); 
    sysSheet.getRange(1, 1, missing.length, 1).getValues().forEach((row, i) => {
      const ticker = missing[i];
      if (typeof row[0] === 'number') {
        prices[ticker] = row[0];
        cache.put(`price_${ticker}`, String(row[0]), 300);
      }
    });
  }
  return prices;
}

/**
 * _getHistoricalFxRate
 * Fetches historical FX rate with permanent caching using PropertiesService.
 */
function _getHistoricalFxRate(currency, date) {
  if (!date) return GETFXRATE_OPTIMIZED(currency, null);
  
  // Normalize symbols for cache consistency
  const mapping = { '$': 'USD', '£': 'GBP', 'kr': 'SEK', '€': 'EUR' };
  const isoCode = mapping[currency] || currency;
  if (isoCode === 'EUR') return 1;

  const dateStr = Utilities.formatDate(date, 'GMT', 'yyyy-MM-dd');
  const propKey = `fx_hist_${isoCode}_${dateStr}`;
  
  // L1 Cache: Permanent (PropertiesService)
  const props  = PropertiesService.getScriptProperties();
  const stored = props.getProperty(propKey);
  if (stored) return parseFloat(stored);

  // L2: API Fetch via Frankfurter
  const rate = GETFXRATE_OPTIMIZED(currency, date);
  if (rate && rate > 0) {
    try { props.setProperty(propKey, String(rate)); } catch (e) {}
  }
  return rate || 1;
}

/**
 * GETFXRATE_OPTIMIZED
 * Exchange rate fetcher with short-term cache.
 * @customfunction
 */
function GETFXRATE_OPTIMIZED(symbol, date) {
  const mapping = { '$': 'USD', '£': 'GBP', 'kr': 'SEK' };
  const currency = mapping[symbol];
  if (!currency) return 1;

  const dateStr = (date instanceof Date) ? Utilities.formatDate(date, "GMT", "yyyy-MM-dd") : "latest";
  const cacheKey = `fx_${currency}_${dateStr}`;
  const cache = CacheService.getScriptCache();
  const cached = cache.get(cacheKey);
  if (cached) return parseFloat(cached);

  try {
    const url = `https://api.frankfurter.app/${dateStr}?from=EUR&to=${currency}`;
    const resp = JSON.parse(UrlFetchApp.fetch(url).getContentText());
    const rate = resp.rates[currency];
    if (rate) {
      cache.put(cacheKey, String(rate), 21600); // 6 hours
      return rate;
    }
  } catch (e) {
    return null;
  }
  return 1;
}

/**
 * _formatTimestamp
 * Generates a compact execution status string.
 */
function _formatTimestamp(startTime) {
  const elapsed = (new Date() - startTime) / 1000;
  const ts = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'dd/MM/yy HH:mm:ss');
  return `↻ ${ts} (${elapsed.toFixed(1)}s)`;
}
