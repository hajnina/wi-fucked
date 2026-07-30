"""Radio profile management — one radio, two jobs.

The Pi Zero 2W has a single 2.4 GHz radio that must run the LAN access point
and, when Wi-Fi is the only WAN, also act as a station. Both cannot be on
different channels.

Two profiles resolve that (ADR-013):

* **ANCHOR** — a USB WAN exists, so the radio is AP-only on a fixed channel that
  never moves. Preferred, and genuinely always-on.
* **SHARED** — Wi-Fi is the only WAN, so AP and station share one channel and
  the AP follows the station via CSA.

The AP is the anchor (ADR-011): this module *requests* changes from hostapd and
observes the result. It never owns hostapd's lifecycle, and it never restarts it
to effect a channel change — that would drop every client, which is the one thing
that must not happen.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from dirty.atomics.model import Atomic, Kind, Mode
from dirty.hal import Hal
from dirty.logging import get_logger
from dirty.telemetry import Telemetry

log = get_logger("radio")


class Profile(enum.StrEnum):
    ANCHOR = "anchor"
    SHARED = "shared"


@dataclass(frozen=True, slots=True)
class RadioState:
    profile: Profile
    ap_channel: int | None
    station_channel: int | None
    ap_running: bool
    clients: int
    #: Set when the AP and station disagree on channel in SHARED — the station
    #: cannot associate until the AP moves.
    channel_conflict: bool = False


#: Where the AP parks when nothing constrains it. Channel 1, 6 and 11 are the
#: non-overlapping 2.4 GHz channels; 6 is the conventional middle choice.
DEFAULT_AP_CHANNEL = 6


def select_profile(atomics: list[Atomic]) -> Profile:
    """ANCHOR whenever any usable non-Wi-Fi WAN exists.

    Deliberately biased towards ANCHOR: a fixed AP channel is strictly better for
    the user, so any excuse to stop sharing the radio is taken.
    """
    for atomic in atomics:
        if (
            atomic.present
            and atomic.mode is not Mode.UNUSED
            and atomic.kind in (Kind.USB_TETHER, Kind.USB_ETHERNET, Kind.CELLULAR)
        ):
            return Profile.ANCHOR
    return Profile.SHARED


class RadioManager:
    def __init__(self, hal: Hal, telemetry: Telemetry):
        self._hal = hal
        self._telemetry = telemetry
        self._profile: Profile | None = None
        #: Set when the driver refuses CSA. We then stop trying to move the AP —
        #: repeatedly failing to switch is worse than staying put, and the user
        #: is told the appliance needs a USB WAN for off-channel networks.
        self.csa_unavailable = False

    @property
    def profile(self) -> Profile | None:
        return self._profile

    def observe(self, atomics: list[Atomic]) -> RadioState:
        ap = self._hal.ap.status()
        link = self._hal.wifi.station_link()
        profile = select_profile(atomics)

        conflict = bool(
            profile is Profile.SHARED
            and link is not None
            and ap.channel is not None
            and link.channel != ap.channel
        )

        if profile is not self._profile:
            log.info(
                "Radio profile changed",
                extra={
                    "workflow": "radio_profile",
                    "state": "completed",
                    "intent": "give the AP a fixed channel whenever a USB WAN exists",
                    "profile_from": str(self._profile) if self._profile else None,
                    "profile_to": str(profile),
                    "ap_channel": ap.channel,
                    "station_channel": link.channel if link else None,
                    "clients": ap.associated_clients,
                },
            )
            self._telemetry.record_event(
                "radio_profile",
                {"from": str(self._profile) if self._profile else None, "to": str(profile)},
            )
            self._profile = profile

        return RadioState(
            profile=profile,
            ap_channel=ap.channel,
            station_channel=link.channel if link else None,
            ap_running=ap.running,
            clients=ap.associated_clients,
            channel_conflict=conflict,
        )

    def align(self, state: RadioState) -> bool:
        """Move the AP to the station's channel if SHARED requires it.

        Returns True when the AP is where it needs to be. Uses CSA so associated
        clients follow without re-associating; never restarts hostapd.
        """
        if state.profile is Profile.ANCHOR or not state.channel_conflict:
            return True
        if state.station_channel is None:
            return True
        if self.csa_unavailable:
            log.warning(
                "AP and station are on different channels and CSA is unavailable",
                extra={
                    "workflow": "csa_move",
                    "state": "skipped",
                    "intent": "keep AP and station on one channel in SHARED profile",
                    "ap_channel": state.ap_channel,
                    "station_channel": state.station_channel,
                    "reason": "driver refused a previous channel switch",
                },
            )
            return False

        before = self._hal.ap.status()
        ok = self._hal.ap.channel_switch(state.station_channel)
        after = self._hal.ap.status()

        # Losing clients across a CSA is a defect, not a cost of doing business.
        lost = max(0, before.associated_clients - after.associated_clients)
        log.info(
            "AP channel move",
            extra={
                "workflow": "csa_move",
                "state": "completed" if ok else "failed",
                "intent": "keep AP and station on one channel in SHARED profile",
                "profile": str(state.profile),
                "channel_from": before.channel,
                "channel_to": state.station_channel,
                "clients_before": before.associated_clients,
                "clients_after": after.associated_clients,
                "clients_lost": lost,
                "reason": None if ok else "driver refused the channel switch",
            },
        )
        self._telemetry.record_event(
            "csa_move",
            {
                "from": before.channel,
                "to": state.station_channel,
                "ok": ok,
                "clients_lost": lost,
            },
        )

        if not ok:
            self.csa_unavailable = True
        return ok
