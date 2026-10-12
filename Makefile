.PHONY: dev test test-images test-stack lint e2e

COMPOSE_DEV = docker compose -f docker-compose.yml -f compose.dev.yml

dev:
	$(COMPOSE_DEV) up --build

test:
	cd backend && uv run pytest
	cd frontend && npm test

test-images:
	./scripts/test-backend-image.sh
	./scripts/test-proxy-image.sh

test-stack:
	./scripts/test-compose-stack.sh

lint:
	cd backend && uv run ruff check . ../shared && uv run ruff format --check . ../shared && uv run mypy
	cd frontend && npm run lint && npm run typecheck

e2e:
	@echo "e2e: not implemented until Milestone 2 (needs fake-camera fixtures, PLAN §12.4)" >&2
	@exit 1
