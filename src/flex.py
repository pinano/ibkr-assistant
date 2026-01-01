# flex.py
import os
import sys
import smtplib
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO
from ibflex import client as ibflex_client
from src.config import settings

def sortchildrenby(parent, attr_1, attr_2=None):
    if attr_2 is None:
        parent[:] = sorted(parent, key=lambda child: child.get(attr_1) or " ")
    else:
        parent[:] = sorted(parent, key=lambda child: (child.get(attr_1) or " ", child.get(attr_2) or " "))

def fmt_num(val, precision=2):
    try:
        f = float(val)
        return ('{:.' + str(precision) + 'f}').format(round(f, precision))
    except:
        return val

class FlexReporter:
    @staticmethod
    def run_report(token=None, query_id=None, local_date=None):
        token = token or settings.IB_FLEX_TOKEN
        query_id = query_id or settings.IB_FLEX_QUERY_ID
        
        old_stdout = sys.stdout
        result = StringIO()
        sys.stdout = result

        telegram_msgs = []
        dividend_msg = ""
        has_dividends = False

        try:
            # 1. Fetch Data
            if local_date:
                try:
                    dirname = os.path.dirname(__file__)
                    # Assuming file format YYYYMMDD.xml (user requested YYYYMMDD arg)
                    file_path = os.path.join(dirname, f'../flex_queries/{local_date}.xml')
                    with open(file_path, 'rb') as f:
                        response = f.read()
                    print(f"<!-- Loaded local report: {local_date}.xml -->")
                    
                    try:
                        tree = ET.ElementTree(ET.fromstring(response))
                        root = tree.getroot()
                    except Exception as e:
                         return f"Error parsing local XML: {e}", None, [], None
                         
                except Exception as e:
                    return f"Error reading local file {local_date}.xml: {e}", None, [], None
            else:
                try:
                    response = ibflex_client.download(token, query_id)
                    tree = ET.ElementTree(ET.fromstring(response))
                    root = tree.getroot()
                except Exception as e:
                    return f"Error downloading/parsing Flex Query: {e}", None, [], None

            # 2. Archiving (Only if not local)
            archive_status = "Skipped (Local)"
            if not local_date:
                try:
                    archive_dir = '/app/flex_queries'
                    os.makedirs(archive_dir, exist_ok=True)
                    
                    flex_stmt = root.find('.//FlexStatement')
                    f_date = flex_stmt.get('fromDate')
                    # t_date = flex_stmt.get('toDate')
                    # User requested YYYYMMDD.xml format
                    arch_filename = f"{f_date.replace('-', '')}.xml"
                    full_path = os.path.join(archive_dir, arch_filename)
                    
                    with open(full_path, 'w') as f:
                        f.write(response.decode('utf-8') if isinstance(response, bytes) else response)
                    archive_status = f"Saved: {arch_filename}"
                except Exception as e:
                    archive_status = f"Failed: {e}"
                    print(f"<!-- Archiving failed: {e} -->")
            else:
                archive_status = f"Read from: {local_date}.xml"

            # 3. Report Logic
            
            # Date range
            fromDate = root.find('.//FlexStatement').get('fromDate')
            toDate = root.find('.//FlexStatement').get('toDate')
            dateRange = fromDate + ' to ' + toDate if fromDate != toDate else fromDate
            print('<h1>' + dateRange + ': Cash Transactions Report</h1>')

            # CashReport
            summary_msg = "Flex Query Report\n" + "-" * 12 + "\n"
            print('<h2>Cash Report</h2>')
            print('<table> <thead> <tr>')
            print('<td>cur</td> <td>startCash</td> <td>endCash</td> <td>endSettledCash</td> <td>deposits</td> '
                  '<td>w/drawals</td> <td>purchases</td> <td>sales</td> <td>divs</td> <td>inLieu</td> <td>whTax</td> '
                  '<td>brkrInt.</td> <td>commiss.</td> <td>transTax</td> <td>fxGainLoss</td></tr> </thead> <tbody>')
            
            idx = 0
            for cashReport in root.iter('CashReportCurrency'):
                if cashReport.get('currency') == 'SEK': continue
                idx += 1
                c = cashReport.get
                is_base = c('currency') == 'BASE_SUMMARY'
                cur_display = '<b>BASE</b>' if is_base else c('currency')
                cur_telegram = 'BASE' if is_base else c('currency')
                
                print(f'<tr class="{"even" if idx % 2 else "odd"}">')
                print(f'<td class="c">{cur_display}</td>')
                print(f'<td class="r">{fmt_num(c("startingCash"))}</td>')
                print(f'<td class="r">{fmt_num(c("endingCash"))}</td>')
                print(f'<td class="r">{fmt_num(c("endingSettledCash"))}</td>')
                print(f'<td class="r">{fmt_num(c("deposits"))}</td>')
                print(f'<td class="r">{fmt_num(c("withdrawals"))}</td>')
                print(f'<td class="r">{fmt_num(c("netTradesPurchases"))}</td>')
                print(f'<td class="r">{fmt_num(c("netTradesSales"))}</td>')
                print(f'<td class="r">{fmt_num(c("dividends"))}</td>')
                print(f'<td class="r">{fmt_num(c("paymentInLieu"))}</td>')
                print(f'<td class="r">{fmt_num(c("withholdingTax"))}</td>')
                print(f'<td class="r">{fmt_num(c("brokerInterest"))}</td>')
                print(f'<td class="r">{fmt_num(c("commissions"))}</td>')
                print(f'<td class="r">{fmt_num(c("transactionTax"))}</td>')
                print(f'<td class="r">{fmt_num(c("fxTranslationGainLoss"))}</td>')
                print('</tr>')
                # Use fmt_num for Telegram message to avoid raw long decimals
                summary_msg += f"{cur_telegram.rjust(4, ' ')}: {fmt_num(c('endingCash'))}\n"
            print('</tbody></table>')
            telegram_msgs.append(summary_msg)

            # CashTransactions
            print('<h2>Cash Transactions</h2>')
            print('<table> <thead> <tr><td>tckr</td> <td>date</td> <td>cur</td> <td>fxRate</td> <td>amount</td> <td>type</td> <td>description</td> <td>exchg</td></tr> </thead> <tbody>')
            cash_txs = list(root.iter('CashTransaction'))
            sortchildrenby(cash_txs, 'dateTime', 'symbol')
            for i, attrs in enumerate(cash_txs):
                a = attrs.get
                if a('type') == 'Dividends':
                    has_dividends = True
                    if not dividend_msg: dividend_msg = "Dividends\n" + "-" * 12 + "\n"
                    dividend_msg += f"{a('symbol')}: {a('currency')} {fmt_num(a('amount'), 3)}\n{a('description')}\n"
                
                cls = 'red' if float(a('amount') or 0) < 0 else 'green'
                print(f'<tr class="{"even" if i % 2 else "odd"}">')
                print(f'<td class="r">{a("symbol")}</td><td class="c">{a("dateTime").split(";",1)[0]}</td><td class="c">{a("currency")}</td>')
                print(f'<td>{fmt_num(a("fxRateToBase"), 7)}</td>')
                print(f'<td class="r {cls}">{fmt_num(a("amount"), 4)}</td><td class="c">{a("type")}</td>')
                print(f'<td>{a("description").replace(a("symbol"),"").strip()}</td><td class="r">{a("listingExchange")}</td>')
                print('</tr>')
            print('</tbody></table>')
            if has_dividends: telegram_msgs.append(dividend_msg)

            # TransactionTaxes
            print('<h2>Transaction Taxes</h2>')
            print('<table> <thead> <tr><td>tckr</td> <td>date</td> <td>cur</td> <td>fxRate</td> <td>tckr desc.</td> <td>taxAmt</td> <td>description</td> <td>exchg</td></tr> </thead> <tbody>')
            taxes = list(root.iter('TransactionTax'))
            sortchildrenby(taxes, 'date')
            for i, a in enumerate(taxes):
                print(f'<tr class="{"even" if i % 2 else "odd"}">')
                print(f'<td>{a.get("symbol")}</td><td>{a.get("date")}</td><td>{a.get("currency")}</td><td>{fmt_num(a.get("fxRateToBase"), 7)}</td>')
                print(f'<td>{a.get("description")}</td><td>{fmt_num(a.get("taxAmount"), 5)}</td><td>{a.get("taxDescription")}</td>')
                print(f'<td class="r">{a.get("listingExchange")}</td></tr>')
            print('</tbody></table>')

            # ChangeInDividendAccruals
            print('<h2>Change in Dividend Accruals</h2>')
            print('<table> <thead> <tr><td>tckr</td> <td>exdate</td> <td>paydate</td> <td>cur</td> <td>qty</td> <td>gRate</td> <td>gAmt</td> <td>tax</td> <td>taxPct</td> <td>nAmt</td> <td>description</td> <td>exchg</td></tr> </thead> <tbody>')
            accruals = list(root.iter('ChangeInDividendAccrual'))
            sortchildrenby(accruals, 'payDate', 'symbol')
            for a in accruals:
                at = a.get
                net = float(at('netAmount') or 0)
                cls = 'red' if net < 0 else 'green'
                tax = float(at('tax') or 0)
                qty = float(at('quantity') or 1)
                grate = float(at('grossRate') or 1)
                taxpct = (tax * 100) / (qty * grate) if (qty * grate) != 0 else 0
                
                print(f'<tr class="{cls}">')
                print(f'<td class="r">{at("symbol").rjust(4)}</td><td>{at("exDate")}</td><td>{at("payDate")}</td><td>{at("currency")}</td>')
                print(f'<td class="r">{at("quantity").rjust(3)}</td><td class="r">{fmt_num(at("grossRate"), 3)}</td><td class="r">{fmt_num(at("grossAmount"), 3)}</td>')
                print(f'<td class="r">{fmt_num(at("tax"), 3)}</td><td class="r">{fmt_num(taxpct, 2)}%</td><td class="r">{fmt_num(at("netAmount"), 3)}</td>')
                print(f'<td>{at("description")}</td><td class="r">{at("listingExchange")}</td></tr>')
            print('</tbody></table>')

            # OpenDividendAccruals
            print('<h2>Open Dividend Accruals</h2>')
            print('<table> <thead> <tr><td>tckr</td> <td>exdate</td> <td>paydate</td> <td>cur</td> <td>qty</td> <td>gRate</td> <td>gAmt</td> <td>tax</td> <td>taxPct</td> <td>nAmt</td> <td>description</td> <td>exchg</td></tr> </thead> <tbody>')
            open_accruals = list(root.iter('OpenDividendAccrual'))
            sortchildrenby(open_accruals, 'payDate', 'symbol')
            for i, a in enumerate(open_accruals):
                at = a.get
                tax = float(at('tax') or 0)
                qty = float(at('quantity') or 1)
                grate = float(at('grossRate') or 1)
                taxpct = (tax * 100) / (qty * grate) if (qty * grate) != 0 else 0
                print(f'<tr class="{"even" if i % 2 else "odd"}">')
                print(f'<td class="r">{at("symbol").rjust(4)}</td><td>{at("exDate")}</td><td>{at("payDate")}</td><td>{at("currency")}</td>')
                print(f'<td class="r">{at("quantity").rjust(3)}</td><td class="r">{fmt_num(at("grossRate"), 3)}</td><td class="r">{fmt_num(at("grossAmount"), 3)}</td>')
                print(f'<td class="r">{fmt_num(at("tax"), 3)}</td><td class="r">{fmt_num(taxpct, 2)}%</td><td class="r">{at("netAmount")}</td>')
                print(f'<td>{at("description")}</td><td class="r">{at("listingExchange")}</td></tr>')
            print('</tbody></table>')

            # TierInterestDetails
            print('<h2>Interest Details</h2>')
            print('<table> <thead> <tr><td>date</td> <td>cur</td> <td>total</td> <td>fxRate</td> <td>rate</td> <td>amt</td> <td>description</td></tr> </thead> <tbody>')
            interest = list(root.iter('TierInterestDetail'))
            sortchildrenby(interest, 'valueDate', 'currency')
            for i, a in enumerate(interest):
                at = a.get
                print(f'<tr class="{"even" if i % 2 else "odd"}">')
                print(f'<td>{at("valueDate")}</td><td>{at("currency")}</td><td class="r">{fmt_num(at("totalPrincipal"))}</td>')
                print(f'<td>{fmt_num(at("fxRateToBase"), 7)}</td><td class="r">{at("rate")}%</td><td>{at("totalInterest")}</td><td>{at("interestType")}</td></tr>')
            print('</tbody></table>')

            # Trades
            print('<h2>Trades</h2>')
            print('<table> <thead> <tr><td>tckr</td> <td>date</td> <td>buySell</td> <td>cur</td> <td>fxRate</td> <td>qty</td> <td>price</td> <td>comm</td> <td>comCur</td> <td>netCash</td> <td>desc</td> <td>underl</td> <td>mult</td> <td>strike</td> <td>expiry</td> <td>p/c</td> <td>exchg</td> <td>listExch</td> <td>undExh</td></tr> </thead> <tbody>')
            trades = list(root.iter('Trade'))
            sortchildrenby(trades, 'tradeDate', 'symbol')
            for a in trades:
                at = a.get
                cls = 'green' if at('buySell') == 'SELL' else 'red'
                net = float(at('netCash') or 0)
                comm = float(at('ibCommission') or 0)
                qty = float(at('quantity') or 1)
                u_price = -1 * (net - comm) / qty if qty != 0 else 0
                
                print(f'<tr class="{cls}">')
                print(f'<td class="r">{at("symbol")}</td><td>{at("tradeDate")}</td><td class="c">{at("buySell")}</td><td>{at("currency")}</td>')
                print(f'<td>{fmt_num(at("fxRateToBase"), 7)}</td><td class="r">{at("quantity")}</td><td class="r">{fmt_num(u_price, 4)}</td>')
                print(f'<td class="r">{fmt_num(-1*comm, 8)}</td><td class="c">{at("ibCommissionCurrency")}</td><td class="r">{fmt_num(at("netCash"), 8)}</td>')
                print(f'<td>{at("description")}</td><td class="r">{at("underlyingSymbol")}</td><td class="r">{at("multiplier").replace(".",",")}</td>')
                print(f'<td class="r">{fmt_num(at("strike"), 2) if at("strike") else ""}</td><td>{at("expiry")}</td><td class="c">{at("putCall")}</td>')
                print(f'<td class="c">{at("exchange")}</td><td class="c">{at("listingExchange")}</td><td class="c">{at("underlyingListingExchange")}</td></tr>')
            print('</tbody></table>')

            # ConversionRates
            print('<h2>Conversion Rates</h2>')
            print('<table> <thead> <tr><td>date</td> <td>from</td> <td>rate</td> <td>to</td> <td>rate</td></tr> </thead> <tbody>')
            rates = list(root.iter('ConversionRate'))
            sortchildrenby(rates, 'reportDate', 'fromCurrency')
            idx = 0
            for a in rates:
                at = a.get
                if at('fromCurrency') not in ['USD', 'GBP']: continue
                idx += 1
                rate = float(at('rate') or 1)
                inv = 1 / rate if rate != 0 else 0
                print(f'<tr class="{"even" if idx % 2 else "odd"}">')
                print(f'<td>{at("reportDate")}</td><td>{at("fromCurrency")}EUR</td><td>{fmt_num(rate, 7)}</td>')
                print(f'<td>EUR{at("fromCurrency")}</td><td>{fmt_num(inv, 10)}</td></tr>')
            print('</tbody></table>')

        except Exception as e:
            sys.stdout = old_stdout
            return f"Error generating report: {e}", None, [], None
        
        sys.stdout = old_stdout
        html_content = result.getvalue()
        return html_content, dateRange, telegram_msgs, archive_status

    @staticmethod
    def send_email(html_content, subject):
        if not settings.EMAIL_SMTP_USER:
            return "SMTP settings missing"

        msg = MIMEMultipart('alternative')
        msg['From'] = settings.EMAIL_SENDER
        msg['To'] = settings.EMAIL_RECIPIENT
        msg['Subject'] = subject

        style = """
        <style>
            @import url(https://fonts.googleapis.com/css?family=Roboto+Condensed);
            * { font-family: Roboto Condensed, monospace; color: black; }
            table, th, td { border: 1px solid Silver; border-collapse: collapse; font-size: 11px; }
            thead td { font-weight: bold; color:white; background-color: black; text-align: center; }
            td { padding: 2px 4px; }
            tr.red td, td.red { color: #C53929; } 
            tr.green td, td.green { color: #0B8043; }
            tr.even { background-color: #E8F0FE; }
            .r { text-align: right; } .c { text-align: center; }
        </style>
        """
        full_html = f"<html><head>{style}</head><body>{html_content}</body></html>"
        msg.attach(MIMEText(full_html, 'html'))

        try:
            server = smtplib.SMTP(settings.EMAIL_SMTP_SERVER, settings.EMAIL_SMTP_PORT)
            server.starttls()
            server.login(settings.EMAIL_SMTP_USER, settings.EMAIL_SMTP_PASSWORD)
            server.sendmail(msg['From'], msg['To'], msg.as_string())
            server.quit()
            return "Email sent successfully"
        except Exception as e:
            return f"Failed to send email: {e}"