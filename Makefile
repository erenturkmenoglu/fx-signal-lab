# Convenience targets.  `make help` lists them.
.PHONY: help up down logs ps verify test test-backend test-frontend lint backup restore shell-db

# database that backup and restore work on (the simulator's is fxlab_mock)
DB ?= fxlab

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

backup: ## dump the database to backups/<database>-<timestamp>.dump
	@mkdir -p backups
	@f=backups/$(DB)-$$(date -u +%Y%m%dT%H%M%SZ).dump; \
	if docker compose exec -T db pg_dump -U fxlab -Fc $(DB) > $$f.part; then \
		mv $$f.part $$f && echo "written: $$f"; \
	else \
		rm -f $$f.part; echo "backup failed: nothing written" >&2; exit 1; \
	fi

restore: ## restore a dump into an empty database: make restore FILE=backups/<file>.dump
	@test -n "$(FILE)" && test -f "$(FILE)" || { \
		echo "usage: make restore FILE=backups/<file>.dump   (application stopped, database empty)" >&2; exit 2; }
	@! docker compose ps --status running --services | grep -qx app || { \
		echo "the application is running: stop it first (docker compose stop app)" >&2; exit 2; }
	@test "$$(docker compose exec -T db psql -U fxlab -d $(DB) -Atc "select to_regclass('public.instruments') is null")" = "t" || { \
		echo "the database $(DB) already has tables: restore only into an empty one (docs/RUNBOOK.md)" >&2; exit 2; }
	@# TimescaleDB must be in its restore mode while pg_restore runs; a plain pg_restore
	@# fails on the foreign keys of the hypertables.
	docker compose exec -T db psql -U fxlab -d $(DB) -v ON_ERROR_STOP=1 -qAt \
		-c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" > /dev/null
	@status=0; docker compose exec -T db pg_restore -U fxlab -d $(DB) < "$(FILE)" || status=$$?; \
	docker compose exec -T db psql -U fxlab -d $(DB) -v ON_ERROR_STOP=1 -qAt -c "SELECT timescaledb_post_restore()" > /dev/null || status=1; \
	if [ $$status -eq 0 ]; then echo "restored: $(FILE) into $(DB)"; else echo "restore FAILED (see the messages above)" >&2; fi; \
	exit $$status

shell-db: ## psql inside the database container
	docker compose exec db psql -U fxlab fxlab
