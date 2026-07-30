# SOP-010 — Keeping documentation current

This SOP is about the other SOPs, the ADRs, `CLAUDE.md`, and the docs. It is
binding like the rest.

## Why this one exists

Stale documentation is worse than none. Missing docs make people ask; wrong docs
make them confidently do the wrong thing, with the authority of a written
procedure behind them. The first time a new engineer follows an SOP and it doesn't
match reality, every other document in the repository loses credibility — and you
do not get that back cheaply.

So: **updating documentation is part of the work, not follow-up.** A change that
makes a document wrong is not finished until that document is right. Same PR.

## What to update when

| You changed | Update |
|---|---|
| How the team works | The relevant SOP |
| A module boundary, format, or contract | A new ADR ([SOP-007](SOP-007-architectural-decisions.md)) + the ADR index |
| A rule that must never be broken | `CLAUDE.md` + the SOP that elaborates it |
| Behaviour a doc describes | That doc |
| A dev command | `CLAUDE.md`, `README.md`, and the SOP that quotes it |
| Hardware understanding | [`../hardware.md`](../hardware.md) and [`../radio-spike.md`](../radio-spike.md) |
| Roadmap scope or phase exit criteria | [`../roadmap.md`](../roadmap.md) |

`grep` for what you changed before assuming nothing references it. Commands and
file paths in particular get quoted in several places.

## SOPs are living; ADRs are not

The distinction matters and is easy to get backwards.

| | ADR | SOP |
|---|---|---|
| Records | A decision at a point in time | Current practice |
| Edit it? | **No.** Supersede with a new one. | **Yes.** Freely. |
| Should it change? | Only when the decision changes | Whenever practice changes |
| Stale means | Superseded, still valid as history | Actively harmful |

An ADR that no longer applies is *history*. An SOP that no longer applies is a
*bug*.

## Changing an SOP

By pull request, with a reviewer, like code. `docs(sop): ...`

A one-line diff to an SOP is normal, healthy, and encouraged. You do not need
consensus to fix a step that is wrong — you need a reviewer to confirm you are not
removing a step that exists for a reason you haven't hit yet.

**An SOP that has not changed in six months of active development is more likely
stale than perfect.** If you notice one describing a workflow nobody follows, fix
it or delete the step. Do not leave it as decoration.

## If an SOP is wrong for your situation

1. **Do not silently ignore it.** That is how documentation stops being trusted.
2. Follow it, or change it. Both are fine; neither is skipping it.
3. If it is wrong only for your edge case, add the exception to the SOP — the next
   person will hit the same case.
4. If you cannot change it right now, say so explicitly in the PR description.
   Visible deviation is recoverable; invisible deviation is not.

## Quarterly review

Once a quarter, someone reads all of `docs/sop/` end to end and asks of each step:
*do we actually do this?* Steps that survive stay. Steps that don't get fixed or
deleted.

This takes about an hour and is worth roughly a week of confusion for the next
person to join. Rotate who does it — a fresh reader notices staleness the author
cannot see.

## Documentation quality bar

- **Write for someone who wasn't there.** No unexplained shorthand, no "as
  discussed."
- **Say why, not just what.** A rule without a reason gets worked around the first
  time it is inconvenient.
- **Be concrete.** A command that can be pasted beats a description of a command.
  A code example beats a paragraph.
- **Be honest about what is unknown.** "This is unverified — see the spike" is far
  more useful than confident prose that turns out to be wrong. Several ADRs here
  encode expectations about driver behaviour rather than measured facts, and they
  say so.
- **Keep it short enough to be read.** A procedure nobody finishes is not a
  procedure.

## The agent files

`CLAUDE.md`, `AGENTS.md`, and `GEMINI.md` are the entry points that AI coding
agents and new humans read first. `CLAUDE.md` is canonical; the others point to it
so there is exactly one copy of every rule.

Keeping them accurate matters more than keeping the rest accurate, because an
agent following a stale rule will apply it consistently across an entire change
before anyone notices.

**When you change a binding rule, update `CLAUDE.md` in the same PR.** Do not let
the SOPs and `CLAUDE.md` drift — `CLAUDE.md` states the rules and links here for
the detail, so a rule that changes has to change in both.
