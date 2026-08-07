"""LAN-out DHCP-server fallback — ADR-023, implementing ADR-022's Decision.

A wired/USB-Ethernet port that gets a DHCP lease is a WAN source (already
handled: discovery/__init__.py defaults it to Mode.NORMAL the moment it's
seen, per ADR-022). A wired port that *doesn't* get a lease might be plugged
into a live network this device doesn't own but whose DHCP server was merely
slow to answer, or it might genuinely have nothing upstream and be exactly
the kind of "bare downstream port" this module exists to serve. Confusing
the two is not a cosmetic bug: switching an owned-by-someone-else segment
into DHCP-server mode puts a second, competing DHCP server on a network this
device has no business touching. ADR-022's Decision section is explicit that
this asymmetry — a false "become a WAN source" costs nothing, a false
"become a DHCP server" is actively harmful — is why the pipeline below is a
strict sequence, never a shortcut:

    1. Attempt a real DHCP client lease (bounded timeout). Got one -> this
       is a WAN source; nothing more to do, ADR-022's default already
       covers it.
    2. No lease -> passively listen (never transmit) for existing DHCP
       server traffic on the segment (bounded timeout). Heard something,
       or couldn't tell -> leave the port inert (`Mode.UNUSED`) and log
       clearly. Never becomes a server on an ambiguous signal.
    3. Still nothing heard -> switch the port into DHCP-server mode and
       mark it `PortRole.LAN_OUT`, so `enforce.render()` routes its traffic
       through the tunnel the same way an AP LAN client's traffic is
       (ADR-019).

Each step runs on a background thread (`_EXECUTOR`) — the bounded timeouts
here are seconds, not milliseconds, and would otherwise stall the daemon's
fast/medium loops (`daemon.py`'s `tick()` runs everything on one thread).
`LanOutClassifier.consider()` is the only thing this module exposes to the
daemon: it is non-blocking, kicks off pipelines for newly-seen candidate
ports, and returns whatever pipelines have *finished* since the last call.
Applying an outcome to the registry is the caller's job (`daemon.py`), same
separation `Discoverer` already uses for Wi-Fi scanning.
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from wifucked.atomics.model import Atomic, Health, Kind, Mode, PortRole
from wifucked.hal import Hal
from wifucked.logging import get_logger

log = get_logger("lanout")

#: Wired kinds only. USB tethering (a phone) already carries its own
#: connectivity via the cellular modem's own NAT — there is no "bare
#: downstream port" reading for it, and running a DHCP server on a phone's
#: RNDIS/NCM interface makes no sense. Scoped deliberately narrower than
#: "every USB device", flagged in the PR body as a judgement call.
CANDIDATE_KINDS = (Kind.USB_ETHERNET, Kind.ETHERNET)

#: Bounded timeouts for each pipeline stage. Judgement calls, not measured
#: against real hardware or a real hostile network — flagged for review, see
#: ADR-023's "must stay true" clause and docs/active-tests.md. Generous
#: enough that a slow DHCP offer on a real, live network is very unlikely to
#: be missed (the actual harm this whole pipeline exists to prevent), short
#: enough that a genuinely bare port becomes useful in well under a minute.
DEFAULT_DHCP_CLIENT_TIMEOUT_S = 8.0
DEFAULT_PASSIVE_LISTEN_TIMEOUT_S = 15.0

#: Where LAN-out subnets are allocated from, relative to the AP's own gateway
#: third octet (`LanConfig.address`). Offset well clear of the 0/1 the AP's
#: own profile(s) use, and hashed per atomic id (ADR-002 identity, never
#: `ifname`) so the same physical port gets the same subnet across restarts
#: with no sequential-allocation state to persist.
_SUBNET_OFFSET = 50
_SUBNET_SPAN = 150


@dataclass(frozen=True, slots=True)
class ClassificationOutcome:
    atomic_id: str
    role: PortRole
    mode: Mode
    reason: str
    duration_ms: int


def subnet_third_octet(atomic_id: str, base_third_octet: int) -> int:
    """Deterministic LAN-out subnet third octet for one atomic.

    Same hash-not-sequence reasoning as `enforce._table_for_atomic`: hashing
    the atomic's stable id gives the same answer every time, for every
    process, with nothing to persist or get out of sync across a restart.
    """
    digest = hashlib.sha256(f"lanout:{atomic_id}".encode()).hexdigest()
    offset = _SUBNET_OFFSET + (int(digest, 16) % _SUBNET_SPAN)
    return min(255, base_third_octet + offset)


def _is_candidate(atomic: Atomic) -> bool:
    return (
        atomic.present
        and atomic.role is PortRole.WAN
        and atomic.kind in CANDIDATE_KINDS
        and atomic.health is Health.GOOD
        and bool(atomic.ifname)
    )


class LanOutClassifier:
    """Runs the DHCP-attempt/passive-listen/server pipeline per port.

    Stateless from the daemon's point of view beyond "what's in flight" and
    "what's already been decided this process lifetime" — the *result* lives
    on the `Atomic` itself (`role`, `mode`), which is what makes it durable
    across restarts via `Registry.persist()`. A replug (present -> absent ->
    present again) clears the per-atomic "already decided" marker, so a port
    moved to a different network gets reclassified rather than permanently
    keeping its first answer.
    """

    def __init__(
        self,
        *,
        dhcp_client_timeout_s: float = DEFAULT_DHCP_CLIENT_TIMEOUT_S,
        passive_listen_timeout_s: float = DEFAULT_PASSIVE_LISTEN_TIMEOUT_S,
        gateway_prefix: str = "10.44",
        base_third_octet: int = 0,
        max_workers: int = 4,
    ) -> None:
        self._dhcp_client_timeout_s = dhcp_client_timeout_s
        self._passive_listen_timeout_s = passive_listen_timeout_s
        #: First two octets of the LAN-out subnet base — matches
        #: `LanConfig.address`'s own first two octets by construction
        #: (`Daemon` passes them through), so a LAN-out subnet and the AP's
        #: own subnet(s) are visibly related on the dashboard without ever
        #: numerically colliding (`_SUBNET_OFFSET` keeps the third octet
        #: well clear of the AP profile(s)' own).
        self._gateway_prefix = gateway_prefix
        self._base_third_octet = base_third_octet
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="lanout")
        self._in_flight: dict[str, Future] = {}
        self._decided: set[str] = set()
        self._was_present: dict[str, bool] = {}

    def consider(self, hal: Hal, atomics: list[Atomic]) -> list[ClassificationOutcome]:
        """Non-blocking: kick off new pipelines, harvest finished ones.

        Called once per medium-loop tick from `daemon.py`, right after
        discovery. Never raises — a broken pipeline for one port must not
        stop the loop, matching every other discovery-adjacent source in
        this codebase (`discovery._sweep`'s own per-source try/except).
        """
        by_id = {a.id: a for a in atomics}
        for atomic in atomics:
            was_present = self._was_present.get(atomic.id, False)
            self._was_present[atomic.id] = atomic.present
            if not was_present and atomic.present:
                # Replug (or first sight): the segment on the other end of
                # this port may be different now. Forget any prior verdict.
                self._decided.discard(atomic.id)

            if not _is_candidate(atomic):
                continue
            if atomic.id in self._decided or atomic.id in self._in_flight:
                continue
            self._in_flight[atomic.id] = self._executor.submit(
                _run_pipeline,
                hal,
                atomic,
                self._dhcp_client_timeout_s,
                self._passive_listen_timeout_s,
                self._gateway_prefix,
                self._base_third_octet,
            )
            log.info(
                "Started port role classification",
                extra={
                    "workflow": "port_role_classification",
                    "state": "started",
                    "intent": "find out whether this wired port has an upstream network, "
                    "and if not, whether it's safe to serve DHCP on it",
                    "atomic_id": atomic.id,
                    "label": atomic.label,
                    "ifname": atomic.ifname,
                    "dhcp_client_timeout_s": self._dhcp_client_timeout_s,
                    "passive_listen_timeout_s": self._passive_listen_timeout_s,
                },
            )

        outcomes: list[ClassificationOutcome] = []
        for atomic_id in list(self._in_flight):
            future = self._in_flight[atomic_id]
            if not future.done():
                continue
            del self._in_flight[atomic_id]
            self._decided.add(atomic_id)
            try:
                outcome = future.result()
            except Exception as exc:  # a pipeline bug must not crash the daemon (SOP-002)
                label = by_id.get(atomic_id).label if atomic_id in by_id else atomic_id
                log.error(
                    "Port role classification pipeline failed; leaving port inert",
                    extra={
                        "workflow": "port_role_classification",
                        "state": "failed",
                        "intent": "find out whether this wired port has an upstream network",
                        "atomic_id": atomic_id,
                        "label": label,
                        "reason": "unhandled error in classification pipeline",
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                outcome = ClassificationOutcome(
                    atomic_id=atomic_id,
                    role=PortRole.WAN,
                    mode=Mode.UNUSED,
                    reason="classification_pipeline_error",
                    duration_ms=0,
                )
            outcomes.append(outcome)
        return outcomes


def _run_pipeline(
    hal: Hal,
    atomic: Atomic,
    dhcp_client_timeout_s: float,
    passive_listen_timeout_s: float,
    gateway_prefix: str,
    base_third_octet: int,
) -> ClassificationOutcome:
    """The actual sequence. Runs on a background thread — see module docstring.

    `atomic.ifname` is read once at pipeline start, exactly the "current
    fact, valid only for as long as you hold it" ADR-002 describes; nothing
    here persists it, and the outcome is applied back against `atomic.id`.
    """
    started = time.monotonic()
    ifname = atomic.ifname
    if not ifname:
        # _is_candidate() already checked this before submitting the pipeline;
        # a defensive re-check rather than `assert` (SOP-002: assertions can
        # be stripped with `-O`, and this must hold even then).
        return ClassificationOutcome(
            atomic_id=atomic.id,
            role=PortRole.WAN,
            mode=Mode.UNUSED,
            reason="no_ifname_at_pipeline_start",
            duration_ms=0,
        )

    lease = hal.dhcp.attempt_client_lease(ifname, dhcp_client_timeout_s)
    if lease is not None:
        return ClassificationOutcome(
            atomic_id=atomic.id,
            role=PortRole.WAN,
            mode=Mode.NORMAL,
            reason="dhcp_client_lease_obtained",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    foreign_heard = hal.dhcp.passive_listen_for_foreign_server(ifname, passive_listen_timeout_s)
    if foreign_heard:
        return ClassificationOutcome(
            atomic_id=atomic.id,
            role=PortRole.WAN,
            mode=Mode.UNUSED,
            reason="foreign_dhcp_server_heard_or_undetermined",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    third_octet = subnet_third_octet(atomic.id, base_third_octet)
    gateway = f"{gateway_prefix}.{third_octet}.1"
    started_ok = hal.dhcp.start_server(ifname, third_octet, gateway)
    return ClassificationOutcome(
        atomic_id=atomic.id,
        role=PortRole.LAN_OUT if started_ok else PortRole.WAN,
        mode=Mode.UNUSED,
        reason="became_dhcp_server" if started_ok else "dhcp_server_start_failed",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


__all__ = [
    "CANDIDATE_KINDS",
    "ClassificationOutcome",
    "LanOutClassifier",
    "subnet_third_octet",
]
