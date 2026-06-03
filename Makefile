# Makefile - Project Management
# Wraps existing scripts and provides utility commands for the Docker stack.

# =============================================================================
# CONFIGURATION
# =============================================================================

# Default shell
SHELL := /bin/bash

# Default target
.DEFAULT_GOAL := help

# ==============================================================================
# HELP COMMAND INTERCEPTOR
# ==============================================================================
# Intercepts 'make <target> help' or 'make help <target>' and routes to 'make help-<target>'
ifneq ($(filter help,$(MAKECMDGOALS)),)
  HELP_TARGET := $(firstword $(filter-out help,$(MAKECMDGOALS)))
  ifneq ($(HELP_TARGET),)
    # Turn all targets except help into dummy targets to suppress "No rule to make target"
    $(eval $(filter-out help,$(MAKECMDGOALS)):;@:)
    # Make 'help' execute the specific help target
    $(eval help:;@$(MAKE) -s help-$(HELP_TARGET))
    # Skip parsing the rest of the Makefile to avoid overriding warnings
    SKIP_MAKEFILE := 1
  endif
endif

ifndef SKIP_MAKEFILE

# Load environment variables from .env if it exists
ifneq (,$(wildcard .env))
    include .env
    export
endif

# Base Docker Compose command
COMPOSE := docker compose -f docker-compose.yaml

# Default tail for logs (can be overridden with tail=N)
tail ?= all

# Helper: Extract arguments for logs, restart, rebuild and shell commands
# This allows using "make logs ibkr-api" instead of "make logs s=ibkr-api"
SUPPORTED_COMMANDS := logs shell restart rebuild
SUPPORTS_ARGS := $(filter $(firstword $(MAKECMDGOALS)),$(SUPPORTED_COMMANDS))
ifneq "$(SUPPORTS_ARGS)" ""
  # The remaining arguments are the service names
  SERVICE_ARGS := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
  # Turn them into do-nothing targets so make doesn't complain
  $(eval $(SERVICE_ARGS):;@:)
endif

# =============================================================================
# TARGETS
# =============================================================================

##@ General

##@help help
## Displays this help message with a grouped list of all available commands.
## To see detailed help about any specific command, run 'make <command> help'.
.PHONY: help
help: ## Show this help message
	@echo "Usage: make [target] [service]"
	@echo "For detailed help on any command, run: make <target> help (e.g., make start help)"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-25s\033[0m %s\n", $$1, $$2 } /^##@ / { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

help-%:
	@awk -v target=$* ' \
	/^##@help / { if ($$2 == target) { flag=1; next } else { flag=0 } } \
	/^## / { if (flag) print substr($$0, 4) } \
	/^[^#]/ { flag=0 } \
	' $(MAKEFILE_LIST)

##@ Versioning & Updates

##@help release
## Generates a new CalVer release (YYYY.MM.DD).
## - Aborts if there are uncommitted changes or no new commits since last release.
## - Must be run on the 'main' branch.
## - Generates/prepends CHANGELOG.md entries from git commit history.
## - Updates the VERSION file.
## - Tags the repository and commits the changes.
.PHONY: release
release: ## Generate a new CalVer release, update CHANGELOG.md, and create a git tag
	@./scripts/release.sh

##@help update
## Safely updates the codebase to the latest release (or a specific tag).
## - Fetches the latest tags from the remote repository.
## - Checks out the highest available version tag (or the specified tag).
## - Interactively prompts you to rebuild and restart the stack to apply changes.
## Usage: make update [version=vYYYY.MM.DD]
.PHONY: update
update: ## Fetch and safely upgrade the codebase (usage: make update [version=vX])
	@./scripts/update.sh $(version)

##@help rollback
## Interactively lists the last 10 versions and allows you to rollback.
## - Fetches the last 10 tags.
## - Presents a numbered list to choose from.
## - Performs a safe git checkout to the selected tag.
## - Interactively prompts to rebuild and restart the stack.
.PHONY: rollback
rollback: ## Interactively list recent versions and rollback to a specific one
	@./scripts/rollback.sh

##@ Environment & Config

##@help init
## Initializes the environment configuration.
## - Wrapper around src/initialize-env.sh.
## - Interactively syncs/creates the .env file from .env.dist.
.PHONY: init
init: ## Initialize environment configuration (.env)
	@bash src/initialize-env.sh

##@ Core Lifecycle

##@help start
## Starts the Docker stack containers in the background.
## - If .env does not exist, it runs the initialization first.
## - Runs the automatic .env validation and sync process against .env.dist.
.PHONY: start
start: ## Start the Docker stack (calls start.sh)
	@bash src/start.sh

##@help stop
## Safely stops and removes all containers in the stack.
## - Keeps persistent volumes intact (like database data).
.PHONY: stop
stop: ## Stop the Docker stack (calls stop.sh)
	@bash src/stop.sh

##@help restart
## Restarts the entire stack or specific services.
## - Full restart: make restart (runs stop.sh and start.sh)
## - Specific service: make restart ibkr-api (runs compose restart for that service)
.PHONY: restart
restart: ## Restart the stack or a specific service (usage: make restart [service])
ifneq ($(strip $(SERVICE_ARGS)),)
	@echo "Restarting service(s): $(SERVICE_ARGS)..."
	@$(COMPOSE) restart $(SERVICE_ARGS)
else ifdef s
	@echo "Restarting service: $(s)..."
	@$(COMPOSE) restart $(s)
else
	@bash src/stop.sh
	@bash src/start.sh
endif

##@help rebuild
## Rebuilds image(s) from scratch and recreates container(s).
## - All services: make rebuild
## - Specific service: make rebuild ibkr-api
.PHONY: rebuild
rebuild: ## Rebuild services from Dockerfile (usage: make rebuild [service])
ifneq ($(strip $(SERVICE_ARGS)),)
	@echo "Rebuilding service(s): $(SERVICE_ARGS)..."
	$(COMPOSE) build --no-cache $(SERVICE_ARGS)
	$(COMPOSE) up -d --force-recreate --remove-orphans $(SERVICE_ARGS)
else ifdef s
	@echo "Rebuilding service: $(s)..."
	$(COMPOSE) build --no-cache $(s)
	$(COMPOSE) up -d --force-recreate --remove-orphans $(s)
else
	@echo "Rebuilding all services..."
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d --force-recreate --remove-orphans
endif

##@ Debugging & Maintenance

##@help status
## Shows the status of all running containers in the stack.
.PHONY: status
status: ## Show container status (docker compose ps)
	$(COMPOSE) ps

##@help logs
## Follows the logs of one or all services.
## - All services: make logs
## - Specific service: make logs ibkr-api
## - Custom tail: make logs tail=50
.PHONY: logs
logs: ## Follow logs (usage: make logs [service] [tail=N])
ifneq ($(strip $(SERVICE_ARGS)),)
	@echo "Following logs for service: $(SERVICE_ARGS) (tail=$(tail))..."
	@-$(COMPOSE) logs -f --tail=$(tail) $(SERVICE_ARGS)
else ifdef s
	@echo "Following logs for service: $(s) (tail=$(tail))..."
	@-$(COMPOSE) logs -f --tail=$(tail) $(s)
else
	@echo "Following logs for ALL services (tail=$(tail))..."
	@-$(COMPOSE) logs -f --tail=$(tail)
endif

##@help shell
## Opens an interactive shell (/bin/sh) inside a service container.
## - Usage: make shell ibkr-api
.PHONY: shell
shell: ## Open a shell in a container (usage: make shell [service])
ifneq ($(strip $(SERVICE_ARGS)),)
	@$(COMPOSE) exec -it $(SERVICE_ARGS) /bin/sh
else ifdef s
	@$(COMPOSE) exec -it $(s) /bin/sh
else
	@echo "Error: Please specify a service name (e.g., 'make shell ibkr-api')."
endif

##@help db
## Opens a database command line client inside the ibkr-db container.
.PHONY: db
db: ## Open MariaDB console inside container
	$(COMPOSE) exec ibkr-db sh -c 'mariadb -u root -p"$$MARIADB_ROOT_PASSWORD" "$$MARIADB_DATABASE"'

##@help ctop
## View real-time container metrics (CPU, memory, traffic, IO) using ctop interface.
.PHONY: ctop
ctop: ## View container metrics with ctop
	@PROJECT_NAME=$$(grep '^PROJECT_NAME=' .env 2>/dev/null | cut -d= -f2 | head -1 || echo ibkr); \
	docker run --rm -ti \
		--name=ctop \
		--volume /var/run/docker.sock:/var/run/docker.sock:ro \
		elswork/ctop:latest -f "$$PROJECT_NAME"

##@help check-updates
## Scans compose files and audits registries for newer Docker image tags.
## Filters tags matching the current tag's flavor (e.g. alpine, slim, stable).
.PHONY: check-updates
check-updates: ## Check for Docker image updates
	@python3 scripts/check-image-updates.py

##@help clean
## Removes dangling/untagged Docker images (<none>) to free disk space.
.PHONY: clean
clean: ## Remove dangling/untagged docker images
	@echo "Removing dangling images..."
	docker image prune -f

endif # SKIP_MAKEFILE
