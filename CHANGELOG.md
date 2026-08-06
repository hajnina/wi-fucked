## v1.3.4 — 2026-08-06

### docs
- changelog for v1.3.3 [skip ci]


## v1.3.3 — 2026-08-06

### fix
- give hostapd a control socket; add periodic diagnostics (#43)
- ship the hotspot open, unauthenticated, on first boot (#42)

### docs
- changelog for v1.3.2 [skip ci]


## v1.3.2 — 2026-08-06

### fix
- default to one plain hotspot and USB-only WAN, not two unverified SSIDs (#41)

### docs
- reflect item 12's dedicated telemetry cadence (#40)
- changelog for v1.3.1 [skip ci]


## v1.3.1 — 2026-08-05

### fix
- flush on the documented cadence, persist capacity confidence across restarts (#39)

### docs
- mark item 12 merged — all 14 backlog items closed out
- changelog for v1.3.0 [skip ci]
- changelog for v1.3.0 [skip ci]


## v1.3.0 — 2026-08-04

### feat
- route LAN egress through the tunnel, add fabric NAT (ADR-019) (#35)

### fix
- liveness stamping side-effect, non-USB Ethernet mislabeling, non-numeric limit param (#38)
- guard registry and telemetry state with a thread lock (#36)

### perf
- stop the medium loop from starving the fast loop (#37)

### docs
- mark item 13 merged
- mark items 7 and 8 merged
- mark item 5 merged
- changelog for v1.2.5 [skip ci]


## v1.3.0 — 2026-08-04

### feat
- route LAN egress through the tunnel, add fabric NAT (ADR-019) (#35)

### fix
- liveness stamping side-effect, non-USB Ethernet mislabeling, non-numeric limit param (#38)
- guard registry and telemetry state with a thread lock (#36)

### perf
- stop the medium loop from starving the fast loop (#37)

### docs
- mark item 13 merged
- mark items 7 and 8 merged
- mark item 5 merged
- changelog for v1.2.5 [skip ci]


## v1.2.5 — 2026-08-04

### docs
- changelog for v1.2.4 [skip ci]


## v1.2.4 — 2026-08-04

### fix
- drive Wi-Fi-as-WAN via iw instead of nmcli (#27)
- require authentication, bind off 0.0.0.0 (#33)
- stop duplicate message/asctime fields, configure fabric logging (#26)

### docs
- mark item 14 merged
- mark item 1 merged
- mark items 9 and 11 merged
- changelog for v1.2.3 [skip ci]
- changelog for v1.2.3 [skip ci]

### ci
- add automerge sweep as a backstop for the one-shot merge trigger (#34)
- pass FABRIC_VERSION build-arg and verify the baked version (#29)


## v1.2.3 — 2026-08-04

### fix
- dedupe SSID-only atomics, stop wearing the SD card (#32)
- table-per-atomic routing, skip zero-ceiling shares, honor quiesced, shape from up_bps (#31)
- hysteresis stuck-in-ACTIVE, duplicate backup-is-primary shares, ARMING decision record lies (#30)
- restore backlog tracker (previous commit corrupted it to a literal shell string)
- feed the systemd watchdog on the fast loop (#25)
- set real version at build time (#24)

### docs
- mark items 4 and 10 merged
- mark item 6 merged
- mark item 3 merged
- mark item 2 merged
- add ordered backlog for traffic-passing blockers (#23)
- changelog for v1.2.2 [skip ci]

### test
- drive harness assertions from Enforcer/MockAp (#28)


## v1.2.3 — 2026-08-04

### fix
- dedupe SSID-only atomics, stop wearing the SD card (#32)
- table-per-atomic routing, skip zero-ceiling shares, honor quiesced, shape from up_bps (#31)
- hysteresis stuck-in-ACTIVE, duplicate backup-is-primary shares, ARMING decision record lies (#30)
- restore backlog tracker (previous commit corrupted it to a literal shell string)
- feed the systemd watchdog on the fast loop (#25)
- set real version at build time (#24)

### docs
- mark items 4 and 10 merged
- mark item 6 merged
- mark item 3 merged
- mark item 2 merged
- add ordered backlog for traffic-passing blockers (#23)
- changelog for v1.2.2 [skip ci]

### test
- drive harness assertions from Enforcer/MockAp (#28)


## v1.2.2 — 2026-08-02

### fix
- grant automerge's GITHUB_TOKEN actions:write for the release dispatch (#22)

### docs
- changelog for v1.2.1 [skip ci]

### ci
- only upload build caches from fast-uplink runners (#21)


## v1.2.1 — 2026-08-02

### fix
- stop NetworkManager fighting hostapd for the AP radio (#19)
- force USB OTG host mode, and add temporary bring-up diagnostics (#16)

### docs
- changelog for v1.2.0 [skip ci]

### ci
- dispatch a release build after auto-merging (#20)
- route bake and fabric jobs to arc-runner-set (#18)


## v1.2.0 — 2026-08-01

### feat
- classify USB tether vs Ethernet adapter by interface descriptor (#13)

### docs
- track unconfirmed-on-hardware code paths in active-tests.md (#14)
- changelog for v1.1.0 [skip ci]

### ci
- add ARC test workflow (manual trigger, arc-runner-set)


## v1.1.0 — 2026-07-31

### feat
- add a DIY auto-merge workflow, and stop treating it as a reason to wait (#12)

### docs
- changelog for v1.0.0 [skip ci]


## v1.0.0 — 2026-07-31

### docs
- changelog for v0.4.0 [skip ci]

### chore
- rename brand from Dirty to Wi-Fucked (#11)


## v0.4.0 — 2026-07-31

### feat
- wire real hardware implementations and attach to the fabric (#10)

### docs
- changelog for v0.3.1 [skip ci]


## v0.3.1 — 2026-07-31

### docs
- changelog for v0.3.0 [skip ci]


## v0.3.0 — 2026-07-31

### feat
- real WireGuard tunnel and fabric peer registration (#9)
- apply real nftables marking, policy routing and readback (#8)
- real saturation, active RTT, and per-class demand (#7)
- add rollback watchdog (#6)

### docs
- changelog for v0.2.1 [skip ci]


## v0.2.1 — 2026-07-31

### docs
- changelog for v0.2.0 [skip ci]


## v0.2.0 — 2026-07-31

### feat
- expose which kernel interface carries each service class (#5)
- configurable address, admin auth, and first-run wizard (#4)

### docs
- changelog for v0.1.0 [skip ci]


## v0.1.0 — 2026-07-30

### feat
- provisioning with the AP decoupled from the daemon
- control-plane skeleton with real seams for each workstream

### fix
- give the release bake job a runner that actually exists (#3)
- use find instead of ls for kernel version lookup
- fix bake capability gate and fabric buildx cache export

### docs
- document a self-checked CI wait-loop for PR ownership
- schedule PR ownership checks
- TODO with the CI workflows bundled for manual install
- architecture, ADRs, SOPs and roadmap for WI-FUCKED -> BALANCED

### test
- scenario harness enforcing the two product invariants

### ci
- use main as the release channel
- image bake builder, versioning and packaging scripts


