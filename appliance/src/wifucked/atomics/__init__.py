"""Atomics — the centre of the system.

An *atomic* is one independently usable Internet connection. Everything else in
the daemon refers to connections by atomic id.
"""

from wifucked.atomics.model import Atomic, Capacity, Cost, Health, Kind, Mode, PortRole, Quality
from wifucked.atomics.registry import Registry

__all__ = [
    "Atomic",
    "Capacity",
    "Cost",
    "Health",
    "Kind",
    "Mode",
    "PortRole",
    "Quality",
    "Registry",
]
