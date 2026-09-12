# AGENTS.md

Guidance for AI coding agents working in this repo. Humans: read README.md and CHANGELOG.md.

## Overview

pytest-parallex: parallel pytest where session fixtures run once for the whole run, not once per worker.

It is a pytest plugin (entry point `pytest11 = parallex`). The controller collects once, builds
`scope="session"` fixtures once, then forks warm workers that inherit them via copy-on-write.
Source lives in `src/pytest_parallex/`; the suite is in `testing/`.

## Build

- `uv sync` installs dependencies from `uv.lock`.
- Toolchain (moon, uv) is pinned in `.prototools`; `proto install` brings it up.
- `make install-hooks` once, to enable the tracked `.githooks/` (pre-commit + pre-push).
- `make build` builds sdist + wheel into `dist/` (`uv build`).

## Test

- `make test` runs the suite (`uv run pytest -q`).
- `make check` runs lint + typecheck + test (the same stage CI runs).
- CI runs the whole graph with `moon run :ci` (on `main`, `moon run :release`).
- `moon run pytest-parallex:test-pytest-floor` runs the suite against the pinned floor
  (Python 3.10 + pytest 8.1.1); the internals this plugin patches move between pytest minors,
  so run it after any change to fixture or fork handling.

## Style

- Format and autofix: `make format` (ruff format + `ruff check --fix`, line length 100).
- Lint: `make lint` (ruff, `select = ["ALL"]` with each opt-out justified in `pyproject.toml`).
- Types: `make typecheck` (pyright `strict` on `src` and `testing`, `pythonVersion` 3.10).
- Keep the public API typed (`py.typed` ships). Everything in `make check` must pass before you commit.

## Gotchas

- The plugin reaches into pytest privates by design (`_pytest.runner`, `FixtureDef.cached_result`,
  `Session._setupstate`, `TestReport._to_json`). `reportPrivateUsage` is off and several ruff rules
  are opted out for this reason; do not "fix" those by removing the private access.
- `requires-python >= 3.10`, `pytest >= 8.1` (not 8.0: `getfixturedefs` took a Node only from 8.1).
  Do not raise or lower either floor without a reason.
- Version lives only in `pyproject.toml`. The pre-commit hook auto-bumps it when `src/` changes, and
  CI `version-guard` fails a `src/` change that carries no bump. Do not edit versions by hand elsewhere.
- Merging a version bump to `main` publishes to PyPI via OIDC (no stored token). Bump deliberately.
- Local imports inside functions are load-bearing: they defer work past fork/collection time. Keep them local.
- No `docs/issues/` convention here yet; do not invent one.
