COMPOSE = docker compose -f docker-compose.yaml

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
	@echo "  make restart   Restart the Docker stack (stop + start)"
	@echo "  make rebuild   Rebuild image (use s=<service> for single service)"
	@echo "  make clean     Remove dangling/untagged images (<none>)"
	@echo "  make logs      Tail logs from all containers"
	@echo "  make status    Show container status"
	@echo ""

init:
	@bash src/initialize-env.sh

start:
	@bash src/start.sh

stop:
	@bash src/stop.sh

restart:
	@bash src/stop.sh
	@bash src/start.sh

rebuild:
	$(COMPOSE) build --no-cache $(s)
	$(COMPOSE) up -d --force-recreate --remove-orphans $(s)

logs:
	$(COMPOSE) logs -f

clean:
	@echo "Removing dangling images..."
	docker image prune -f

status:
	$(COMPOSE) ps

db:
	$(COMPOSE) exec ibkr-db sh -c 'mariadb -u root -p"$$MARIADB_ROOT_PASSWORD" "$$MARIADB_DATABASE"'
