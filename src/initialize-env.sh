#!/bin/bash
set -u

# Wrapper script to initialize environment using Python for robust handling
# of variable merging and interactive configuration.

# Absolute path to the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Ensure we are in the project root
cd "$PROJECT_ROOT"

# Check for Python 3
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is required but not installed."
    exit 1
fi

echo "Initializing environment configuration..."

# delegate to Python script
python3 src/sync_env.py

RET=$?
if [ $RET -ne 0 ]; then
    echo "Environment initialization failed or aborted."
    exit $RET
fi

# Ensure required directories exist with correct user ownership
# (These are needed for Docker volume mounts)
if [ ! -d "mariadb_data" ]; then
    echo ""
    echo "Creating directory: mariadb_data"
    mkdir -p mariadb_data
fi

if [ ! -d "flex_queries" ]; then
    echo "Creating directory: flex_queries"
    mkdir -p flex_queries
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Initialization Complete."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
