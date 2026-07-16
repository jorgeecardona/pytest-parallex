"""The real macOS fork traps, exercised for real — no faked platform.

Linux CI covers the guards' *mechanics* (test_plugin, test_forksafe_resolver, the
end-to-end fakes in test_regressions). What it cannot cover is the traps themselves:
these run only on a Mac and fork a fresh interpreter into the actual crash paths.

Each trap gets a pair: a repro test proving the crash still exists (a canary — if an
OS update fixes the trap, the repro fails and the guard can be reconsidered), and a
survival test proving ``harden_darwin_fork_child`` defuses it.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="exercises the real macOS fork traps"
)

_TIMEOUT = 60


def _run_forking_script(body: str) -> str:
    """Fork inside a fresh interpreter and report the child's fate on stdout.

    A fresh process, not an in-test fork: the traps depend on parent-process state
    (libinfo, CoreFoundation), and pytest's own state must not contaminate the repro.
    OBJC_DISABLE_INITIALIZE_FORK_SAFETY is stripped so the traps are armed even if the
    developer exports it.
    """
    script = f"""\
import os, sys
import warnings
warnings.simplefilter("ignore")  # fork-in-threaded-process noise

{body}

pid = os.fork()
if pid == 0:
    try:
        child()
        os._exit(0)
    except Exception:
        os._exit(2)
_, status = os.waitpid(pid, 0)
if os.WIFSIGNALED(status):
    print(f"SIGNALED:{{os.WTERMSIG(status)}}")
else:
    print(f"EXITED:{{os.WEXITSTATUS(status)}}")
"""
    proc = subprocess.run(  # noqa: S603 — our own interpreter, our own literal script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        check=False,
        env={k: v for k, v in os.environ.items() if k != "OBJC_DISABLE_INITIALIZE_FORK_SAFETY"},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _skip_without_dns() -> None:
    import socket

    try:
        socket.getaddrinfo("example.com", 443)
    except socket.gaierror:
        pytest.skip("no external DNS available")


class TestScproxyTrap:
    def test_proxy_lookup_after_fork_crashes_unguarded(self) -> None:
        """The trap is real: parent touches the proxy path, forked child dies on it."""
        fate = _run_forking_script(
            """\
import urllib.request
urllib.request.getproxies()

def child():
    urllib.request.proxy_bypass("example.com")
"""
        )
        assert fate.startswith("SIGNALED:"), (
            f"child survived ({fate}) — has this macOS/Python fixed the _scproxy trap? "
            f"If so the guard in harden_darwin_fork_child can be reconsidered."
        )

    def test_hardened_child_survives_proxy_lookup(self) -> None:
        fate = _run_forking_script(
            """\
import urllib.request
urllib.request.getproxies()

def child():
    from pytest_parallex._safety import harden_darwin_fork_child
    harden_darwin_fork_child()
    assert urllib.request.proxy_bypass("example.com") is False
"""
        )
        assert fate == "EXITED:0", fate


class TestDnsTrap:
    def test_dns_lookup_after_fork_crashes_unguarded(self) -> None:
        """The trap is real: parent resolves any name, forked child dies resolving.

        Bimodal, measured here: a parent is either poisoned at fork time (every child
        segfaults) or not (every child survives), at roughly 60/40 per fresh parent. So
        the repro retries across parent processes; all six surviving would put a still-
        existing trap below a 0.5% false-fail — treat that as the trap actually gone.
        """
        _skip_without_dns()
        fates: list[str] = []
        for _ in range(6):
            fate = _run_forking_script(
                """\
import socket
socket.getaddrinfo("example.com", 443)

def child():
    socket.getaddrinfo("anthropic.com", 443)
"""
            )
            fates.append(fate)
            if fate.startswith("SIGNALED:"):
                return
        pytest.fail(
            f"every child survived ({fates}) — has this macOS/Python fixed the libinfo "
            f"trap? If so install_forksafe_getaddrinfo can be reconsidered."
        )

    def test_hardened_child_survives_dns_lookup(self) -> None:
        _skip_without_dns()
        fate = _run_forking_script(
            """\
import socket
socket.getaddrinfo("example.com", 443)

def child():
    from pytest_parallex._safety import harden_darwin_fork_child
    harden_darwin_fork_child()
    results = socket.getaddrinfo("anthropic.com", 443)
    assert results
"""
        )
        assert fate == "EXITED:0", fate
