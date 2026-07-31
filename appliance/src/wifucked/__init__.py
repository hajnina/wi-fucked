"""WI-FUCKED → BALANCED — an autonomous connectivity appliance.

Turns chaotic WAN connectivity into two LAN networks that never go away.

The control plane is Python; the data plane is the kernel. No packet is ever
touched by Python — this daemon observes, decides, and programs tc/CAKE,
nftables, and policy routing. See docs/adr/ADR-001.
"""

#: Fallback only. The real version is baked into /etc/wifucked-release at image
#: build time and read via wifucked.config.release_info() — the git tag is the
#: source of truth, not this string (ADR-016).
__version__ = "0.0.0-dev"

__all__ = ["__version__"]
