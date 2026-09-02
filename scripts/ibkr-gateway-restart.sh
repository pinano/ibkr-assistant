#!/bin/bash
set -euo pipefail

# ==============================================================================
# IBKR Gateway & Services Daily Clean Restart
# ==============================================================================
# Restarts IBKR gateways and downstream services daily to ensure fresh session
# tokens, preventing stale authentication token errors after IBKR server resets.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${LOG_FILE:-$SCRIPT_DIR/ibkr-gateway-restart.log}"
exec >> "$LOG_FILE" 2>&1

echo "================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting daily IBKR clean restart"
echo "================================================================="

# Detect gateway containers if not passed as arguments
if [ "$#" -gt 0 ]; then
    GATEWAY_CONTAINERS=("$@")
else
    # Find all running gateway containers (e.g. ib1-gateway, ib2-gateway, ibkr-gateway)
    mapfile -t GATEWAY_CONTAINERS < <(docker ps --format '{{.Names}}' | grep -E 'gateway$' || true)
fi

if [ ${#GATEWAY_CONTAINERS[@]} -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] No gateway containers found to restart."
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Restarting gateway containers: ${GATEWAY_CONTAINERS[*]}..."
docker restart "${GATEWAY_CONTAINERS[@]}"

# Wait for gateways to initialize and listen on internal API port (4001 live / 4002 paper)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Waiting for gateways to authenticate and open API ports..."
for gw in "${GATEWAY_CONTAINERS[@]}"; do
    READY=0
    for i in {1..30}; do
        if docker exec "$gw" bash -c 'PORT=$([ "$TRADING_MODE" = "paper" ] && echo 4002 || echo 4001); < /dev/tcp/localhost/$PORT' 2>/dev/null; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Container $gw is authenticated and ready (took ~ $((i * 2))s)."
            READY=1
            break
        fi
        sleep 2
    done
    if [ "$READY" -ne 1 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: $gw timed out waiting for API port."
    fi
done

# Restart associated API and Bot containers for clean connection pools
# Derive project prefixes from gateway names (e.g. 'ib1-gateway' -> 'ib1-api', 'ib1-bot')
DOWNSTREAM_CONTAINERS=()
for gw in "${GATEWAY_CONTAINERS[@]}"; do
    PREFIX="${gw%-gateway}"
    for svc in api bot; do
        CONTAINER="${PREFIX}-${svc}"
        if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
            DOWNSTREAM_CONTAINERS+=("$CONTAINER")
        fi
    done
done

if [ ${#DOWNSTREAM_CONTAINERS[@]} -gt 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Restarting downstream services: ${DOWNSTREAM_CONTAINERS[*]}..."
    docker restart "${DOWNSTREAM_CONTAINERS[@]}"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] IBKR daily restart finished successfully."
