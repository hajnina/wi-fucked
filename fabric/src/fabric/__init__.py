"""The server fabric — the other end of the stable tunnel.

Client sessions terminate here rather than at the WAN, so the client-visible IP
never changes when the appliance switches connections. That is what makes a WAN
swap survivable instead of a visible outage (ADR-005).

WS-E owns this. It ships the health and registration API the appliance needs to
check compatibility and pick a server, plus real WireGuard peer management:
tunnel-address allocation and adding the appliance as a peer on ``wg0``.
"""

__version__ = "0.0.0-dev"

#: The oldest appliance this fabric can serve. An appliance below its own
#: DIRTY_FABRIC_MIN refuses to attach rather than failing mid-tunnel, and this
#: is the mirror of that check.
MIN_APPLIANCE_VERSION = "0.1.0"

__all__ = ["MIN_APPLIANCE_VERSION", "__version__"]
