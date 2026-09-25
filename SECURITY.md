# Security Policy

## Supported versions

pytest-parallex is pre-1.0 (beta). Security fixes land on `main` and ship in the next
release to [PyPI](https://pypi.org/project/pytest-parallex/). Please run the latest
released version before reporting; older versions are not patched separately.

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately by email to
**jorgeecardona@gmail.com** with:

- a description of the issue and its impact,
- the plugin version (`pip show pytest-parallex`), pytest version, Python version and OS,
- steps or a minimal proof of concept to reproduce it.

You can expect an acknowledgement within a few days. Once a fix is released, credit is given
in the changelog unless you prefer to stay anonymous.

## Scope

pytest-parallex runs a test suite across worker processes or threads on one machine. Reports
that matter most:

- a way for another local user to read or tamper with the worker-to-controller report
  channel (pickled report files under a private temporary directory) or the per-worker
  cache directories the fork runner creates,
- a hostname, environment value or command-line argument that reaches a shell or a
  subprocess unescaped (the fork worker resolves DNS names in a `python -I -c` subprocess on
  macOS; `--parallel=process` launches worker interpreters),
- a way for a worker to influence the controller's reported outcome (a failed test reported
  as passed, or a worker's tests silently missing from the total without the run failing).

## Trust model

The plugin executes the test suite it is given, with that suite's own permissions. Test code,
`conftest.py` files and installed pytest plugins are trusted: pytest itself already runs them
with full access, and pytest-parallex adds no sandbox around them. Reports that assume an
attacker can already edit the test suite, the project's `pyproject.toml`, or the environment
the tests run in are out of scope.
