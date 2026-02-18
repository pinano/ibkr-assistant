#!/bin/bash

echo "Stopping Docker services..."
docker compose -f docker-compose.yaml down

echo "---------------------------------------------------"
echo "Stack stopped successfully."
echo "---------------------------------------------------"
