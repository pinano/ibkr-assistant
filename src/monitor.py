import logging
import asyncio
import httpx
from sqlalchemy.orm import Session
from src.models import Alert
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

                # Collect symbols to check: DB Alerts + All Open Options
                custom_symbols = set(a.symbol for a in alerts)
                option_symbols = set(p['symbol'] for p in options)
                all_symbols = custom_symbols.union(option_symbols)

                if not all_symbols:
                    logger.info("No symbols to monitor.")
                    return

                # Fetch data for all symbols in parallel (or single loop if easier, but let's do batching if possible? 
                # For now, simple loop is safer for throttling logic)
                market_data = {}
                for symbol in all_symbols:
                    try:
                        url = f"{settings.WEB_SERVICE_URL}/option/risk/{symbol}"
                        r = await client.get(url, headers=headers)
                        if r.status_code == 200:
                            market_data[symbol] = r.json()
                        else:
                            # 404 is common for expired options or bad data
                            pass 
                    except Exception as e:
                         # Log debug to avoid spamming error logs for every missing symbol
                         logger.debug(f"Error fetching data for {symbol}: {e}")

            # --- Evaluate Custom DB Alerts ---
            for alert in alerts:
                data = market_data.get(alert.symbol)
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
                    from src.bot import notify_admins
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
            # Rule: Delta > 0.25
            for symbol in option_symbols:
                data = market_data.get(symbol)
                if not data: continue
                
                delta = data.get('delta', 0.0)
                # Ignore if delta is effectively zero (bad data?)
                if abs(delta) < 0.0001: continue

                if abs(delta) > self.GLOBAL_DELTA_THRESHOLD:
                    # Check throttling
                    last_alert = self.global_alert_cache.get(symbol)
                    now = datetime.datetime.now()
                    
                    # Alert if never alerted OR last alert was > 4 hours ago
                    if not last_alert or (now - last_alert) > datetime.timedelta(hours=4):
                        logger.info(f"Global Alert triggered: {symbol} Delta {delta}")
                        from src.bot import notify_admins
                        msg = (
                            f"⚠️ <b>High Delta Warning</b>\n"
                            f"🔹 <code>{symbol}</code>\n"
                            f"🔹 Delta: <code>{delta:.3f}</code>\n"
                            f"🔸 Threshold: <code>&gt; {self.GLOBAL_DELTA_THRESHOLD}</code>"
                        )
                        await notify_admins(msg, parse_mode="HTML")
                        self.global_alert_cache[symbol] = now

        except Exception as e:
            logger.error(f"Error in check_alerts: {e}", exc_info=True)
        finally:
            session.close()
