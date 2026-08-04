"""sd_notify: no-op without systemd, correct bytes with it.

See docs/backlog/traffic-blockers.md item 2 and
appliance/stage-custom/etc/systemd/system/wifucked.service's WatchdogSec=120,
which is only a real liveness contract if something feeds it.
"""

from __future__ import annotations

import os
import socket
import tempfile

from wifucked.watchdog import sd_notify


class TestSdNotify:
    def test_noop_without_notify_socket(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        # Must not raise, block, or require a socket to exist.
        sd_notify("WATCHDOG=1")

    def test_sends_expected_bytes_over_real_socket(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "notify.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(path)
            server.settimeout(2.0)
            try:
                monkeypatch.setenv("NOTIFY_SOCKET", path)
                sd_notify("WATCHDOG=1")
                data, _ = server.recvfrom(1024)
                assert data == b"WATCHDOG=1"
            finally:
                server.close()

    def test_sends_ready(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "notify.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(path)
            server.settimeout(2.0)
            try:
                monkeypatch.setenv("NOTIFY_SOCKET", path)
                sd_notify("READY=1")
                data, _ = server.recvfrom(1024)
                assert data == b"READY=1"
            finally:
                server.close()

    def test_missing_socket_file_does_not_raise(self, monkeypatch):
        # NOTIFY_SOCKET pointing at a nonexistent path (systemd went away,
        # or a stale env var) must degrade quietly, not crash the daemon.
        monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/path.sock")
        sd_notify("WATCHDOG=1")
