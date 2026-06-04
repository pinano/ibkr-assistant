/**
 * ui.gs
 * User interface, menus, and automatic triggers.
 * @author pinano
 */

const UI_CONFIG = {
  DATA_START_ROW: 3,
  MAX_DATA_COL:   40,
  SHEET_NAME:    'Opciones',
  CELL_TOGGLE_VISIBILITY: 'C2', // Checkbox to hide/show closed rows
  COL_EXPIRY:     1,  // Column A
  COL_OPEN_DATE:  2,  // Column B
  COL_CLOSE_DATE: 3,  // Column C
  COL_OPERATION:  5,  // Column E
  COL_TICKER:     6,  // Column F
  COL_IS_CLOSED:  38, // Column AL
};

/**
 * Runs on spreadsheet open. Builds the custom menu.
 */
function onOpen() {
  const ui = SpreadsheetApp.getUi();
  
  // 1. Utilities Menu
  ui.createMenu('⚙️ Utilities')
    .addItem('➕ Add 5 Rows',  'insert5Rows')
    .addItem('➕ Add 10 Rows', 'insert10Rows')
    .addItem('➕ Add 50 Rows', 'insert50Rows')
    .addItem('🔼🔽 Sort (Closed + Expiry)', 'sortRows')
    .addSeparator()
    .addItem('📈 Update Quotes Now', 'updateQuotes')
    .addItem('💱 Update FX Now', 'updateFX')    
    .addItem('🔄 Update Greeks Now', 'updateGreeks')
    .addSeparator()
    .addItem('⏱️ Install ALL Triggers', 'installAllTriggers')
    .addItem('⏹️ Uninstall Triggers',   'uninstallTriggers')
    .addSeparator()
    .addItem('📖 About / Help', 'showReadme')
    .addToUi();
}


function insert5Rows()  { insertRows(5);  }
function insert10Rows() { insertRows(10); }
function insert50Rows() { insertRows(50); }

/**
 * Inserts rows by auto-filling from the last data row, then clears values.
 */
function insertRows(numRows) {
  const startTime = new Date();
  try {
    const sheet = getOptionsSheet();
    if (!sheet) return;

    const lastRow = sheet.getLastRow();
    const maxRows = sheet.getMaxRows();
    const numCols = Math.min(sheet.getLastColumn(), UI_CONFIG.MAX_DATA_COL);

    if (lastRow < UI_CONFIG.DATA_START_ROW) return;

    // Only expand the physical sheet if necessary
    if (lastRow + numRows > maxRows) sheet.insertRowsAfter(maxRows, numRows);

    sheet.getRange(lastRow, 1, 1, numCols)
         .autoFill(sheet.getRange(lastRow, 1, numRows + 1, numCols), SpreadsheetApp.AutoFillSeries.DEFAULT_SERIES);

    // Clear data values in new rows (keeping formulas from AutoFill)
    // Clear Columns A to G (1 to 7)
    sheet.getRange(lastRow + 1, 1, numRows, 7).clearContent();
    // Clear Columns P to W (based on GREEKS_CONFIG)
    sheet.getRange(lastRow + 1, GREEKS_CONFIG.COL_RESULT, numRows, GREEKS_CONFIG.NUM_COLS).clearContent();

    const elapsed = (new Date() - startTime) / 1000;
    SpreadsheetApp.getActive().toast(`Added ${numRows} rows in ${elapsed.toFixed(1)}s`, '✅ Success');

  } catch (e) {
    Logger.log(`Error in insertRows: ${e.message}`);
  }
}

/**
 * Built-in simple trigger (no installation required).
 * Re-applies the expiry gradient when the Expiry or Closed columns are edited.
 */
function onEdit(e) {
  if (!e) return;
  const sheet = e.source.getActiveSheet();
  if (sheet.getName() !== UI_CONFIG.SHEET_NAME) return;
  
  const range = e.range;
  const col = range.getColumn();
  
  // Handle visibility toggle checkbox
  if (range.getA1Notation() === UI_CONFIG.CELL_TOGGLE_VISIBILITY) {
    const hideClosed = range.getValue() === true;
    applyClosedRowsVisibility(hideClosed);
    return; // Skip gradient check for this specific cell
  }

  if (col === UI_CONFIG.COL_EXPIRY || col === UI_CONFIG.COL_IS_CLOSED) {
    applyExpiryDateGradient();
  }
}

/**
 * Applies a red→yellow→green gradient to the expiry date column (A) for open positions.
 * Closed positions and empty rows receive no color.
 * Uses only 4 RPC calls total (2 reads + 2 writes), regardless of row count.
 */
function applyExpiryDateGradient() {
  try {
    const sheet = getOptionsSheet();
    const lastRow = sheet.getLastRow();
    if (lastRow < UI_CONFIG.DATA_START_ROW) return; // BUG #1 FIX: was UI_CONFIG_DATA_START_ROW

    const numRows    = lastRow - UI_CONFIG.DATA_START_ROW + 1;
    const dateRange  = sheet.getRange(UI_CONFIG.DATA_START_ROW, UI_CONFIG.COL_EXPIRY,    numRows, 1);
    const filterRange = sheet.getRange(UI_CONFIG.DATA_START_ROW, UI_CONFIG.COL_IS_CLOSED, numRows, 1);

    const dateValues   = dateRange.getValues().flat();
    const filterValues = filterRange.getValues().flat();

    const parseDate = v => {
      if (v instanceof Date) return v;
      if (typeof v === 'string' && v.trim() !== '') {
        const p = new Date(v);
        return isNaN(p.getTime()) ? null : p;
      }
      return null;
    };

    // Determine date range across open positions only
    let minDate = null, maxDate = null;
    dateValues.forEach((v, i) => {
      if (filterValues[i] !== false) return;
      const d = parseDate(v);
      if (!d) return;
      if (!minDate || d < minDate) minDate = d;
      if (!maxDate || d > maxDate) maxDate = d;
    });

    if (!minDate) {
      dateRange.setBackground(null).setFontColor(null);
      return;
    }

    const totalMs = maxDate.getTime() - minDate.getTime();
    const colors  = { min: '#cc0000', mid: '#ffcc00', max: '#009900' };
    const bgColors    = [];
    const fontColors  = [];

    dateValues.forEach((v, i) => {
      const d = parseDate(v);
      if (!d || filterValues[i] !== false) {
        bgColors.push([null]);
        fontColors.push([null]);
        return;
      }
      const diffMs = d.getTime() - minDate.getTime();
      const factor = totalMs === 0 ? 0
        : Math.log(diffMs / 86400000 + 1) / Math.log(totalMs / 86400000 + 1);
      const color = factor <= 0.5
        ? _interpolateColor(colors.min, colors.mid, factor / 0.5)
        : _interpolateColor(colors.mid, colors.max, (factor - 0.5) / 0.5);
      bgColors.push([color]);
      fontColors.push([_getContrastYIQ(color)]);
    });

    dateRange.setBackgrounds(bgColors);
    dateRange.setFontColors(fontColors);

  } catch (e) {
    console.error(`Error in applyExpiryDateGradient: ${e.message}`);
  }
}

/** Returns the 'Opciones' sheet or null if it doesn't exist. */
function getOptionsSheet() {
  return SpreadsheetApp.getActiveSpreadsheet().getSheetByName(UI_CONFIG.SHEET_NAME);
}

/**
 * Sorts data rows: closed first, then by close date, expiry, open date, ticker, operation.
 */
function sortRows() {
  try {
    const sheet = getOptionsSheet();
    const lastRow = sheet.getLastRow();
    if (lastRow <= UI_CONFIG.DATA_START_ROW) return;

    const numRows = lastRow - UI_CONFIG.DATA_START_ROW + 1;
    sheet.getRange(UI_CONFIG.DATA_START_ROW, 1, numRows, sheet.getLastColumn()).sort([
      { column: UI_CONFIG.COL_IS_CLOSED,  ascending: false }, // Closed rows first
      { column: UI_CONFIG.COL_CLOSE_DATE, ascending: true  }, // Then by close date
      { column: UI_CONFIG.COL_EXPIRY,     ascending: true  }, // Then by expiry
      { column: UI_CONFIG.COL_OPEN_DATE,  ascending: true  }, // Then by open date
      { column: UI_CONFIG.COL_TICKER,     ascending: true  }, // Then by ticker
      { column: UI_CONFIG.COL_OPERATION,  ascending: true  }, // Then by operation
    ]);

    applyExpiryDateGradient();
    SpreadsheetApp.getActive().toast('Sheet sorted successfully.', '✅ Sorting');

  } catch (e) {
    SpreadsheetApp.getUi().alert(`Error: ${e.message}`);
  }
}

/**
 * Applies visibility to rows marked as closed (Column AL) based on the checkbox.
 * @param {boolean} hideClosed True to hide closed rows, False to show them.
 */
function applyClosedRowsVisibility(hideClosed) {
  try {
    const sheet = getOptionsSheet();
    if (!sheet) return;

    const lastRow = sheet.getLastRow();
    if (lastRow < UI_CONFIG.DATA_START_ROW) return;

    const numRows = lastRow - UI_CONFIG.DATA_START_ROW + 1;
    
    if (!hideClosed) {
      // Simplest: Show the entire data range
      sheet.showRows(UI_CONFIG.DATA_START_ROW, numRows);
      SpreadsheetApp.getActive().toast('All rows are now visible.', '👁️ Show');
      return;
    }

    const closedValues = sheet.getRange(UI_CONFIG.DATA_START_ROW, UI_CONFIG.COL_IS_CLOSED, numRows, 1).getValues();

    // More complex: Hide only the rows that are closed.
    // We group contiguous closed rows to make fewer hideRows() calls.
    let startOfRange = -1;
    
    for (let i = 0; i < closedValues.length; i++) {
      const isClosed = closedValues[i][0] === true;
      
      if (isClosed && startOfRange === -1) {
        startOfRange = i;
      } else if (!isClosed && startOfRange !== -1) {
        // End of a contiguous closed range
        sheet.hideRows(UI_CONFIG.DATA_START_ROW + startOfRange, i - startOfRange);
        startOfRange = -1;
      }
    }
    
    // Handle the last range if it goes to the end of the data
    if (startOfRange !== -1) {
      sheet.hideRows(UI_CONFIG.DATA_START_ROW + startOfRange, closedValues.length - startOfRange);
    }
    
    SpreadsheetApp.getActive().toast('Closed rows have been hidden.', '🙈 Hide');

  } catch (e) {
    Logger.log(`Error in applyClosedRowsVisibility: ${e.message}`);
    SpreadsheetApp.getActive().toast(`Error: ${e.message}`, '❌ Error');
  }
}

/** Removes all installed project triggers. */
function uninstallTriggers() {
  const triggers = ScriptApp.getProjectTriggers();
  triggers.forEach(t => ScriptApp.deleteTrigger(t));
  SpreadsheetApp.getActive().toast(`${triggers.length} trigger(s) removed.`, '⏹️ Triggers');
}

/** Installs time-based triggers for all updaters (uninstalls old ones first to avoid duplicates). */
function installAllTriggers() {
  uninstallTriggers();
  ScriptApp.newTrigger('updateGreeks').timeBased().everyMinutes(15).create();
  ScriptApp.newTrigger('updateQuotes').timeBased().everyMinutes(5).create();
  ScriptApp.newTrigger('updateFX').timeBased().everyMinutes(15).create();
  SpreadsheetApp.getActive().toast('Triggers installed: Greeks (15m), Quotes (5m), FX (15m)', '✅ Triggers');
}


/**
 * Displays the README documentation in a modal dialog.
 */
function showReadme() {
  const html = HtmlService.createHtmlOutputFromFile('README')
    .setWidth(850)
    .setHeight(600)
    .setTitle('Financial Options Tracker - Documentation');
  SpreadsheetApp.getUi().showModalDialog(html, ' ');
}

// --- Color Helpers ---

function _interpolateColor(c1, c2, factor) {
  const f  = Math.max(0, Math.min(1, factor));
  const parse = (hex, s, e) => parseInt(hex.substring(s, e), 16);
  const lerp  = (a, b) => Math.round(a + f * (b - a));
  const r = lerp(parse(c1, 1, 3), parse(c2, 1, 3));
  const g = lerp(parse(c1, 3, 5), parse(c2, 3, 5));
  const b = lerp(parse(c1, 5, 7), parse(c2, 5, 7));
  return `#${r.toString(16).padStart(2, '0')}${g.toString(16).padStart(2, '0')}${b.toString(16).padStart(2, '0')}`;
}

function _getContrastYIQ(hex) {
  const r = parseInt(hex.substring(1, 3), 16);
  const g = parseInt(hex.substring(3, 5), 16);
  const b = parseInt(hex.substring(5, 7), 16);
  return ((r * 299) + (g * 587) + (b * 114)) / 1000 >= 128 ? 'black' : 'white';
}