import textwrap

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
