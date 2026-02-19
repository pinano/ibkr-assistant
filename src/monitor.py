import logging
import asyncio
import httpx
import datetime
from sqlalchemy.orm import Session

from src.config import settings

logger = logging.getLogger("ibkr-monitor")


class Monitor:
    def __init__(self, db_session_factory):
        self.SessionLocal = db_session_factory
        # Cache for global alerts to avoid spam: {symbol: last_alert_time}
        self.global_alert_cache = {}

    async def prune_old_snapshots(self, days_retention=7):
        """Delete option snapshots older than retention period."""
        from src.models import OptionSnapshot
        logger.info(f"Pruning option snapshots older than {days_retention} days...")
        session = self.SessionLocal()
        try:
            cutoff_date = datetime.datetime.now() - datetime.timedelta(days=days_retention)
            logger.info(f"Cutoff date for pruning: {cutoff_date}")
            
            # Use synchronize_session=False for performance on bulk delete
            deleted_count = session.query(OptionSnapshot).filter(
                OptionSnapshot.updated_at < cutoff_date
            ).delete()
            session.commit()
            if deleted_count > 0:
                logger.info(f"Pruned {deleted_count} old option snapshots (updated before {cutoff_date.strftime('%Y-%m-%d %H:%M')}).")
            else:
                logger.info("No old snapshots found to prune.")
        except Exception as e:
            logger.error(f"Error pruning snapshots: {e}")
            session.rollback()
        finally:
            session.close()

    async def check_alerts(self):
        from src.bot import notify_admins
        logger.info("Checking alerts...")
        session = self.SessionLocal()
        try:
            # 2. Global Monitoring (All Open Options)
            # Fetch all positions first
            headers = {"X-API-Key": settings.API_KEY}
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Fetch positions
                try:
                    r_pos = await client.get(f"{settings.WEB_SERVICE_URL}/account/positions", headers=headers)
                    if r_pos.status_code == 200:
                        positions = r_pos.json()
                        options = [
                            p for p in positions if p.get('secType') == 'OPT']
                    else:
                        logger.warning(f"Failed to fetch positions: {r_pos.status_code}")
                        options = []
                except Exception as e:
                    logger.error(f"Error fetching positions: {e}")
                    options = []

                # Build map: conId -> position data (including qty for
                # short/long detection)
                option_map = {}
                for p in options:
                    con_id = p.get('conId', 0)
                    if con_id:
                        option_map[con_id] = p

                if not option_map:
                    logger.info("No options to monitor.")
                    return

                # Fetch Greeks for all open options via /option/greeks
                # (conId-based)
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
                        logger.debug(
                            f"Error fetching greeks for conId={con_id}: {e}")

            # --- Evaluate Global Rules (Automated) ---
            # Rule: High Delta Warning — only for SHORT positions (sold options)
            # Collect all triggered alerts, then send a single digest message
            delta_alerts = []
            now = datetime.datetime.now()

            for con_id, data in market_data.items():
                opt = option_map.get(con_id)
                if not opt:
                    continue

                # Skip long positions (qty > 0) — only alert for sold options
                qty = opt.get('qty', 0)
                if qty >= 0:
                    continue

                delta = data.get('delta', 0.0)
                # Ignore if delta is effectively zero (no data)
                if abs(delta) < 0.0001:
                    continue

                if abs(delta) > settings.ALERT_DELTA_THRESHOLD:
                    # Check throttling: skip if alerted < 4 hours ago
                    last_alert = self.global_alert_cache.get(con_id)
                    if last_alert and (
                            now - last_alert) <= datetime.timedelta(hours=4):
                        continue

                    underlying = opt.get('underlying', '??')
                    right = opt.get('right', '?')
                    strike = opt.get('strike', 0)
                    expiry = opt.get('expiry', '')

                    strike_fmt = f"{strike:.0f}" if strike == int(strike) else f"{strike}"
                    exp_fmt = expiry.replace("-", "")
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
                    # In automated alerts, all are above threshold, so always
                    # red
                    marker = "🔴"
                    display_padded = a['display'].ljust(pad)
                    delta_str = f"{a['delta']:+.3f}".rjust(7)
                    qty_str = f"({a['qty']:.0f})".rjust(4)

                    lines.append(
                        f"{marker} <code>{display_padded} Δ{delta_str} {qty_str}</code>"
                    )
                lines.append(f"\n🔴 abs(Δ) &gt; {settings.ALERT_DELTA_THRESHOLD}")

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
                    options = [
                        p for p in positions if p.get('secType') == 'OPT']

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
                            symbol_name = opt.get('underlying', 'unknown')
                            max_retries = 3
                            for attempt in range(max_retries):
                                try:
                                    params = {
                                        'underlying': opt.get('underlying', ''),
                                        'expiry': opt.get('expiry', ''),
                                        'strike': opt.get('strike', 0),
                                        'right': opt.get('right', ''),
                                        'conId': opt.get('conId', 0),
                                        'force_refresh': 'true'
                                    }
                                    if not params['underlying'] or not params['expiry']:
                                        return False

                                    resp = await client.get(
                                        f"{settings.WEB_SERVICE_URL}/option/greeks",
                                        params=params, headers=headers
                                    )
                                    if resp.status_code == 200:
                                        data = resp.json()
                                        # Check if returned data has valid Greeks
                                        d = data.get('delta', 0)
                                        g = data.get('gamma', 0)
                                        th = data.get('theta', 0)
                                        v = data.get('vega', 0)
                                        u = data.get('underlying_price', 0)
                                        if (d == 0 and g == 0 and th == 0 and v == 0) or u == 0:
                                            if attempt < max_retries - 1:
                                                logger.debug(f"Retry {attempt + 1}/{max_retries} for {symbol_name}: Greeks invalid")
                                                await asyncio.sleep(3)
                                                continue
                                            logger.warning(f"Failed to get valid Greeks for {symbol_name} after {max_retries} attempts")
                                            return False
                                        return True
                                    else:
                                        if attempt < max_retries - 1:
                                            await asyncio.sleep(2)
                                            continue
                                        return False
                                except Exception as e:
                                    logger.error(f"Error refreshing {symbol_name}: {e}")
                                    if attempt < max_retries - 1:
                                        await asyncio.sleep(2)
                                        continue
                                    return False
                            return False

                    tasks = [refresh_one(opt) for opt in options]
                    results = await asyncio.gather(*tasks)
                    success = sum(1 for r in results if r)
                    failed = len(results) - success

                    logger.info(f"Greeks cache refresh complete: {success}/{len(options)} valid, {failed} failed")

                except Exception as e:
                    logger.error(f"Error checking positions/refreshing: {e}")

        except Exception as e:
            logger.error(f"Error in refresh_greeks_cache: {e}", exc_info=True)
