"""The Atomic — one independently usable Internet connection.

This is the centre of the system. Nothing below ``atomics`` may import from
above it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace


class Mode(enum.StrEnum):
    """What the user has permitted us to do with a connection."""

    #: Part of the active pool. Use freely.
    NORMAL = "normal"
    #: Expensive. Zero bytes at rest beyond the liveness budget (ADR-006).
    BACKUP = "backup"
    #: Known to exist, never used automatically. Discovery is not permission.
    UNUSED = "unused"


class Kind(enum.StrEnum):
    WIFI = "wifi"
    USB_TETHER = "usb_tether"
    USB_ETHERNET = "usb_ethernet"
    CELLULAR = "cellular"


class Health(enum.StrEnum):
    #: Carrying traffic, meeting expectations.
    GOOD = "good"
    #: Usable but measurably worse than it was — high RTT, loss, or low capacity.
    DEGRADED = "degraded"
    #: Present but not passing traffic.
    DOWN = "down"
    #: Not currently present at all.
    ABSENT = "absent"
    #: Seen, never measured. Capacity is unknown, not zero.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Capacity:
    """A capacity estimate with an honest confidence.

    Capacity is observed, not configured, and a link that has never been
    saturated has no estimate (ADR-003). ``confidence`` is what stops the
    allocator treating a guess as a measurement — it decays as an estimate ages.
    """

    down_bps: int = 0
    up_bps: int = 0
    #: 0.0 = pure guess, 1.0 = measured under recent saturation.
    confidence: float = 0.0
    measured_at: float | None = None

    @property
    def known(self) -> bool:
        return self.confidence > 0.0


@dataclass(frozen=True, slots=True)
class Quality:
    rtt_ms: float | None = None
    jitter_ms: float | None = None
    loss_pct: float | None = None
    #: Latency added under load. The bufferbloat signal.
    bloat_ms: float | None = None


@dataclass(frozen=True, slots=True)
class Cost:
    """What using this connection costs. UNUSED is priced at infinity."""

    metered: bool = False
    #: Bytes attributed to this atomic since the accounting epoch.
    consumed_bytes: int = 0
    #: Bytes spent on liveness probes specifically (ADR-006), tracked
    #: separately so the dashboard can be honest about the "zero bytes" claim.
    liveness_bytes: int = 0
    activations: int = 0
    active_seconds: float = 0.0


@dataclass(slots=True)
class Atomic:
    """One independently usable Internet connection.

    ``id`` derives from stable properties of the connection and never from a
    kernel interface name (ADR-002). ``ifname`` is a *current fact about* this
    atomic: read it at the point of use, never persist or compare it.
    """

    id: str
    kind: Kind
    label: str
    mode: Mode = Mode.UNUSED
    health: Health = Health.UNKNOWN
    capacity: Capacity = field(default_factory=Capacity)
    quality: Quality = field(default_factory=Quality)
    cost: Cost = field(default_factory=Cost)

    #: Volatile. Valid only while present; may change on any re-enumeration.
    ifname: str | None = None
    present: bool = False
    first_seen: float | None = None
    last_seen: float | None = None

    #: Sticky once True. Set the first time this atomic was actually connected
    #: to (a Wi-Fi station association, a USB link with carrier) rather than
    #: merely seen in a scan. Bounds what `Registry.persist()` writes to disk
    #: (ADR-010): a network glimpsed once and never joined has no reason to
    #: live in `atomics.json` forever.
    ever_connected: bool = False

    #: Kind-specific detail for the dashboard (SSID, channel, USB serial, …).
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Present, permitted, and actually passing traffic."""
        return (
            self.present
            and self.mode is not Mode.UNUSED
            and self.health in (Health.GOOD, Health.DEGRADED)
        )

    @property
    def in_normal_pool(self) -> bool:
        return self.usable and self.mode is Mode.NORMAL

    def with_capacity(self, capacity: Capacity) -> Atomic:
        return replace(self, capacity=capacity)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": str(self.kind),
            "label": self.label,
            "mode": str(self.mode),
            "health": str(self.health),
            "present": self.present,
            "ifname": self.ifname,
            "capacity": {
                "down_bps": self.capacity.down_bps,
                "up_bps": self.capacity.up_bps,
                "confidence": round(self.capacity.confidence, 3),
                "known": self.capacity.known,
            },
            "quality": {
                "rtt_ms": self.quality.rtt_ms,
                "jitter_ms": self.quality.jitter_ms,
                "loss_pct": self.quality.loss_pct,
                "bloat_ms": self.quality.bloat_ms,
            },
            "cost": {
                "metered": self.cost.metered,
                "consumed_bytes": self.cost.consumed_bytes,
                "liveness_bytes": self.cost.liveness_bytes,
                "activations": self.cost.activations,
                "active_seconds": round(self.cost.active_seconds, 1),
            },
            "attributes": dict(self.attributes),
        }
