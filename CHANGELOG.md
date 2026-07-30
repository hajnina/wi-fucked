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
- architecture, ADRs, SOPs and roadmap for DIRTY -> BALANCED

### test
- scenario harness enforcing the two product invariants

### ci
- use main as the release channel
- image bake builder, versioning and packaging scripts


