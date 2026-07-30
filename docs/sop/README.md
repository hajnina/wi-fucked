# Standard Operating Procedures

These SOPs are **binding**. They are not suggestions, style preferences, or
aspirational documentation. Work that does not follow them gets sent back.

They exist because this project will be built mostly by people who did not make
the architectural decisions, on hardware they cannot easily observe, for failure
modes that only appear in the field. The SOPs encode the things that are expensive
to rediscover.

## The procedures

| SOP | Covers | Read it when |
|---|---|---|
| [SOP-001](SOP-001-taking-on-work.md) | Taking on work | Before you write any code |
| [SOP-002](SOP-002-writing-code.md) | Writing code | Every time |
| [SOP-003](SOP-003-testing.md) | Testing | Before you claim something works |
| [SOP-004](SOP-004-logging-and-observability.md) | Logging and observability | Every time you add a log line |
| [SOP-005](SOP-005-commits-and-pull-requests.md) | Commits and pull requests | Before you commit |
| [SOP-006](SOP-006-code-review.md) | Code review | When reviewing, and before requesting review |
| [SOP-007](SOP-007-architectural-decisions.md) | Architectural decisions | When you want to change a boundary |
| [SOP-008](SOP-008-release-and-ota.md) | Release and OTA | Before touching the pipeline or shipping |
| [SOP-009](SOP-009-hardware-and-field-debugging.md) | Hardware and field debugging | When something works locally but not on a Pi |
| [SOP-010](SOP-010-keeping-documentation-current.md) | Keeping documentation current | Continuously — this one is about the others |

## The three rules about the SOPs themselves

**1. Follow them.** If an SOP is wrong for your situation, that is a signal to
change the SOP, not to quietly ignore it. Ignoring one silently is how a team ends
up with documentation nobody trusts, which is worse than no documentation.

**2. Keep them current.** An SOP that describes a workflow the team no longer
follows is actively harmful — it teaches new people the wrong thing with the
authority of a written procedure. Updating the relevant SOP is **part of the work**,
not follow-up. See [SOP-010](SOP-010-keeping-documentation-current.md).

**3. Change them deliberately.** SOPs change by pull request like everything else,
with a reviewer. A one-line diff to an SOP is a normal, healthy, encouraged thing.
An SOP that has not changed in six months of active development is probably stale
rather than perfect.

## Relationship to ADRs

They answer different questions and have different lifecycles.

| | ADR | SOP |
|---|---|---|
| Answers | *Why is the system like this?* | *How do we work?* |
| Lifecycle | Immutable. Superseded, never edited. | Living. Edited freely. |
| Changing it | Write a new ADR that supersedes the old one | Open a PR against the SOP |
| Location | [`docs/adr/`](../adr/) | `docs/sop/` |

An ADR records a decision at a point in time and stays as history. An SOP
describes current practice and should always reflect reality.
