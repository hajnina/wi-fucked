"""Service profiles — what the user wants protected.

WAN policy describes what connectivity exists. Service policy describes what
matters. The two default profiles are deliberately few: the configuration should
be stupid simple by default, and infinitely deeper only if asked for.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Priority(enum.IntEnum):
    """Lower is protected first when capacity is scarce."""

    CRITICAL = 1
    BEST_EFFORT = 2


@dataclass(frozen=True, slots=True)
class ServiceProfile:
    name: str
    priority: Priority
    #: Whether this class may cause a metered BACKUP connection to activate.
    #: Best-effort must never be able to spend the user's money by wanting more
    #: bandwidth than exists.
    may_use_backup: bool
    #: VLAN the LAN layer assigns this class to. Everything above the LAN sees
    #: VLANs, not SSIDs, so the two-SSID / two-PSK choice (ADR-014) is invisible
    #: from here up.
    vlan: int
    #: DSCP marking applied by nftables, consumed by CAKE's diffserv mapping.
    dscp: int
    ssid_suffix: str


CRITICAL = ServiceProfile(
    name="Stable_critical",
    priority=Priority.CRITICAL,
    may_use_backup=True,
    vlan=10,
    dscp=0x2E,  # EF — expedited forwarding
    ssid_suffix="critical",
)

BEST_EFFORT = ServiceProfile(
    name="Stable_besteffort",
    priority=Priority.BEST_EFFORT,
    may_use_backup=False,
    vlan=20,
    dscp=0x00,  # CS0 — default
    ssid_suffix="besteffort",
)

DEFAULT_PROFILES: tuple[ServiceProfile, ...] = (CRITICAL, BEST_EFFORT)

#: The only profile a single, undifferentiated hotspot can offer (ADR-020).
#: Deliberately BEST_EFFORT, never CRITICAL: BEST_EFFORT cannot trigger BACKUP
#: (`may_use_backup=False`), so collapsing both classes onto one SSID can never
#: accidentally spend the user's money on traffic that only asked for best-effort.
SINGLE_LAN_PROFILES: tuple[ServiceProfile, ...] = (BEST_EFFORT,)


def profiles_for_lan_mode(lan_mode: str) -> tuple[ServiceProfile, ...]:
    """Which service profiles actually exist for a given LAN configuration.

    `"single"` (ADR-020) has no VLAN split, so `CRITICAL` cannot be offered —
    every caller that classifies or accounts LAN traffic must use this rather
    than assuming `DEFAULT_PROFILES`.
    """
    if lan_mode == "single":
        return SINGLE_LAN_PROFILES
    return DEFAULT_PROFILES


def by_priority(
    profiles: tuple[ServiceProfile, ...] = DEFAULT_PROFILES,
) -> list[ServiceProfile]:
    """Profiles most-protected first."""
    return sorted(profiles, key=lambda p: p.priority)


def by_name(name: str) -> ServiceProfile | None:
    return next((p for p in DEFAULT_PROFILES if p.name == name), None)


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Tunables for the allocator.

    Activation and recovery are deliberately asymmetric, and both carry dwell
    times. WAN quality oscillates on a scale of seconds; without hysteresis the
    appliance would activate and deactivate a metered connection repeatedly,
    which is both expensive and visibly broken.
    """

    #: Critical demand must exceed NORMAL capacity by at least this much before
    #: spending money is considered. Tolerating slower service is correct;
    #: reaching for the metered link at the first shortfall is not.
    activation_deficit_bps: int = 500_000
    #: …and must stay that way for this long.
    activation_dwell_s: float = 120.0
    #: NORMAL must be able to cover critical demand plus this margin before
    #: BACKUP is released, so recovery is not triggered by a lucky sample.
    recovery_margin_bps: int = 1_000_000
    recovery_dwell_s: float = 300.0

    #: Quality worse than these marks an atomic DEGRADED.
    degraded_rtt_ms: float = 400.0
    degraded_loss_pct: float = 5.0

    #: BACKUP liveness budget (ADR-006). Small, bounded, and accounted.
    liveness_interval_s: float = 900.0
    liveness_bytes: int = 300

    #: Below this, a capacity estimate is a guess and the allocator must not
    #: treat it as a measurement (ADR-003).
    min_confidence: float = 0.25
