.PHONY: install dev test test-core release-gate live-smoke lint run skill appium benchmark contracts docker

install:
	python -m pip install -e .

dev:
	python -m pip install -e '.[dev,skill]'

test:
	python -m pytest -q -o addopts=''

test-core:
	python scripts/release_gate.py --core

release-gate:
	python scripts/release_gate.py --release

live-smoke:
	python scripts/live_smoke.py

lint:
	python -m ruff check .
	python -m mypy src/lobster_phone_agent

run:
	lobster-phone-agent

skill:
	lobster-phone-skill --config /etc/lobster-phone-skill.yaml

appium:
	bash scripts/bootstrap_appium.sh

benchmark:
	PYTHONPATH=src python scripts/benchmark.py

contracts:
	PYTHONPATH=src python scripts/generate_contracts.py

docker:
	docker compose -f deploy/docker-compose.yml up --build
