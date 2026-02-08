# Makefile for matrx-sandbox

# Doppler environment sync commands
env-pull:
	@doppler secrets download --project matrx-sandbox --config dev --no-file --format env > .env
	@echo "✓ Env updated from Doppler"

env-push:
	@doppler secrets upload --project matrx-sandbox --config dev .env
	@echo "✓ Env pushed to Doppler"

# Test commands
test:
	cd orchestrator && python -m pytest tests/ -v --ignore=tests/test_integration.py

test-integration:
	@echo "Starting docker-compose services..."
	docker compose up -d
	@echo "Waiting for orchestrator to be healthy..."
	@for i in $$(seq 1 30); do \
		curl -sf http://localhost:8000/health > /dev/null 2>&1 && break; \
		sleep 2; \
	done
	cd orchestrator && python -m pytest tests/test_integration.py -v --run-integration
	@echo "Stopping docker-compose services..."
	docker compose down

.PHONY: env-pull env-push test test-integration
