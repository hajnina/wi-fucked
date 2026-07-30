# GEMINI.md

Entry point for Gemini working in this repository.

## Read `CLAUDE.md` first

**[`CLAUDE.md`](CLAUDE.md) is the canonical rules file**, regardless of which agent
you are. Every binding rule lives there and nowhere else, so there is exactly one
copy of each and no chance of two files disagreeing.

This file exists so that a tool looking for `GEMINI.md` finds its way there. It
deliberately does not restate the rules — see [`AGENTS.md`](AGENTS.md) for the same
pointer and a short orientation.

## Obligations

The standard operating procedures in [`docs/sop/`](docs/sop/) are **binding**. They
must be followed, and they must be **kept current** — a change that makes one wrong
is not finished until that SOP is fixed in the same pull request. See
[SOP-010](docs/sop/SOP-010-keeping-documentation-current.md).

Do not add rules to this file. Add them to `CLAUDE.md` and let this one keep
pointing there.
