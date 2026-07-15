# pytest-parallex -- task runner. uv is the only tool needed.
# One-time setup after cloning:  make install-hooks
.DEFAULT_GOAL := help
.PHONY: help install-hooks format lint typecheck test check bump build

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install-hooks: ## point git at the tracked .githooks/ (pre-commit + pre-push)
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-commit .githooks/pre-push scripts/bump-version scripts/check-version-bump
	@echo "hooks installed: core.hooksPath -> .githooks"

format: ## auto-format + autofix lint (ruff)
	uv run ruff format src testing
	uv run ruff check --fix src testing

lint: ## lint (ruff) -- same as CI
	uv run ruff check src testing

typecheck: ## type-check (pyright) -- same as CI
	uv run pyright

test: ## run the test suite
	uv run pytest -q

check: lint typecheck test ## the full CI test stage: lint + typecheck + test

bump: ## bump version in pyproject.toml -- TYPE=patch|minor|major (default patch)
	./scripts/bump-version $(or $(TYPE),patch)

build: ## build sdist + wheel into dist/
	uv build --out-dir dist
