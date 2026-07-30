# AGENTS.md

Entry point for AI coding agents and new contributors working in this repository.

## Read `CLAUDE.md` first

**[`CLAUDE.md`](CLAUDE.md) is the canonical rules file.** Every binding rule lives
there and nowhere else, so there is exactly one copy of each and no chance of two
files disagreeing.

This file exists so that agents looking for `AGENTS.md` find their way there. It
deliberately does not restate the rules.

## The short version

Enough to orient; not enough to work from.

- **DIRTY → BALANCED** is an autonomous connectivity appliance on a Raspberry Pi
  Zero 2W. It turns chaotic WAN connectivity into two LAN networks that never go
  away.
- **The control plane is Python; the data plane is the kernel.** No packet is ever
  touched by Python.
- **One branch: `master`.** Every merge publishes an immutable release.
- **Conventional commits are mandatory** — the release version is derived from them.
- **`MOCK_HW=1` is the primary development path.** Nothing requires a Pi to test.

```bash
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m dirty     # run it
./run_all_tests.sh                                      # test it
```

## Where things are

| Read | For |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | The rules. Start here. |
| [`docs/sop/`](docs/sop/) | **Binding** standard operating procedures — how to work |
| [`docs/adr/`](docs/adr/) | Architectural decisions and why they're load-bearing |
| [`docs/architecture.md`](docs/architecture.md) | How the system fits together |
| [`docs/hardware.md`](docs/hardware.md) | What a Pi Zero 2W can and cannot do |
| [`docs/roadmap.md`](docs/roadmap.md) | Phases, workstreams, exit criteria |

## Obligations

The SOPs are binding, must be followed, and **must be kept current** — a change
that makes one wrong is not finished until that SOP is fixed in the same pull
request. See [SOP-010](docs/sop/SOP-010-keeping-documentation-current.md).

If you change a binding rule, update `CLAUDE.md` in the same PR. Do not add rules
to this file; add them to `CLAUDE.md` and let this one keep pointing there.
