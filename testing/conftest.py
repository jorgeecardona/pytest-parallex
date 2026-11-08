import os
import textwrap
from typing import Any

import pytest

pytest_plugins = ["pytester"]


def suite(header: str, *body: str) -> str:
    """Build a test-module source for ``pytester.makepyfile``.

    ``makepyfile`` dedents by common leading prefix, which a zero-indented line appended to
    an indented triple-quoted block silently defeats: the common prefix becomes '', the
    block keeps its indentation, and you get a module that is a syntax error rather than a
    test. That shows up as a confusing collection error rather than "your string is wrong",
    and it has now bitten twice in this suite — hence a shared helper instead of a third
    copy.
    """
    return textwrap.dedent(header).strip() + "\n\n" + "\n".join(body) + "\n"


@pytest.fixture
def pytester(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """Run fork-mode inner sessions in a fresh subprocess (#3).

    ``runpytest`` runs in-process, so ``--parallel=fork`` would fork this outer test runner,
    with whatever threads and native state earlier thread and async runs left behind. Under
    load that segfaulted the outer process about once in 7 runs.
    """
    in_process = pytester.runpytest

    def runpytest(*args: str | os.PathLike[str], **kwargs: Any) -> pytest.RunResult:
        if any("fork" in str(arg) for arg in args):
            return pytester.runpytest_subprocess(*args, **kwargs)
        return in_process(*args, **kwargs)

    monkeypatch.setattr(pytester, "runpytest", runpytest)
    return pytester
