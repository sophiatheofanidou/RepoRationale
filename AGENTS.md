# RepoRationale: AI Collaboration Guide

This file is the shared operating guide for AI agents working on
RepoRationale. It defines how a new session restores context, how work is
coordinated, and how the repository is left ready for the next session.

## 1. Authority and source of truth

Read the following documents before proposing or changing project work:

1. [Project definition](docs/project.md): users, first-release scope, and
   boundaries.
2. [Decision log](docs/decisions.md): accepted, superseded, and deferred
   product or technical choices.
3. [Architecture](docs/architecture.md): current component boundaries and
   contracts.
4. [Evaluation](docs/evaluation.md): datasets, metrics, experiments, and
   results.
5. [Implementation plan](docs/plan.md): completed first-release milestones and
   verification. A future implementation cycle may introduce a new active
   plan.

These files are authoritative for their respective concerns. Do not create a
second source of truth or treat old chat content as more current than the
repository documents.

If `.local/context.md` is available, read it as supplementary private context.
It may direct the session to other local learning or reference material and may
influence how work is explained or sequenced, but it does not override the
canonical documents. Never commit, quote, summarize, or otherwise expose
`.local/` content in public project files.

## 2. Default collaboration roles

- The user is the product owner and final decision-maker. Significant changes
  to product scope, architecture, priorities, or accepted decisions require
  the user's approval.
- Claude is the default implementation owner. It implements only the bounded
  task supplied in a Codex-authored implementation prompt, adds tests, runs the
  requested verification, and reports its handoff without editing project
  state or canonical documentation.
- Codex is the planning, orchestration, documentation, and review owner. It
  keeps implementation work aligned with any active plan, authors each bounded
  implementation prompt, reviews the actual changes and verification results,
  updates task state when one is being tracked, and checks architecture,
  evaluation quality, and scope boundaries.

These are default responsibilities, not capability restrictions. Either tool
may perform another role when the user explicitly requests it. Avoid having two
agents edit the same task concurrently unless the work has been deliberately
split into non-overlapping parts.

## 3. New-session startup protocol

At the beginning of every new session:

1. Read this file and the canonical documents in the order listed above.
2. Read the optional local context files when they exist, without exposing
   their contents publicly.
3. Inspect the current working tree and relevant implementation before relying
   on a previous agent's summary.
4. Determine from the [implementation plan](docs/plan.md) whether the project
   has an active implementation cycle or a completed delivery record. If a new
   active plan exists, identify its milestone, task status, blockers, and exact
   next action.
5. Confirm that the requested work is either release administration or belongs
   to an active plan, and that it does not violate an accepted decision or
   first-release non-goal.
6. Continue from the recorded state. Do not ask the user to repeat project
   context already captured in these files.

If documents disagree, stop and surface the conflict instead of silently
choosing one. Do not reopen a completed plan for release administration or
small documentation corrections. New post-release implementation requires a
new bounded plan.

## 4. Working protocol

- When implementation is governed by an active plan, work in small, testable
  vertical slices tied to its active task.
- Define logical commit checkpoints when planning a milestone. After Codex
  accepts a vertical slice, stop before starting an unrelated slice: Codex
  presents the exact files and proposed message, the user explicitly approves
  the commit, and only then is the checkpoint created. If the user deliberately
  defers it, record that choice in the active plan rather than silently
  accumulating multiple accepted slices. Claude never creates the checkpoint.
- Do not implement deferred features or broaden supported platforms, sources,
  authentication, deployment, or infrastructure without an accepted decision.
- Discuss meaningful new product or architecture decisions with the user before
  recording or implementing them. Routine implementation details inside an
  approved contract do not require a new decision record.
- Treat generated indexes, local data, credentials, and `.local/` material as
  non-public. Never commit secrets or generated repository snapshots.
- Preserve unrelated user changes and inspect existing code before editing.
- Verify changes in proportion to their risk. Record commands and outcomes
  needed for another agent to understand what was actually checked.
- Keep public documentation concise. Preserve the established language and
  purpose of each document; do not copy chat transcripts into the repository.
- Keep internal tracking identifiers confined to their source-of-truth files:
  milestone identifiers (the letter M followed by a number) may appear only in
  the [implementation plan](docs/plan.md), and numbered decision identifiers
  may appear only in the [decision log](docs/decisions.md) and
  [implementation plan](docs/plan.md). In source code, tests, README,
  architecture, evaluation, and other audience-facing material, state the
  relevant behaviour or rationale directly without those internal identifiers.

## 5. Git authorship and publishing boundary

The user owns the repository and is the author responsible for its commits. AI
tools may prepare changes, inspect Git state, and propose commit messages, but
must not create a commit, tag, push, force-push, publish a branch, create or
change a remote, or open or update a pull request unless the user explicitly
approves that exact action in the current conversation.

Approval to edit files, implement a task, or run tests is not approval to
commit or publish the result. Commit approval and push approval must not be
inferred from earlier approvals. A single user instruction may authorize both
only when it clearly names both actions and their intended scope.

Before creating an approved commit:

- show or summarize the exact files that will be included and the proposed
  commit message;
- inspect the effective repository-local Git `user.name` and `user.email`;
- confirm that they are the user's intended identity and that the email is
  associated with the user's GitHub account or is the user's GitHub-provided
  `noreply` address;
- stop and ask the user if the identity is missing, ambiguous, or belongs to an
  AI tool, automation account, or another person;
- never add an AI tool as author or co-author and never override author or
  committer metadata unless the user explicitly requests it.

Before an approved push, confirm the exact remote and branch. Never force-push,
rewrite published history, or amend an existing commit without separate,
explicit user approval.

The user's name and email must remain in local Git configuration, not in these
public instructions. When the repository is initialized, configure and verify
its repository-local identity before the first commit.

## 6. Implementation and review handoff

When Claude finishes an implementation task, it reports to Codex without
editing the [implementation plan](docs/plan.md),
[project definition](docs/project.md), [decision log](docs/decisions.md),
[architecture](docs/architecture.md), [evaluation](docs/evaluation.md), or the
agent instruction files. Its handoff should state:

- the bounded outcome completed;
- the main files or contracts changed;
- verification performed and its result;
- known limitations, deviations, or unresolved questions;
- the next review action.

Codex reviews the actual diff, implementation, and verification evidence rather
than relying only on the handoff summary. Codex alone updates the canonical
documents and records task state, including `ready for review`, `changes
requested`, or `complete`. Claude must not accept its own work or choose the
next task.

Small documentation corrections do not require a separate cross-agent review.
Code changes, architecture-affecting work, ingestion or retrieval behaviour,
and milestone completion normally do.

## 7. End-of-session protocol

After meaningful work governed by an active implementation plan, Codex updates
that plan so a fresh session can resume without chat history. Implementation
agents report their results to Codex and do not edit the plan. Keep its handoff
state compact and include:

- current milestone;
- active task;
- task status: `ready`, `in progress`, `ready for review`,
  `changes requested`, `blocked`, or `complete`;
- last completed work;
- verification performed;
- open questions or blockers;
- one exact next action.

When no active implementation plan exists, do not reopen the completed plan for
release administration or small documentation corrections. Update other
documents only when their source-of-truth content changed:

- product scope or behaviour → [project definition](docs/project.md);
- accepted or rejected choice → [decision log](docs/decisions.md);
- component boundary or technical contract →
  [architecture](docs/architecture.md);
- dataset, metric, experiment, or result →
  [evaluation](docs/evaluation.md);
- private learning explanation → `.local/learning-notes.md`, when available.

Before declaring a milestone in an active plan complete, verify its exit
criteria. Leave active implementation work with one clear next action even when
the current task is blocked or incomplete.
