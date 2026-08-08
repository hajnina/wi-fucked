"""The entrypoint's bind-retry behaviour.

Regression coverage for a real field bug: ``config.api_host`` is hardcoded to
the LAN gateway address, which ``systemd-networkd`` assigns to the AP
interface independently of this daemon (ADR-011) and with no ordering
guarantee against it. Starting before that address lands used to raise an
unhandled ``OSError`` straight out of ``main()``, killing the whole process
-- including the already-running control-loop thread -- and, under
``Restart=always``, crash-looping without ever getting far enough to log
anything useful about the AP itself.
"""

from __future__ import annotations

import errno
import time

import pytest

from wifucked.__main__ import _BIND_RETRY_INTERVAL_S, _serve_with_retry


class _FakeApp:
    """Stands in for Flask's ``app.run()``: raises N times, then "serves"."""

    def __init__(self, failures: int, errno_: int = errno.EADDRNOTAVAIL):
        self.failures = failures
        self.errno = errno_
        self.calls = 0

    def run(self, host, port, threaded=True):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError(self.errno, "Cannot assign requested address")
        # A real bind success blocks forever serving; the caller only cares
        # that this returns normally instead of raising.


class TestServeWithRetry:
    def test_retries_address_not_available_until_it_binds(self):
        app = _FakeApp(failures=3)
        slept = []
        _serve_with_retry(app, "10.44.0.1", 8080, sleep=slept.append)

        assert app.calls == 4
        assert slept == [_BIND_RETRY_INTERVAL_S] * 3

    def test_succeeds_immediately_with_no_retry(self):
        app = _FakeApp(failures=0)
        slept = []
        _serve_with_retry(app, "10.44.0.1", 8080, sleep=slept.append)

        assert app.calls == 1
        assert slept == []

    def test_does_not_retry_a_non_address_error(self):
        """Address-already-in-use (a real config conflict) must surface, not loop."""
        app = _FakeApp(failures=1, errno_=errno.EADDRINUSE)
        with pytest.raises(OSError):
            _serve_with_retry(app, "10.44.0.1", 8080, sleep=lambda _s: None)

        assert app.calls == 1

    def test_gives_up_once_the_deadline_passes(self):
        """A permanently-missing address must not retry forever."""
        app = _FakeApp(failures=10_000)

        with pytest.raises(OSError):
            _serve_with_retry(
                app,
                "10.44.0.1",
                8080,
                sleep=lambda _s: None,
                # Already in the past: gives up on the very first failure
                # without needing real wall-clock time to elapse in the test.
                deadline=time.monotonic() - 1,
            )
        assert app.calls == 1
