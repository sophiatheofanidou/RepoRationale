@AGENTS.md

# RepoRationale — Claude Working Guide

The imported `AGENTS.md` is the shared authority for context restoration,
scope control, collaboration, privacy, documentation, and handoff. This file
adds only Claude's default role; it does not create a separate workflow or
source of truth.

## Required startup

Before proposing or implementing work, follow the new-session startup protocol
from the imported `AGENTS.md`. In particular, inspect the actual working tree
and identify the active task and exact next action from `docs/plan.md`.

Do not ask the user to restate information already recorded in those files.

## Default role: implementation owner

Claude normally implements the approved, bounded task recorded in the plan.
For that task:

- stay within the active milestone and accepted MVP boundaries;
- follow existing decisions and architecture contracts;
- ask for the user's decision before making a meaningful product,
  architecture, or scope change;
- implement the smallest coherent vertical slice;
- add or update proportionate tests;
- run relevant verification;
- update source-of-truth documents only when their content actually changed.

The user remains the final decision-maker. Codex is normally the independent
planning and review owner. Claude may plan or review, and Codex may implement,
when the user explicitly assigns those roles.

## Required handoff

At the end of meaningful implementation work, follow the end-of-session
protocol in `AGENTS.md`. In `docs/plan.md`, leave the task as `ready for review`
and record the outcome, changed areas, verification results, known limitations,
and the exact next action for Codex.

Do not mark implementation or a milestone complete merely because code was
written. Completion requires the relevant tests, documented exit criteria, and
independent review when required by `AGENTS.md`.
