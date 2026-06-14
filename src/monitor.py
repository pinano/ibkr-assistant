import logging
import asyncio
import httpx
import datetime

from src.config import settings

logger = logging.getLogger("ibkr-monitor")


class Monitor:
    def __init__(self, db_session_factory):
        self.SessionLocal = db_session_factory
        # Cache for global alerts to avoid spam: {symbol: last_alert_time}
        self.global_alert_cache = {}

    async def prune_old_snapshots(self, days_retention=30):
        """Delete option snapshots older than retention period."""
        from src.models import OptionSnapshot
        logger.info(f"Pruning option snapshots older than {days_retention} days...")
        try:
            with self.SessionLocal() as session:
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

    async def prune_old_market_cache(self, days_retention=7):
        """Delete market cache entries older than retention period."""
        from src.models import MarketCache
        logger.info(f"Pruning market cache older than {days_retention} days...")
        try:
            with self.SessionLocal() as session:
                cutoff_date = datetime.datetime.now() - datetime.timedelta(days=days_retention)
                deleted_count = session.query(MarketCache).filter(
                    MarketCache.updated_at < cutoff_date
                ).delete()
                session.commit()
                if deleted_count > 0:
                    logger.info(f"Pruned {deleted_count} old market cache entries.")
                else:
                    logger.info("No old market cache entries to prune.")
        except Exception as e:
            logger.error(f"Error pruning market cache: {e}")

    async def check_alerts(self):
        from src.bot import notify_admins
        logger.info("Checking alerts...")
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
                # (conId-based) — parallelized with controlled concurrency
                market_data = {}  # conId -> greeks
                semaphore = asyncio.Semaphore(3)

                async def fetch_greeks(con_id, opt):
                    async with semaphore:
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
                                return (con_id, r.json())
                        except Exception as e:
                            logger.debug(
                                f"Error fetching greeks for conId={con_id}: {e}")
                        return (con_id, None)

                results = await asyncio.gather(
                    *[fetch_greeks(cid, opt) for cid, opt in option_map.items()]
                )
                for con_id, data in results:
                    if data is not None:
                        market_data[con_id] = data
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

                # Skip tickers excluded from alerts (e.g. box spreads)
                underlying = opt.get('underlying', '')
                if (underlying.upper() in settings.delta_alert_exclude_list or
                        underlying.split(':')[-1].upper() in settings.delta_alert_exclude_list):
                    continue

                delta = data.get('delta', 0.0)
                # Ignore if delta is effectively zero (no data)
                if abs(delta) < 0.0001:
                    continue

                if abs(delta) > settings.DELTA_ALERT_THRESHOLD:
                    # Check throttling: skip if alerted < 4 hours ago
                    last_alert = self.global_alert_cache.get(con_id)
                    if last_alert and (
                            now - last_alert) <= datetime.timedelta(hours=4):
                        continue

                    underlying = opt.get('underlying', '??')
                    display_und = (underlying.split(':')[-1] if ':' in underlying else underlying)[:5]
                    right = opt.get('right', '?')
                    strike = opt.get('strike', 0)
                    expiry = opt.get('expiry', '')

                    strike_fmt = f"{strike:.0f}" if strike == int(strike) else f"{strike}"
                    exp_fmt = expiry.replace("-", "")

                    # Capture price data for IV/TV calculation
                    raw_last = data.get('last_price', 0.0)
                    raw_und = data.get('underlying_price', 0.0)
                    last_price = None
                    underlying_price = None
                    if raw_last and raw_last > 0:
                        last_price = raw_last
                    if raw_und and raw_und > 0:
                        underlying_price = raw_und

                    # Compute intrinsic value and time value
                    intrinsic = None
                    time_value = None
                    if last_price is not None and underlying_price is not None and strike:
                        right_upper = right.upper()
                        if right_upper == 'P':
                            intrinsic = max(0.0, strike - underlying_price)
                        else:  # Call
                            intrinsic = max(0.0, underlying_price - strike)
                        time_value = last_price - intrinsic

                    # Calculate data age in minutes
                    age_str = ""
                    last_date_str = data.get('last_date')
                    if last_date_str:
                        try:
                            last_dt = datetime.datetime.strptime(last_date_str, "%Y-%m-%d %H:%M:%S")
                            age_min = int((datetime.datetime.now() - last_dt).total_seconds() / 60)
                            age_str = f"{age_min}m" if age_min >= 2 else ""
                        except (ValueError, TypeError):
                            pass

                    delta_alerts.append({
                        'con_id': con_id,
                        'underlying': display_und,
                        'right': right,
                        'strike': strike,
                        'right_strike': f"{right}{strike_fmt}",
                        'expiry': exp_fmt,
                        'delta': delta,
                        'qty': abs(qty),
                        'age': age_str,
                        'intrinsic': intrinsic,
                        'time_value': time_value,
                    })

            # Send a single digest if there are any high-delta alerts
            if delta_alerts:
                # Sort by absolute delta descending
                delta_alerts.sort(key=lambda x: abs(x['delta']), reverse=True)
                
                logger.info(f"Global Delta Alert: {len(delta_alerts)} contract(s) above threshold")

                # Calculate max widths for alignment
                max_qty = max(len(f"{a['qty']:.0f}") for a in delta_alerts)
                max_und = 5
                max_rs = max(len(a['right_strike']) for a in delta_alerts)

                header = f"⚠️ <b>High Delta Warning — {len(delta_alerts)} Short Position(s)</b>\n"

                # Calculate TV strings first to determine max_tv
                for a in delta_alerts:
                    a['tv_str'] = ""
                    a['low_tv'] = False
                    if a['intrinsic'] is not None and a['time_value'] is not None:
                        tv_val = f"{a['time_value']:+.2f}".replace("+0.", "+.").replace("-0.", "-.")
                        a['tv_str'] = tv_val
                        if a['intrinsic'] > 0 and a['time_value'] <= (0.01 * a['strike']):
                            a['low_tv'] = True

                has_any_tv = any(a['tv_str'] != "" for a in delta_alerts)
                max_tv = max(len(a['tv_str']) for a in delta_alerts if a['tv_str'] != "") if has_any_tv else 0
                has_any_low_tv = any(a['low_tv'] for a in delta_alerts)

                option_lines = []
                for a in delta_alerts:
                    marker = "🔴"
                    delta_str = f"{abs(a['delta']):.2f}"
                    qty_str = f"{a['qty']:.0f}".rjust(max_qty)
                    und_padded = a['underlying'].ljust(max_und)
                    rs_padded = a['right_strike'].ljust(max_rs)
                    age = a['age']

                    # Format expiry to DDMMMyy for consistent alignment (e.g. 19Jun26)
                    display_expiry = a['expiry']
                    if len(display_expiry) == 8 and display_expiry.isdigit():
                        try:
                            dt = datetime.datetime.strptime(display_expiry, "%Y%m%d")
                            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                            month_str = months[dt.month - 1]
                            display_expiry = f"{dt.day:02d}{month_str}{dt.year % 100:02d}"
                        except Exception:
                            pass

                    # Append TV column if available
                    tv_part = ""
                    if a['tv_str']:
                        tv_padded = a['tv_str'].ljust(max_tv)
                        if has_any_low_tv:
                            warning_indicator = "⚠️" if a['low_tv'] else "  "
                            tv_part = f" {tv_padded}{warning_indicator}"
                        else:
                            tv_part = f" {tv_padded}"
                    elif has_any_tv:
                        if age:
                            padding_len = max_tv + (2 if has_any_low_tv else 0) + 1
                            tv_part = " " * padding_len

                    line = f"<code>{qty_str} {und_padded} {rs_padded} {display_expiry} {marker}{delta_str}{tv_part} {age}</code>"
                    option_lines.append(line.strip())

                options_text = "\n".join(option_lines)
                message = f"{header}\n{options_text}\n\n🔴 abs(Δ) &gt; {settings.DELTA_ALERT_THRESHOLD}\n⚠️ low time value (≤ 1% strike)"

                await notify_admins(message, parse_mode="HTML")

                # Update throttle cache for all alerted contracts
                for a in delta_alerts:
                    self.global_alert_cache[a['con_id']] = now

            # Purge stale entries from alert cache (options no longer in portfolio)
            stale_keys = [k for k in self.global_alert_cache if k not in option_map]
            for k in stale_keys:
                del self.global_alert_cache[k]

        except Exception as e:
            logger.error(f"Error in check_alerts: {e}", exc_info=True)

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
                    semaphore = asyncio.Semaphore(5)

                    async def refresh_one(opt):
                        async with semaphore:
                            symbol_name = opt.get('underlying', 'unknown')
                            # Single attempt - let the API handle retries internally
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

                                # Increased timeout because API now does internal retries (3x ~5s)
                                resp = await client.get(
                                    f"{settings.WEB_SERVICE_URL}/option/greeks",
                                    params=params, headers=headers,
                                    timeout=30.0 
                                )
                                if resp.status_code == 200:
                                    # We trust the API to return valid data if 200 OK
                                    return True
                                else:
                                    logger.warning(f"Failed to refresh {symbol_name}: Status {resp.status_code}")
                                    return False
                            except Exception as e:
                                logger.error(f"Error refreshing {symbol_name}: {e}")
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
