# Contributing to pytest-parallex

Thanks for helping. This is a pytest plugin: one package under `src/pytest_parallex/`,
a pytester-based test suite under `testing/`, and an mkdocs site under `docs/`. The only
runtime dependency is `pytest>=8.1`.

AI coding agents: read [AGENTS.md](AGENTS.md) instead; it is the machine-facing version
of this file.

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). To report a security issue,
see [SECURITY.md](SECURITY.md) rather than filing a public issue.

## Prerequisites

The toolchain (moon, uv) is pinned in `.prototools`. Install it with
[proto](https://moonrepo.dev/proto):

```bash
proto install
```

If you already have `uv`, that alone is enough for everything below. Python 3.10 or newer
is required (see `requires-python` in `pyproject.toml`).

## Setup

```bash
uv sync              # install dependencies into .venv
make install-hooks   # point git at the tracked .githooks/ (once)
```

## Development loop

```bash
make format    # auto-format + autofix (ruff, line length 100)
make check     # lint + typecheck + test
```

`make check` must pass before you commit. Individual stages:

- `make lint` runs ruff (same as CI).
- `make typecheck` runs pyright in strict mode.
- `make test` runs the pytest suite.

CI runs the full graph with `moon run :ci`, which adds `format-check`, `build`, `docs`
(mkdocs `--strict`), `version-guard`, and `test-pytest-floor` (the suite on Python 3.10
with `pytest==8.1.1`). moon owns task ordering, so run tasks through `make` or `moon run`,
never by hand-wiring stages.

## Tests

Tests live in `testing/`. A bug fix needs a regression test that fails without the fix;
`testing/test_regressions.py` is where those go. Use the `pytester` fixture and assert on
behaviour (outcomes, files the test wrote, lines in stdout), not on the plugin's internals.

On macOS, a few tests fork into the real `_scproxy` and libinfo crash paths on purpose and
assert the child dies; they are skipped everywhere else.

## Style

- Formatting and lint are enforced by ruff at line length 100 with `select = ["ALL"]`; every
  opt-out is justified inline in `pyproject.toml`.
- Types are enforced by pyright in strict mode. The plugin reaches into pytest privates by
  design, so `reportPrivateUsage` is the one exception.
- Prose (README, docs, changelog, comments, commit messages): no em-dashes; claim first,
  then evidence; concrete names, paths and numbers over qualitative words.

## Versioning

The version is the `__version__` literal in `src/pytest_parallex/__init__.py`, which
hatchling reads so `pyproject.toml` never disagrees. Do not edit it by hand: the pre-commit
hook auto-bumps the patch version when anything under `src/**/*.py` changes, and CI's
`version-guard` enforces that a `src/` change carries a bump. Use `make bump TYPE=minor|major`
for a deliberate larger bump.

## Changelog

Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for any user-visible
change. Versioning follows [SemVer](https://semver.org/).

## Commit messages

Write them for a stranger reading the history later: state what changed and why. The repo
uses lowercase, area-prefixed subjects (for example
`fix(fork): give each worker private first-import cache dirs`).

## Issues and merge requests

- File issues at <https://gitlab.com/cardona-oss/pytest-parallex/-/issues>.
- Open merge requests against `main`. Make sure `make check` passes first.

## Releasing

There is no manual release step. On a green `main` pipeline, `moon run :release` publishes
to PyPI via GitLab OIDC Trusted Publishing, only for a version that is not already there.
Merging a version bump to `main` is the release.

## License

By contributing, you agree that your contributions are licensed under the MIT License, the
same terms as the project (see [LICENSE](https://gitlab.com/cardona-oss/pytest-parallex/-/blob/main/LICENSE)).
