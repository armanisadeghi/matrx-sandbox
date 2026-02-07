# Makefile for matrx-sandbox

# Doppler environment sync commands
env-pull:
	@doppler secrets download --project matrx-sandbox --config dev --no-file --format env > .env
	@echo "✓ Env updated from Doppler"

env-push:
	@doppler secrets upload --project matrx-sandbox --config dev .env
	@echo "✓ Env pushed to Doppler"

.PHONY: env-pull env-push
