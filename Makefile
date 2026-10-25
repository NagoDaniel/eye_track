.PHONY: dev test test-images test-stack test-dev test-setup test-fetch-models test-backup test-install-docker lint lint-shell e2e e2e-throttle

# Recorded in each analysis run (PLAN §9); empty outside a git checkout.
export GIT_SHA := $(shell git describe --always --dirty --abbrev=40 2>/dev/null)

COMPOSE_DEV = docker compose -f docker-compose.yml -f compose.dev.yml

# Development stack on http://localhost:8080 (PLAN §11.5). Needs a `.env` with real secrets.
dev:
	@test -f .env || { \
	  echo "make dev: no .env found." >&2; \
	  echo "  run ./scripts/setup.sh --env-only to create it with random secrets" >&2; \
	  echo "  (DOMAIN and TLS_MODE are not used in dev)." >&2; \
	  exit 1; }
	$(COMPOSE_DEV) up --build

test:
	cd backend && uv run pytest
	cd frontend && npm test

test-images:
	./scripts/test-backend-image.sh
	./scripts/test-proxy-image.sh

test-dev:
	./scripts/test-dev-stack.sh

test-setup:
	./scripts/test-setup.sh

test-fetch-models:
	./scripts/test-fetch-models.sh

test-backup:
	./scripts/test-backup-restore.sh

test-install-docker:
	./scripts/test-install-docker.sh

test-stack:
	./scripts/test-compose-stack.sh

lint:
	cd backend && uv run ruff check . ../shared && uv run ruff format --check . ../shared && uv run mypy
	cd frontend && npm run lint && npm run typecheck
	cd e2e && npm run typecheck

# shellcheck for every shell script, run in a container so the host needs only Docker.
lint-shell:
	docker run --rm -v "$(CURDIR):/mnt" koalaman/shellcheck:stable proxy/entrypoint.sh scripts/*.sh

# Playwright end-to-end test against the production stack (PLAN §13).
e2e:
	./scripts/e2e.sh

# How playback and tracking hold up on a slow machine: the browser's CPU slowed down 1, 4 and 6 times.
# Prints numbers, never fails on them (DECISIONS 2026-10-25). E2E_THROTTLE="1,2,4" to change.
e2e-throttle:
	E2E_THROTTLE="$${E2E_THROTTLE:-1,4,6}" ./scripts/e2e.sh
