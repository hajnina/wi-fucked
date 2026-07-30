"""Hardware abstraction. ``MOCK_HW=1`` selects the fakes."""

from __future__ import annotations

import os

from dirty.hal.base import (
    ApStatus,
    Hal,
    RadioCapabilities,
    ScannedNetwork,
    StationLink,
    SystemFacts,
    UsbDevice,
)

__all__ = [
    "ApStatus",
    "Hal",
    "RadioCapabilities",
    "ScannedNetwork",
    "StationLink",
    "SystemFacts",
    "UsbDevice",
    "build_hal",
]

_singleton: Hal | None = None


def build_hal(force_mock: bool | None = None) -> Hal:
    """Build the HAL. Mocked when ``MOCK_HW`` is set, or when forced."""
    mock = os.getenv("MOCK_HW") == "1" if force_mock is None else force_mock
    if mock:
        from dirty.hal.mock import build_mock_hal

        return build_mock_hal()

    from dirty.hal.linux import build_linux_hal

    return build_linux_hal()


def get_hal() -> Hal:
    """Process-wide HAL singleton."""
    global _singleton
    if _singleton is None:
        _singleton = build_hal()
    return _singleton
