.PHONY: init start stop rebuild logs status help

# Default target
help:
	@echo ""
	@echo "  IBKR Assistant - Management Commands"
	@echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  make init      Initialize .env from .env.dist (interactive)"
	@echo "  make start     Start the Docker stack (builds if needed)"
	@echo "  make stop      Stop the Docker stack"
	@echo "  make rebuild   Rebuild image from scratch and recreate containers"
	@echo "  make logs      Tail logs from all containers"
	@echo "  make status    Show container status"
	@echo ""

init:
	@bash src/initialize-env.sh

start:
	@bash src/start.sh

stop:
	@bash src/stop.sh

rebuild:
	docker compose build --no-cache
	docker compose up -d --force-recreate --remove-orphans

logs:
	docker compose logs -f

status:
	docker compose ps
