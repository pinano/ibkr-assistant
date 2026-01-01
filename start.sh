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

    # Ensure we use a clean temp file
    > "$TEMP_ENV"

    while IFS= read -r line || [ -n "$line" ]; do
        # Preserve comments and empty lines
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

    mv "$TEMP_ENV" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    
    if [ $ADDED -gt 0 ]; then
        echo "Successfully added $ADDED new variable(s) to .env"
    fi
fi

# 3. Load PROJECT_ID from .env
if [ -f ".env" ]; then
    PROJECT_ID=$(grep "^PROJECT_ID=" .env | cut -d= -f2-)
fi
PROJECT_ID=${PROJECT_ID:-ibkr}

# 3. Start Services
echo "Starting Docker services..."
docker compose up -d --build --force-recreate --remove-orphans

echo "---------------------------------------------------"
echo "Stack started successfully!"
echo "Bot logs:        docker logs -f ${PROJECT_ID}-bot"
echo "API logs:        docker logs -f ${PROJECT_ID}-api"
echo "IB Gateway logs: docker logs -f ${PROJECT_ID}-gateway"
echo "Database logs:   docker logs -f ${PROJECT_ID}-db"
echo "---------------------------------------------------"
