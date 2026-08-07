# ADR-022 — Interfaces enable themselves; the user only chooses main vs. backup

**Status:** Accepted
**Date:** 2026-08-07

## Context

The appliance shipped with discovery deliberately conservative: "a newly
discovered connection is always UNUSED until they say otherwise"
(`wifucked.discovery`'s own docstring). That was the right default for a
product whose primary WAN sources were assumed to be occasional and
user-supplied — a phone plugged in for the afternoon, a hotel Wi-Fi network
picked from a list. It requires a person to open the dashboard and classify
every connection before any of them do anything.

The actual deployment shape this device is aimed at is different: a person
plugs a handful of USB Ethernet adapters into whatever ports are around —
some are LAN ports on existing routers (they'll get a DHCP lease and can
carry real traffic outward), some are meant to *be* the WAN for something
downstream (they won't get a lease from anything, because nothing upstream
is offering one). The person doing this should not have to open a dashboard
and classify each one by hand for the device to work. "Plug it in and it
works" is the product now, not a stretch goal.

That collides with two things this codebase already protects on purpose:

- **ADR-006's accounted liveness budget** exists because a `BACKUP`
  connection can be a metered phone tether, and spending a user's data
  without them saying yes is a real cost, not a UX nicety. Making every new
  connection carry live traffic immediately removes the step where a user
  would have said yes.
- **A rogue DHCP server is actively harmful, not just a bug.** If an
  adapter is plugged into what is actually a live, owned network and this
  device starts answering DHCP requests on it because its own DHCP client
  attempt happened to miss a slow offer, it competes with whatever DHCP
  server already exists there — potentially breaking a network that isn't
  this appliance's to touch.

## Decision

**Interface enablement is fully automatic; the user's only manual control is
choosing MAIN vs. BACKUP for an already-enabled interface.**

- Every newly discovered atomic defaults to `NORMAL` ("main"), not `UNUSED`
  — including metered kinds (USB/phone tethering). The liveness-budget and
  activation-dwell machinery ADR-006 already built is what keeps this safe
  in practice: a newly-main tether atomic still only carries the small
  accounted liveness probe until the allocator's own activation threshold
  says it's actually needed, exactly as it does today for any `BACKUP`
  atomic. This ADR changes the *default classification*, not the spending
  discipline that already governs what a classification is allowed to do.
- A wired interface's role — WAN source vs. LAN-out — is decided
  automatically per port, not configured:
  - Attempt a real DHCP client lease. A lease means an upstream network
    exists here: treat it as a WAN atomic, exactly like today's USB
    Ethernet/tether discovery.
  - No lease within a bounded timeout does **not** immediately mean "become
    a DHCP server." First, passively listen on that segment for existing
    DHCP server traffic (`DHCPOFFER`/`DHCPACK` from something other than
    this device) for a further bounded window. Only if nothing is heard
    does the port switch into DHCP-server mode and start handing out the
    stabilized internet — the same NAT/tunnel path LAN clients already get
    via the AP.
  - This detection is conservative on purpose: a false "become a WAN
    source" costs nothing (an unused atomic just sits there); a false
    "become a DHCP server" can break someone else's live network. The
    asymmetry in cost is why the passive-listen guard exists at all.
- The AP's role narrows to **setup and fallback**, not the primary way LAN
  clients get served: wired ports are the primary path going forward.
  `hostapd`/`dnsmasq` remain independent systemd units per ADR-011 — this
  does not change how the AP is owned or brought up, only what a user is
  expected to reach for day to day once wired ports are available.

## Consequences

**Easier:**
- Genuinely zero-configuration deployment for the target use case: plug in
  adapters, get stabilized internet out the other side, no dashboard visit
  required.
- The DHCP-server-fallback path turns "spare wired port with nothing
  upstream" into a usable LAN-out port automatically, which today requires
  no manual work either — it just doesn't exist yet.

**Harder / foreclosed:**
- A user who *wants* a newly plugged-in tether to stay inert until they
  explicitly approve it loses that default — they now have to reach for
  BACKUP (or physically unplug) instead of relying on UNUSED protecting
  them. Accepted explicitly, not accidentally: the money-safety mechanism
  is the liveness budget and activation dwell (ADR-006), not the mode
  defaulting to UNUSED.
- The passive DHCP-listen guard adds real latency (a bounded window with no
  answer, twice — once for the client attempt, once for the passive
  listen) before a genuinely-unconnected wired port becomes useful. Chosen
  deliberately over speed, because the failure mode being guarded against
  is not "port takes an extra ten seconds," it's "this device just broke a
  network it doesn't own."
- Every port now runs *some* automatic classification logic instead of
  waiting for a human — a bug in that logic is now a bug in a safety-
  relevant default (rogue DHCP), not just a UX inconvenience, and gets
  reviewed accordingly (SOP-006).

**What has to stay true for this to keep making sense:** the passive
DHCP-listen guard has to actually be conservative in the field — this ADR
does not by itself prove that a bounded listen window reliably distinguishes
"nothing is here" from "something is here but slow to answer." That is a
real-hardware question (`docs/active-tests.md`, `docs/radio-spike.md`'s
pattern applies here too), not one this ADR can close by asserting a number.

## Alternatives considered

**Keep UNUSED as the default, add a "classify everything at once" bulk
action to the dashboard.** Rejected: still requires a person to be present
and to visit the dashboard at least once per device, which is exactly the
step the target use case wants removed.

**Auto-promote non-metered kinds (USB/wired Ethernet) only, leave tether
requiring explicit approval.** Considered, would have kept ADR-006's
original intent completely untouched. Not chosen: the person doing this
(per direct product direction) wants uniform "just works" behavior across
every kind, and judged the existing liveness-budget/activation-dwell
machinery sufficient protection against a defaulted-main tether spending
money before it's actually needed.

**Timeout-only detection for the DHCP-server fallback, no passive listen.**
Rejected outright: a missed or slow DHCP offer on a real, live network would
put a second DHCP server on it, which is a real, not hypothetical, way to
break someone else's connectivity. The extra latency the passive-listen
window costs is the price of not doing that.

---
