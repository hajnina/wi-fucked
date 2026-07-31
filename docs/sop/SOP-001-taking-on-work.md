# SOP-001 — Taking on work

Applies before you write a line of code.

## 1. Understand which workstream you are in

Work is organised into workstreams (see [`../roadmap.md`](../roadmap.md)). Know
yours, because it tells you what you own and what you must not change.

| | Owns | Must not change without an ADR |
|---|---|---|
| WS-A Connectivity | `discovery/`, `atomics/`, `hal/` | The identity scheme |
| WS-B Measurement | `probe/`, `capacity/`, `demand/` | The capacity model's public shape |
| WS-C Control | `policy/`, `allocator/` | Mode semantics, priority ladder |
| WS-D Enforcement | `enforce/` | Which kernel primitives are used |
| WS-E Fabric | `tunnel/`, `fabric/` | The tunnel protocol |
| WS-F Surface | `api/`, `ui/`, `telemetry/` | The decision-record schema |
| WS-G Platform | `.github/`, `scripts/`, provisioning | The release contract |

Touching another workstream's module is normal and fine. Changing its *interface*
is a conversation, not a commit.

## 2. Read the ADRs that constrain your module

Not all of them — the ones that apply. `docs/adr/README.md` has the index. If your
change contradicts an ADR, stop and read [SOP-007](SOP-007-architectural-decisions.md)
before continuing. Contradicting an ADR by accident is the single most expensive
mistake available in this codebase, because it usually works locally.

## 3. Establish how you will know it works

Before writing the implementation, answer: **what scenario proves this?**

For anything in `policy/`, `allocator/`, `enforce/`, or `radio/`, that answer must
be a scenario test — it is mandatory, not optional
([SOP-003](SOP-003-testing.md)). Write the scenario first if you can. It clarifies
the requirement more reliably than a ticket does.

If you cannot describe a failing test that your change would make pass, you do not
yet understand the task well enough to start.

## 4. Check it runs under `MOCK_HW=1`

Every module must be exercisable without a Pi, a radio, or root. If the task as
described seems to require hardware, that is a design problem to solve now — add
the HAL seam — not a constraint to accept.

```bash
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m wifucked
```

## 5. Branch

```bash
git checkout main
git pull origin main
git checkout -b <type>/<short-description>
```

Branch prefix matches the conventional-commit type you expect to use: `feat/`,
`fix/`, `docs/`, `refactor/`, `ci/`.

## When to stop and ask

Ask before proceeding — do not guess — when:

- Your change would contradict an ADR.
- You need to change another workstream's interface.
- The task requires hardware behaviour nobody has verified. Check
  [`../radio-spike.md`](../radio-spike.md) findings first; if the answer isn't
  there, the honest move is a spike, not an assumption.
- The requirement is ambiguous in a way that changes the design. Two plausible
  readings that produce the same code is not ambiguity worth blocking on. Two that
  produce different architectures is.
