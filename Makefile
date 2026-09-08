PYTHON := python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: install test lint fmt \
        run-mcp-server run-mcp-gateway run-llm-gateway run-rate-limiter \
        run-mock-mcp run-mock-llm run-mock-llm-secondary smoke clean

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

test:
	$(BIN)/pytest

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

# --- task services (each a no-op stub until its task is implemented) ---

run-mcp-server:
	$(BIN)/python -m mcp_server

run-mcp-gateway:
	$(BIN)/uvicorn mcp_gateway.app:app --reload --port 8010

run-llm-gateway:
	$(BIN)/uvicorn llm_gateway.app:app --reload --port 8020

run-rate-limiter:
	$(BIN)/uvicorn rate_limiter.app:app --reload --port 8030

# --- mock upstreams (implemented) ---

run-mock-mcp:
	$(BIN)/uvicorn mocks.mcp_downstream:app --port 9001

run-mock-llm:
	$(BIN)/uvicorn mocks.llm_provider:app --port 9002

# Same mock, second port — stands in as Task 4's "secondary" model provider.
run-mock-llm-secondary:
	$(BIN)/uvicorn mocks.llm_provider:app --port 9003

smoke:
	./scripts/smoke.sh

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
