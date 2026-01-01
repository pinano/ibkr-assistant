#!/bin/bash
set -e

# 1. Initialize Environment (only if .env doesn't exist)
if [ ! -f ".env" ]; then
    echo "Initializing environment..."
    ./initialize-env.sh
fi

# 2. Load PROJECT_ID from .env
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
