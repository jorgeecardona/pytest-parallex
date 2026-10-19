# pytest-parallex -- human task verbs. These are thin aliases: the CI-graded
# tasks (lint/typecheck/test/build) delegate to the matching moon task, so the
# command + its inputs/outputs live in ONE place (moon.yml) and `make` runs
# exactly what CI runs, with moon's caching. Only the verbs moon has no task for
# (format = write, bump, install-hooks) run a command directly here.
# One-time setup after cloning:  make install-hooks
.DEFAULT_GOAL := help
.PHONY: help install-hooks format lint typecheck test check bump docs build

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

lint: ## lint (ruff) -- the CI :lint task
	moon run :lint

typecheck: ## type-check (pyright) -- the CI :typecheck task
	moon run :typecheck

test: ## run the test suite -- the CI :test task
	moon run :test

check: lint typecheck test ## the full CI test stage: lint + typecheck + test

bump: ## bump __version__ in src/pytest_parallex/__init__.py -- TYPE=patch|minor|major (default patch)
	./scripts/bump-version $(or $(TYPE),patch)

docs: ## build the mkdocs site -- the CI :docs task
	moon run :docs

build: ## build sdist + wheel into dist/ -- the CI :build task
	moon run :build
