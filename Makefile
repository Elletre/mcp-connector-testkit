.PHONY: help install lint fmt types unit layers test check matrix evals replay docs fixtures screenshot clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-11s\033[0m %s\n", $$1, $$2}'

install: ## Create the environment: the kit, the demo and the dev tools
	uv sync --all-groups

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Format the code
	uv run ruff format .
	uv run ruff check --fix .

types: ## Type-check the kit (strict)
	uv run mypy

unit: ## The kit's own tests: checks fire, quotes match, statistics hold
	uv run pytest tests/unit -n 4

layers: ## Layers 1-4 against the demo connector
	uv run pytest -m "layer1 or layer2 or layer3 or layer4" -n 4

replay: ## Layer 5 without a model: replay the recorded run
	uv run pytest tests/layer5_agent

test: unit layers replay ## Everything that needs no model

check: lint types test ## What CI runs on a pull request
	uv run python tools/gen_docs.py --check

AGENT_RESULTS = $(foreach d,vague-tool-descriptions untrusted-content-unmarked,$(d)=evals/results/compare-$(d).json)

matrix: ## Every seeded defect through layers 1-4, plus the agent layer's verdicts (slow)
	uv run python tools/defect_matrix.py --jobs 4 --agent-results $(AGENT_RESULTS)

evals: ## Agent-level evaluation with the local model (needs Ollama)
	uv run mcpqa evals run --environment evals/acme_environment.py:make --model ollama:llama3:latest

docs: ## Regenerate the catalogues, the matrix page and the eval results from committed files
	uv run python tools/gen_docs.py
	uv run python tools/defect_matrix.py --render-only --agent-results $(AGENT_RESULTS)
	uv run python tools/eval_results.py

fixtures: ## Regenerate the demo mailboxes (deterministic)
	uv run python tools/gen_fixtures.py

screenshot: ## Re-render the README image from a real run
	uv run python tools/render_screenshot.py

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage out .mcpqa-results.json
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
