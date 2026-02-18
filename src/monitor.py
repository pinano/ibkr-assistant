import logging
import asyncio
import httpx
from sqlalchemy.orm import Session
from src.models import Alert, OptionSnapshot
from src.config import settings

logger = logging.getLogger("ibkr-monitor")

import datetime

class Monitor:
    def __init__(self, db_session_factory):
        self.SessionLocal = db_session_factory
        # Cache for global alerts to avoid spam: {symbol: last_alert_time}
        self.global_alert_cache = {}
        self.GLOBAL_DELTA_THRESHOLD = 0.25

    async def check_alerts(self):
        from src.bot import notify_admins
        logger.info("Checking alerts...")
        session = self.SessionLocal()
        try:
            # 1. Custom Alerts (Database)
            alerts = session.query(Alert).filter(Alert.triggered == 0).all()
            
            # 2. Global Monitoring (All Open Options)
            # Fetch all positions first
            headers = {"X-API-Key": settings.API_KEY}
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Fetch positions
                try:
                    r_pos = await client.get(f"{settings.WEB_SERVICE_URL}/account/positions", headers=headers)
                    if r_pos.status_code == 200:
                        positions = r_pos.json()
                        options = [p for p in positions if p.get('secType') == 'OPT']
                    else:
                        logger.warning(f"Failed to fetch positions: {r_pos.status_code}")
                        options = []
                except Exception as e:
                    logger.error(f"Error fetching positions: {e}")
                    options = []

                # Build map: conId -> position data (including qty for short/long detection)
                option_map = {}
                for p in options:
                    con_id = p.get('conId', 0)
                    if con_id:
                        option_map[con_id] = p

                # Collect symbols to check from DB Alerts
                custom_symbols = set(a.symbol for a in alerts)

                if not custom_symbols and not option_map:
                    logger.info("No symbols to monitor.")
                    return

                # Fetch Greeks for all open options via /option/greeks (conId-based)
                market_data = {}  # conId -> greeks
                for con_id, opt in option_map.items():
                    try:
                        params = {
                            'underlying': opt.get('underlying', ''),
                            'expiry': opt.get('expiry', ''),
                            'strike': opt.get('strike', 0),
                            'right': opt.get('right', ''),
                            'conId': con_id
                        }
                        r = await client.get(f"{settings.WEB_SERVICE_URL}/option/greeks", params=params, headers=headers)
                        if r.status_code == 200:
                            market_data[con_id] = r.json()
                    except Exception as e:
                        logger.debug(f"Error fetching greeks for conId={con_id}: {e}")

                # Fetch data for custom DB alert symbols (these use the old endpoint)
                custom_data = {}
                for symbol in custom_symbols:
                    try:
                        url = f"{settings.WEB_SERVICE_URL}/option/risk/{symbol}"
                        r = await client.get(url, headers=headers)
                        if r.status_code == 200:
                            custom_data[symbol] = r.json()
                    except Exception as e:
                        logger.debug(f"Error fetching data for {symbol}: {e}")

            # --- Evaluate Custom DB Alerts ---
            for alert in alerts:
                data = custom_data.get(alert.symbol)
                if not data: continue

                metric_map = {
                    'delta': 'delta', 'gamma': 'gamma', 'vega': 'vega', 'theta': 'theta',
                    'iv': 'implied_vol', 'price': 'last_price', 'underlying': 'underlying_price'
                }
                metric_key = metric_map.get(alert.metric.lower())
                current_value = data.get(metric_key) if metric_key else None
                
                if current_value is None: continue
                
                triggered = False
                if alert.condition == '>': triggered = current_value > alert.threshold
                elif alert.condition == '<': triggered = current_value < alert.threshold
                
                if triggered:
                    logger.info(f"Custom Alert {alert.id} triggered: {alert.symbol}")
                    msg = (
                        f"🚨 <b>Custom Alert!</b>\n"
                        f"🔹 <code>{alert.symbol}</code>\n"
                        f"🔹 Rule: <code>{alert.metric} {alert.condition} {alert.threshold}</code>\n"
                        f"🔸 Value: <code>{current_value:.4f}</code>"
                    )
                    await notify_admins(msg, parse_mode="HTML")
                    alert.triggered = 1
                    session.commit()

            # --- Evaluate Global Rules (Automated) ---
            # Rule: High Delta Warning — only for SHORT positions (sold options)
            # Collect all triggered alerts, then send a single digest message
            delta_alerts = []
            now = datetime.datetime.now()
            
            for con_id, data in market_data.items():
                opt = option_map.get(con_id)
                if not opt: continue
                
                # Skip long positions (qty > 0) — only alert for sold options
                qty = opt.get('qty', 0)
                if qty >= 0: continue
                
                delta = data.get('delta', 0.0)
                # Ignore if delta is effectively zero (no data)
                if abs(delta) < 0.0001: continue

                if abs(delta) > self.GLOBAL_DELTA_THRESHOLD:
                    # Check throttling: skip if alerted < 4 hours ago
                    last_alert = self.global_alert_cache.get(con_id)
                    if last_alert and (now - last_alert) <= datetime.timedelta(hours=4):
                        continue
                    
                    underlying = opt.get('underlying', '??')
                    right = opt.get('right', '?')
                    strike = opt.get('strike', 0)
                    expiry = opt.get('expiry', '')
                    
                    strike_fmt = f"{strike:.0f}" if strike == int(strike) else f"{strike}"
                    exp_fmt = f"{expiry[0:4]}-{expiry[4:6]}-{expiry[6:8]}" if len(expiry) == 8 else expiry
                    display = f"{underlying} {right} {strike_fmt} {exp_fmt}"
                    
                    delta_alerts.append({
                        'con_id': con_id,
                        'display': display,
                        'delta': delta,
                        'qty': qty
                    })
            
            # Send a single digest if there are any high-delta alerts
            if delta_alerts:
                logger.info(f"Global Delta Alert: {len(delta_alerts)} contract(s) above threshold")
                
                # Calculate max display width for alignment
                max_display = max(len(r['display']) for r in delta_alerts)
                pad = max(max_display, 18)
                
                lines = [f"⚠️ <b>High Delta Warning — {len(delta_alerts)} Short Position(s)</b>\n"]
                for a in delta_alerts:
                    # In automated alerts, all are above threshold, so always red
                    marker = "🔴"
                    display_padded = a['display'].ljust(pad)
                    delta_str = f"{a['delta']:+.3f}".rjust(7)
                    qty_str = f"({a['qty']:.0f})".rjust(4)
                    
                    lines.append(
                        f"{marker} <code>{display_padded}  Δ {delta_str}  {qty_str}</code>"
                    )
                lines.append(f"\n🔴 abs(Δ) &gt; {self.GLOBAL_DELTA_THRESHOLD}")
                
                await notify_admins("\n".join(lines), parse_mode="HTML")
                
                # Update throttle cache for all alerted contracts
                for a in delta_alerts:
                    self.global_alert_cache[a['con_id']] = now

        except Exception as e:
            logger.error(f"Error in check_alerts: {e}", exc_info=True)
        finally:
            session.close()

    async def refresh_greeks_cache(self):
        """
        Periodically triggered job to force-refresh the Greeks cache for all open options.
        Target: European options (but we do all for completeness).
        """
        logger.info("Executing periodic Greeks cache refresh...")
        from src.config import settings
        
        # We need a dedicated client or use one-off
        headers = {"X-API-Key": settings.API_KEY}
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                # 1. Fetch positions
                try:
                    r_pos = await client.get(f"{settings.WEB_SERVICE_URL}/account/positions", headers=headers)
                    if r_pos.status_code != 200:
                        logger.warning(f"Refresh failed: Could not fetch positions ({r_pos.status_code})")
                        return
                    
                    positions = r_pos.json()
                    options = [p for p in positions if p.get('secType') == 'OPT']
                    
                    if not options:
                        logger.info("No options to refresh.")
                        return

                    logger.info(f"Refreshing cache for {len(options)} options...")
                    
                    # 2. Call API for each option with force_refresh=True
                    # We limit concurrency to avoid overwhelming IBKR Gateway
                    import asyncio
                    semaphore = asyncio.Semaphore(5)
                    
                    async def refresh_one(opt):
                        async with semaphore:
                            try:
                                params = {
                                    'underlying': opt.get('underlying', ''),
                                    'expiry': opt.get('expiry', ''),
                                    'strike': opt.get('strike', 0),
                                    'right': opt.get('right', ''),
                                    'conId': opt.get('conId', 0),
                                    'force_refresh': 'true'
                                }
                                # Verify essential params are present
                                if not params['underlying'] or not params['expiry']:
                                    return
                                    
                                await client.get(f"{settings.WEB_SERVICE_URL}/option/greeks", params=params, headers=headers)
                            except Exception as e:
                                logger.error(f"Error refreshing {opt.get('underlying')}: {e}")

                    tasks = [refresh_one(opt) for opt in options]
                    await asyncio.gather(*tasks)
                    
                    logger.info("Greeks cache refresh complete.")

                except Exception as e:
                    logger.error(f"Error checking positions/refreshing: {e}")

        except Exception as e:
            logger.error(f"Error in refresh_greeks_cache: {e}", exc_info=True)

