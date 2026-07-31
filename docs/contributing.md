# Contributing

## Start here

1. Read [`../CLAUDE.md`](../CLAUDE.md) — the binding rules.
2. Read [`sop/`](sop/) — the standard operating procedures. **They are binding.**
3. Read the ADRs that constrain your module — [`adr/README.md`](adr/README.md) has
   the index and a suggested reading order.

Then [SOP-001](sop/SOP-001-taking-on-work.md) walks you through taking on your
first piece of work.

## Set up

```bash
git clone https://github.com/hajnina/wi-fucked.git
cd wi-fucked
pip install -r appliance/requirements.txt -r appliance/requirements-dev.txt

MOCK_HW=1 PYTHONPATH=appliance/src python3 -m wifucked     # dashboard on :8080
./run_all_tests.sh
```

No Raspberry Pi required, and none should ever be. `MOCK_HW=1` is the primary
development path — if something can only be tested on hardware, that is a design
problem to fix, not a constraint to accept.

## The short version of the rules

The detail is in the SOPs; this is what gets a PR sent back most often.

- **No packet is touched by Python.** The daemon programs the kernel.
- **Never key on an interface name.** `wlan1` becomes `wlan2`. Use the atomic ID.
- **Never tear down kernel state on exit.** A crash must not become an outage.
- **`get_logger`, never `logging.getLogger`**, and every log line describing work
  carries `workflow`, `state`, and `intent`.
- **Conventional commits are mandatory** — the release version is derived from them.
- **Scenario test required** for any change to `policy/`, `allocator/`, `enforce/`,
  or `radio/`.
- **A change that makes a document wrong is not finished** until the document is
  fixed, in the same PR.

## Workflow

```bash
git checkout main && git pull origin main
git checkout -b feat/short-description

# work, following SOP-002 and SOP-003

./run_all_tests.sh          # green locally, before you push
git push -u origin feat/short-description
```

Open a PR against `main`. There is no other branch. Fill in the description
template from [SOP-005](sop/SOP-005-commits-and-pull-requests.md) — What, Why, How,
Verification, Risk.

**Every merge to `main` publishes a release that devices will install.** If you
are not ready for users to have it, do not merge it.

## Commits

```
<type>(<scope>): <subject>
```

`feat` `fix` `perf` `refactor` `docs` `test` `build` `ci` `chore` — with `!` or a
`BREAKING CHANGE:` footer for a major bump.

```
feat(allocator): activate backup only after sustained critical deficit
fix(discovery): key USB atomics on serial instead of enumeration order
docs(sop): add field-debugging procedure
```

`feat: updates` and `chore: wip` are not acceptable — these subjects become the
changelog.

## Review

Expect turnaround within a working day. Expect comments labelled by severity —
`blocking:`, `question:`, `nit:`, `praise:` — and expect to be asked for a scenario
test if you touched control code.

Disagreeing with a review comment is fine and expected. Say why. If it does not
converge in two rounds, pull in a third person rather than grinding in the thread.

Reviewing is covered by [SOP-006](sop/SOP-006-code-review.md), including what to
check and in what order.

## Documentation is part of the work

Not follow-up, not a separate ticket. If your change makes an SOP, ADR, or doc
wrong, fix it in the same PR ([SOP-010](sop/SOP-010-keeping-documentation-current.md)).

- Changed **how the team works** → update the SOP.
- Changed a **boundary, format, or contract** → write a new ADR. Never edit an
  existing one; supersede it.
- Changed a **binding rule** → update `CLAUDE.md` *and* the relevant SOP.

An SOP that describes a workflow nobody follows is worse than no SOP. Fixing one is
a normal, encouraged, one-line pull request.

## When to ask rather than guess

- Your change would contradict an ADR.
- You need to change another workstream's interface.
- The task depends on hardware behaviour nobody has verified — check
  [`radio-spike.md`](radio-spike.md) first; if the answer is not there, the honest
  move is a spike, not an assumption.
- Two readings of the requirement would produce genuinely different architectures.

Two readings that produce the same code is not ambiguity worth blocking on. Pick
one and note the assumption.
