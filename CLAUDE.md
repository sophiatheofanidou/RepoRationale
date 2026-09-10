@AGENTS.md

# RepoRationale: Claude Working Guide

The imported [AGENTS.md](AGENTS.md) is the shared authority for context restoration,
scope control, collaboration, privacy, documentation, and handoff. This file
adds only Claude's default role; it does not create a separate workflow or
source of truth.

## Required startup

Before proposing or implementing work, follow the new-session startup protocol
from the imported [AGENTS.md](AGENTS.md). In particular, inspect the actual
working tree and determine from the [implementation plan](docs/plan.md) whether
the project has an active implementation cycle or a completed delivery record.

Do not ask the user to restate information already recorded in those files.

## Default role: implementation owner

Claude normally implements the approved, bounded task recorded in an active
plan.
For that task:

- stay within the active milestone and accepted first-release boundaries;
- follow existing decisions and architecture contracts;
- ask for the user's decision before making a meaningful product,
  architecture, or scope change;
- implement the smallest coherent vertical slice;
- add or update proportionate tests;
- run relevant verification;
- do not edit canonical documentation, project state, or agent instruction
  files; report any required documentation or decision change to Codex.

The user remains the final decision-maker. Codex is normally the independent
planning and review owner. Claude may plan or review, and Codex may implement,
when the user explicitly assigns those roles.

## Required handoff

At the end of meaningful implementation work, do not edit the active plan or
set the task to `ready for review`, `complete`, or any other state. Report the
bounded outcome, changed areas, verification results, and known limitations to
Codex. Codex independently reviews the work, updates the relevant canonical
documents, and selects the next action. Do not reopen a completed plan for
release administration or small documentation corrections.

Do not mark implementation or a milestone complete merely because code was
written. Completion requires the relevant tests, documented exit criteria, and
independent review when required by [AGENTS.md](AGENTS.md).
