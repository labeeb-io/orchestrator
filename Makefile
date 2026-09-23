SHELL := /bin/bash
.DEFAULT_GOAL := help

CONFIG ?=
ARGS ?=
PYTHON ?= python3
CLI = ./labeeb_controller.py $(if $(CONFIG),--config "$(CONFIG)")

.PHONY: help setup doctor start run approve status background stop reconcile unblock web web-deps-check version test

help: ## Show available commands
	@printf '%s\n' \
	  'Usage: make <command> [ARGS="..."] [CONFIG=path/to/config.toml]' \
	  '' \
	  '  setup       Create .venv and install Web UI/API dependencies' \
	  '  doctor      Check local tools and configuration' \
	  '  web         Start the Web UI (pass options with ARGS)' \
	  '  start       Create a goal (pass options with ARGS)' \
	  '  run         Run or recover a goal' \
	  '  approve     Approve a goal waiting at PLAN_GATE' \
	  '  status      Show a goal status' \
	  '  background  Start an existing goal in the background' \
	  '  stop        Request a safe stop' \
	  '  reconcile   Reconcile an in-flight or ambiguous action' \
	  '  unblock     Retry a BLOCKED goal' \
	  '  version     Show the Labeeb version' \
	  '  test        Run the test suite'

setup: ## Create the virtual environment and install dependencies
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { \
	  printf '%s\n' 'Python 3.11+ is required. Override with make setup PYTHON=/path/to/python3.' >&2; \
	  exit 1; \
	}
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r requirements.txt

doctor: ## Check local tools and configuration
	$(CLI) doctor

start: ## Create a goal; pass CLI options in ARGS
	$(CLI) start $(ARGS)

run: ## Run or recover a goal; pass goal ID and options in ARGS
	$(CLI) run $(ARGS)

approve: ## Approve a goal waiting at PLAN_GATE
	$(CLI) approve $(ARGS)

status: ## Show a goal status
	$(CLI) status $(ARGS)

background: ## Start an existing goal in the background
	$(CLI) background $(ARGS)

stop: ## Request a safe stop
	$(CLI) stop $(ARGS)

reconcile: ## Reconcile an in-flight or ambiguous action
	$(CLI) reconcile $(ARGS)

unblock: ## Retry a BLOCKED goal
	$(CLI) unblock $(ARGS)

web: web-deps-check ## Start the Web UI; pass --host/--port/--reload in ARGS
	$(CLI) web $(ARGS)

web-deps-check:
	@.venv/bin/python -c 'import fastapi, uvicorn, httpx, jinja2, multipart' >/dev/null 2>&1 || { \
	  printf '%s\n' "Web UI dependencies are missing. Run 'make setup' first." >&2; \
	  exit 1; \
	}

version: ## Show the Labeeb version
	$(CLI) --version

test: test-deps-check ## Run the full pytest test suite
	PYTHONPATH=. .venv/bin/pytest tests/ $(ARGS)

test-unittest: ## Run legacy unittest discovery only
	PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v

test-deps-check:
	@.venv/bin/python -c 'import pytest' >/dev/null 2>&1 || { \
	  printf '%s\n' "pytest is missing from .venv. Run 'make setup' or '.venv/bin/pip install pytest'." >&2; \
	  exit 1; \
	}

