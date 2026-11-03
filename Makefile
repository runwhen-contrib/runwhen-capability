.PHONY: install test lint fmt fmt-check dist contracts-sync contracts-models

install:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest -q

lint:
	ruff check .

fmt:
	ruff format .

fmt-check:
	ruff format --check .

dist:
	python3 -m build

contracts-sync:
	python3 scripts/sync_contracts.py

contracts-models:
	python3 scripts/gen_contract_models.py
