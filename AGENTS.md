# AGENTS.md

Guidance for AI coding agents working in this repo. Humans: read README.md, CONTRIBUTING.md and CHANGELOG.md.

## Overview

pytest-parallex is a pytest plugin that runs tests in parallel. Its distinguishing mode is `--parallel=fork`: the controller collects once, runs `scope="session"` fixtures once, then forks warm workers that inherit the imported suite and the built fixtures through copy-on-write. Three other modes exist (`thread`, `async`, `process`). The plugin reaches into pytest internals on purpose (`FixtureDef`, `Session._setupstate`, `TestReport._to_json`, the `terminalreporter` plugin), which is why ruff's `SLF001` and pyright's `reportPrivateUsage` are switched off.

Layout: `src/pytest_parallex/` is the package (`plugin.py` registers options, fixtures and hooks; one `runner_*.py` per mode; `session_scope.py` hoists session fixtures; `_safety.py` holds the fork-safety checks and the macOS hardening). Tests live in `testing/`, not `tests/`; `testing/test_regressions.py` is where a bug fix gets its pytester-based regression test.

## Build

- Toolchain (moon, uv) is pinned in `.prototools`; `proto install` brings it up. `uv` alone is enough for local work.
- `uv sync` installs dependencies into `.venv`. The only runtime dependency is `pytest>=8.1`.
- `make install-hooks` once, to point git at the tracked `.githooks/` (pre-commit + pre-push).
- `make build` builds the sdist and wheel into `dist/`.

## Test

- `make test` runs the suite (pytest, `testing/`).
- `make check` runs lint + typecheck + test. Everything in it must pass before you commit.
- CI runs the full graph with `moon run :ci` (and `moon run :release` on main). `:ci` also covers `format-check`, `build`, `docs` (mkdocs `--strict`), `version-guard`, and `test-pytest-floor` (the suite on Python 3.10 with `pytest==8.1.1`, the two floors from `pyproject.toml`).
- A bug fix needs a regression test that fails without the fix. Use the `pytester` fixture and assert on behaviour (outcomes, files the test wrote, lines in stdout), not on the plugin's internals.

## Style

- Format and autofix: `make format` (ruff, line length 100).
- Lint: `make lint` (ruff, `select = ["ALL"]`; every opt-out is justified inline in `pyproject.toml`). Types: `make typecheck` (pyright, strict).
- Comments and docstrings explain why, not what. Keep new ones to one line unless the reasoning needs more.
- Prose (README, docs, CHANGELOG, comments, commit messages): no em-dashes; use commas, colons, parentheses or separate sentences. Claim first, then evidence. Concrete names, paths and numbers over qualitative words.

## Commits

- Commit under the identity of the human you are working for, never under your own or a made-up one.
- Do not add a `Co-Authored-By` trailer or any "Generated with" line to commit messages.
- Subjects are lowercase and area-prefixed, for example `fix(fork): give each worker private first-import cache dirs`. The body says what changed and why, for a stranger reading the history later.
- Commit or push only when asked.

## Gotchas

- The version is the `__version__` literal in `src/pytest_parallex/__init__.py`; hatchling reads it so `pyproject.toml` never disagrees. The pre-commit hook auto-bumps the patch version when `src/**/*.py` changes, and CI's `version-guard` enforces that a `src/` change carries a bump. Do not edit versions by hand; `make bump TYPE=minor|major` for a deliberate larger bump.
- Merging a version bump to `main` is the release: `:release` publishes to PyPI via OIDC Trusted Publishing if that version is not there yet. No tag, no manual step.
- `docs/index.md` and `docs/changelog.md` are `--8<--` snippet includes of `README.md` and `CHANGELOG.md`, so an edit to either file changes the published site, and a broken link in them fails `mkdocs build --strict`.
- The fork worker must not unregister the `terminalreporter` plugin: pytest's assertion diff calls `config.get_terminal_writer()`, which asserts it exists. The worker silences it by redirecting its `_tw` to `/dev/null` instead (`runner_fork.py`).
- `pytest>=8.1` is a real floor: `FixtureManager.getfixturedefs` took a nodeid string until 8.1 and a `Node` from 8.1 on. The `test-pytest-floor` task exists because a green run on the locked pytest says nothing about 8.1.
- The macOS fork tests that fork into the real `_scproxy` and libinfo crash paths are `skipif` off macOS: faking `sys.platform="darwin"` and forking into that platform's C code segfaults in the Linux CI container.
- pytest-xdist is a test dependency, not a leftover: the suite asserts that `worker_id` still resolves to ours when xdist is installed alongside.
