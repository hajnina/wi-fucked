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


