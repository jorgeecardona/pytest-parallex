"""The darwin DNS fork trap and its guard (see ``install_forksafe_getaddrinfo``).

The segfault itself only reproduces on macOS in a forked child of a DNS-poisoned parent,
so these tests exercise the guard's *mechanics* — fast path, subprocess path, cache,
failure shape, uninstall — which behave identically on every platform. The only names
resolved are numeric addresses and ``localhost``, so CI needs no external DNS.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any

import pytest

from pytest_parallex._safety import install_forksafe_getaddrinfo, uninstall_forksafe_getaddrinfo

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def forksafe_resolver() -> Iterator[None]:
    original = socket.getaddrinfo
    install_forksafe_getaddrinfo()
    try:
        yield
    finally:
        uninstall_forksafe_getaddrinfo()
        assert socket.getaddrinfo is original  # the guard must leave no trace


def _addresses(results: list[tuple[Any, ...]]) -> list[str]:
    return sorted(sockaddr[0] for _family, _kind, _proto, _canonname, sockaddr in results)


class TestForksafeGetaddrinfo:
    def test_numeric_host_stays_in_process(self, forksafe_resolver: None) -> None:
        results = socket.getaddrinfo("127.0.0.1", 80, type=socket.SOCK_STREAM)
        assert _addresses(results) == ["127.0.0.1"]

    def test_real_name_resolves_via_subprocess(self, forksafe_resolver: None) -> None:
        results = socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)
        assert "127.0.0.1" in _addresses(results) or "::1" in _addresses(results)

    def test_result_shape_matches_the_stdlib(self, forksafe_resolver: None) -> None:
        for family, kind, proto, canonname, sockaddr in socket.getaddrinfo(
            "localhost", 80, type=socket.SOCK_STREAM
        ):
            assert isinstance(family, int)
            assert isinstance(kind, int)
            assert isinstance(proto, int)
            assert isinstance(canonname, str)
            assert isinstance(sockaddr, tuple)
            socket.socket(family, kind, proto).close()  # usable, not just shaped right

    def test_repeat_lookup_is_served_from_cache(self, forksafe_resolver: None) -> None:
        first = socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)
        second = socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)
        assert second is first  # identity: no second subprocess ran

    def test_unresolvable_name_raises_gaierror(self, forksafe_resolver: None) -> None:
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("definitely-not-a-real-host.invalid", 80)

    def test_install_is_idempotent(self, forksafe_resolver: None) -> None:
        patched = socket.getaddrinfo
        install_forksafe_getaddrinfo()
        assert socket.getaddrinfo is patched
