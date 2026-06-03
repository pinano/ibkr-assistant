#!/bin/bash
set -e

# 1. Initialize Environment (only if .env doesn't exist)
if [ ! -f ".env" ]; then
    echo "Initializing environment..."
    ./initialize-env.sh
fi

# 2. Sync .env with .env.dist automatically
if [ -f ".env" ]; then
    echo "Checking for missing environment variables..."
    TEMP_ENV=".env.tmp"
    DIST_FILE=".env.dist"
    ENV_FILE=".env"
    ADDED=0

    # 1. First, sync everything from .env.dist
    while IFS= read -r line || [ -n "$line" ]; do
        # Preserve comments and empty lines from dist
        if [[ -z "$line" ]] || [[ "$line" == \#* ]]; then
            echo "$line" >> "$TEMP_ENV"
            continue
        fi

        # Process variables
        if [[ "$line" == *=* ]]; then
            key=$(echo "$line" | cut -d= -f1 | tr -d '[:space:]')
            
            # Use grep to find the exact key definition in existing .env
            if grep -q "^${key}=" "$ENV_FILE"; then
                grep "^${key}=" "$ENV_FILE" | head -n1 >> "$TEMP_ENV"
            else
                echo "  + Adding new variable: $key"
                echo "$line" >> "$TEMP_ENV"
                ADDED=$((ADDED + 1))
            fi
        else
            echo "$line" >> "$TEMP_ENV"
        fi
    done < "$DIST_FILE"

    # 2. Check for extra variables in .env that are NOT in .env.dist
    # Collect all keys from .env.dist
    DIST_KEYS=$(grep -o '^[^#]*=' "$DIST_FILE" | cut -d= -f1)
    
    # Iterate over .env
    HAS_UNDEFINED=0
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" == *=* ]] && [[ ! "$line" == \#* ]]; then
            key=$(echo "$line" | cut -d= -f1 | tr -d '[:space:]')
            
            # Check if this key is in DIST_KEYS
            if ! echo "$DIST_KEYS" | grep -q "^${key}$"; then
                if [ $HAS_UNDEFINED -eq 0 ]; then
                    echo "" >> "$TEMP_ENV"
                    echo "# --- DEPRECATED / CUSTOM VARIABLES ---" >> "$TEMP_ENV"
                    HAS_UNDEFINED=1
                fi
                echo "# UNDEFINED: $line" >> "$TEMP_ENV"
            fi
        fi
    done < "$ENV_FILE"

    mv "$TEMP_ENV" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    
    if [ $ADDED -gt 0 ]; then
        echo "Successfully added $ADDED new variable(s) to .env"
    fi
fi

# 3. Load PROJECT_NAME from .env
if [ -f ".env" ]; then
    PROJECT_NAME=$(grep "^PROJECT_NAME=" .env | cut -d= -f2-)
fi
PROJECT_NAME=${PROJECT_NAME:-ibkr}

# 4. Start Services
echo "Starting Docker services..."
docker compose -f docker-compose.yaml up -d --remove-orphans

echo "---------------------------------------------------"
echo "  ✅ Stack started successfully!"
echo ""
echo "  Useful commands:"
echo "    make logs     Tail logs from all containers"
echo "    make status   Show container status"
echo "    make stop     Stop the stack"
echo "---------------------------------------------------"
