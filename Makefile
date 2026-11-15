# Convenience targets.  `make help` lists them.
.PHONY: help up down logs ps verify test test-backend test-frontend lint backup shell-db

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

up: ## build and start the stack in the background
	docker compose up -d --build

down: ## stop the stack (data is kept)
	docker compose down

logs: ## follow the application log
	docker compose logs -f app

ps: ## container state and health
	docker compose ps

verify: ## probe the Massive API with the configured key (stop the app first: one connection only)
	docker compose stop app
	docker compose run --rm app python -m fxlab.cli verify-provider
	docker compose start app

test: test-backend test-frontend ## run every test-suite

test-backend: ## backend tests (set TEST_DATABASE_URL to include the database tests)
	cd backend && python -m pytest

test-frontend: ## dashboard type-check, unit tests and build
	cd frontend && npm run typecheck && npm test && npm run build

lint: ## backend lint, format check and strict typing
	cd backend && ruff check fxlab tests && ruff format --check fxlab tests migrations && mypy

backup: ## dump the database to backups/fxlab-<timestamp>.dump
	mkdir -p backups
	docker compose exec -T db pg_dump -U fxlab -Fc fxlab > backups/fxlab-$$(date -u +%Y%m%dT%H%M%SZ).dump

shell-db: ## psql inside the database container
	docker compose exec db psql -U fxlab fxlab
