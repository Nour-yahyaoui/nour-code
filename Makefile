# Development helpers for nour-code.
PYTHON ?= python3
VENV ?= .venv
PY := $(VENV)/bin/python
PORT ?= 8000

.PHONY: help venv install lint fmt test run docker clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtual environment
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

install: venv ## Install runtime + dev dependencies
	$(PY) -m pip install -r requirements.txt -r requirements-dev.txt

lint: ## Ruff lint
	$(PY) -m ruff check nour_code tests

fmt: ## Ruff autofix + import sorting
	$(PY) -m ruff check nour_code tests --fix

test: ## Run the test suite
	$(PY) -m pytest tests -q

run: ## Start the server (uvicorn)
	$(PY) -m nour_code --port $(PORT)

docker: ## Build the Docker image
	docker build -t nour-code:latest .

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache build dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
