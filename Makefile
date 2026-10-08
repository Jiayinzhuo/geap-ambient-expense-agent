.PHONY: install serve run playground test generate-traces grade clean help

# Default target
all: install

help:
	@echo "Available targets:"
	@echo "  make install         - Install project dependencies with uv"
	@echo "  make serve           - Stand up ambient web service on port 8080 (Pub/Sub triggers)"
	@echo "  make run             - Alias for make serve"
	@echo "  make playground      - Launch local ADK 2.0 Web UI Playground"
	@echo "  make test            - Run unit test suite with pytest"
	@echo "  make generate-traces - Run evaluation scenarios and generate trace artifacts"
	@echo "  make grade           - Grade generated traces using agents-cli eval and LLM judges"
	@echo "  make clean           - Remove cache and build artifacts"

install:
	uv sync

serve:
	uv run uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8080

run: serve

playground:
	uv run uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8080

test:
	uv run pytest tests/unit/ -v

generate-traces:
	uv run python tests/eval/generate_traces.py

grade:
	agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info

