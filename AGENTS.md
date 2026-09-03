# RepoRationale — AI Collaboration Guide

This file is the shared operating guide for AI agents working on
RepoRationale. It defines how a new session restores context, how work is
coordinated, and how the repository is left ready for the next session.

## 1. Authority and source of truth

Read the following documents before proposing or changing project work:

1. `docs/project.md` — product definition, users, MVP scope, and non-goals.
2. `docs/decisions.md` — accepted and rejected product or technical choices.
3. `docs/architecture.md` — current component boundaries and contracts, once it
   exists.
4. `docs/evaluation.md` — datasets, metrics, experiments, and results, once it
   exists.
5. `docs/plan.md` — current milestone, active task, progress, and next action.

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
- Claude is the default implementation owner. It normally implements an
  approved, bounded task, adds tests, and prepares the work for review.
- Codex is the default planning and review owner. It normally keeps the work
  aligned with the plan, reviews the actual changes and verification results,
  and checks architecture, evaluation quality, and scope boundaries.

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
4. Identify from `docs/plan.md` the current milestone, active task, task status,
   last completed work, blockers or open questions, and exact next action.
5. Confirm that the requested work belongs to the active milestone and does not
   violate an accepted decision or MVP non-goal.
6. Continue from the recorded state. Do not ask the user to repeat project
   context already captured in these files.

If documents disagree, stop and surface the conflict instead of silently
choosing one. If the plan is stale, reconcile it with the actual repository
state before starting unrelated work.

## 4. Working protocol

- Work in small, testable vertical slices tied to the active task.
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

When Claude finishes an implementation task, it should leave the task as
`ready for review` in `docs/plan.md` and record:

- the bounded outcome completed;
- the main files or contracts changed;
- verification performed and its result;
- known limitations, deviations, or unresolved questions;
- the next review action.

Codex reviews the actual diff, implementation, and verification evidence rather
than relying only on the handoff summary. After review, it records either
`changes requested` with actionable findings or `complete` when the task and
its relevant exit criteria are genuinely satisfied.

Small documentation corrections do not require a separate cross-agent review.
Code changes, architecture-affecting work, ingestion or retrieval behaviour,
and milestone completion normally do.

## 7. End-of-session protocol

After a meaningful work session, update `docs/plan.md` so a fresh session can
resume without chat history. Keep its handoff state compact and include:

- current milestone;
- active task;
- task status: `ready`, `in progress`, `ready for review`,
  `changes requested`, `blocked`, or `complete`;
- last completed work;
- verification performed;
- open questions or blockers;
- one exact next action.

Update other documents only when their source-of-truth content changed:

- product scope or behaviour → `docs/project.md`;
- accepted or rejected choice → `docs/decisions.md`;
- component boundary or technical contract → `docs/architecture.md`;
- dataset, metric, experiment, or result → `docs/evaluation.md`;
- private learning explanation → `.local/learning-notes.md`, when available.

Before declaring a milestone complete, verify its exit criteria in
`docs/plan.md`. Leave the repository with one clear next action even when the
current task is blocked or incomplete.
