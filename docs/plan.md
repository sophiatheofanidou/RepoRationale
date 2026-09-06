# RepoRationale — Implementation Plan and Current State

**Project state:** Implementation  
**Current milestone:** M3 — Retrieval foundation
**Last updated:** 2026-09-06

This document is the source of truth for current progress, the active
milestone, and the next implementation steps. It is not a detailed activity log
or a replacement for the product and architecture documents.

## 1. Current state

**Active task:** Define the M3 chunk identity and source-aware splitting contract
**Task status:** Ready
**Last completed work:** M2 is complete after independent review and its
accepted implementation has been separated into collaboration, domain,
GitHub-adapter, and application/snapshot commit checkpoints. The project
accepts one public GitHub repository, validates it through preflight, collects
the complete supported pull-request, issue, comment, review, commit, and
Markdown corpus, normalizes stable evidence sources, and atomically persists a
validated reusable snapshot. Repository-wide comment collections reduced
requests, eight bounded workers handle the unavoidable per-PR review calls, and
the shared D-028 defaults enforce admission limits of 700 roots, 500 closed
pull requests, 1,100 commits, and 100 tree entries plus runtime caps of 3,000
sources and 750 collection requests.
**Verification performed:** Codex independently ran `uv sync --locked`,
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
`uv run pytest -q`; all passed, with mypy reporting no issues in 48 source files
and pytest reporting 478 passing tests in 1.47 seconds. `git diff --check`
passed apart from line-ending notices. Authenticated builds of
`pallets/itsdangerous` and `pallets/markupsafe` produced deterministic reusable
snapshots; the optimized `markupsafe` run used 411 requests including lookup.
No credential, generated snapshot, index, or `.local/` file is tracked.
**Known limitations or deviations:** M2 produces normalized sources, not
searchable chunks, embeddings, or a vector index. Its two completed development
samples do not establish general production capacity, and repositories beyond
the measured D-028 envelope are intentionally unsupported.
**Open questions or blockers:** None. The exact chunk identity and splitting
rules still need to be proposed and reviewed before implementation.
**Next action:** Codex prepares one bounded Claude implementation prompt for
the chunk identity and source-aware splitting contract, including only the
minimum focused tests, then reviews the resulting diff before updating task
state.

The project now has a tested, repeatable GitHub ingestion and normalized-
snapshot foundation. M3 builds retrieval artifacts from that canonical local
corpus without reopening the GitHub ingestion scope.

Agreed product boundaries are recorded in `project.md`. Accepted and proposed
choices are recorded in `decisions.md`. The original draft remains reference
material and is no longer authoritative.

Python (D-008), Chroma for MVP vector storage (D-009), and a project-owned agent
loop without a large orchestration framework (D-010) are accepted. Streamlit is
the only MVP user-facing interface (D-013). PostgreSQL/pgvector is the first
planned post-MVP improvement (D-014), not an MVP requirement. Anthropic Claude
is the generation provider, with the exact model deferred to a bounded
project-specific comparison (D-015), and Voyage 4 is the embedding model
(D-016). Repository onboarding is bounded and self-service (D-018), accepted
repositories are indexed across their complete supported corpus (D-019), local
snapshots are persisted and reused (D-020), and embeddings are created per
source-aware chunk (D-021). Users supply their own external-service credentials
(D-022). Final evaluation-corpus selection remains deferred to M5 (D-017).

## 2. Immediate next steps

1. Define the chunk identity and source-aware splitting contract.
2. Persist deterministic derived chunks from a validated normalized snapshot.
3. Add the lexical retrieval baseline over the same chunks before embeddings.

## 3. Milestones

### M0 — Canonical project foundation

**Status:** Complete

**Goal:** Establish a compact source of truth before implementation.

Deliverables:

- approved project definition and MVP boundaries;
- accepted initial decision log;
- implementation plan;
- lean architecture document;
- initial evaluation plan;
- root `AGENTS.md` and `CLAUDE.md` instructions that direct Codex and Claude to
  the canonical documents and define startup, collaboration, privacy, review,
  and session-handoff protocols.

Exit criteria:

- no unresolved disagreement about the primary user, product behaviour, or MVP
  non-goals;
- proposed implementation choices are either accepted, rejected, or assigned a
  bounded technical spike;
- a new AI session can understand the project and identify the next task from
  the repository documents alone.

### M1 — Language foundation and repository skeleton

**Status:** Complete

**Goal:** Establish a tested project skeleton without adding RAG complexity.

Deliverables:

- concise initial README that explains the project, its current status, and
  points readers to the canonical documents without claiming unimplemented
  behaviour;
- Python environment and package structure;
- formatter, linter/type-checker decisions, and pytest setup;
- configuration and secret-handling approach;
- local bring-your-own credential configuration for GitHub, Voyage, and
  Anthropic without committing secrets;
- minimal CI for automated quality checks;
- small source-document domain model with tests.

Exit criteria:

- the project installs and tests from documented commands;
- no API keys or generated indexes are committed;
- the source-document model is independent of GitHub SDK response types.

### M2 — GitHub ingestion

**Status:** Complete

**Goal:** Build a repeatable corpus from one public GitHub repository.

Deliverables:

- configurable public GitHub repository reference;
- lightweight preflight with ready, indexing-required, and unsupported
  outcomes;
- development repository identification and validation, without assuming it
  will become the final evaluation corpus;
- pull request ingestion;
- related issue ingestion;
- commit-message ingestion;
- Markdown-document ingestion;
- normalization into the common source-document shape;
- local snapshot manifest and normalized `sources.jsonl` output;
- complete-supported-corpus enforcement without folder, date-window, or silent
  truncation modes;
- initial measured ingestion limits and clear rejection reasons;
- fixture-based tests for malformed, missing, and repeated source data;
- explicit full-index rebuild command.

Exit criteria:

- a sample repository produces a deterministic local corpus;
- preflight rejects an inaccessible or over-limit repository before embedding;
- repeated ingestion does not create duplicate source identities;
- incomplete ingestion is never reported as a ready snapshot;
- every normalized item preserves its type, original identifier, timestamp,
  and source URL.

### M3 — Retrieval foundation

**Status:** Active

**Goal:** Retrieve relevant evidence independently of answer generation.

Deliverables:

- source-aware chunking;
- lexical search baseline;
- embeddings pipeline;
- local persisted Chroma vector index;
- one embedding per chunk with source and position metadata;
- reusable per-repository snapshot loading across application restarts;
- `search_history` service and tool contract;
- retrieval tests against seed questions.

Exit criteria:

- both lexical and vector retrieval can be run on the same corpus;
- expected evidence appears in the measured top results for the seed set;
- retrieval results contain stable evidence IDs and provenance;
- a completed index can be reopened without re-embedding its repository.

### M4 — Bounded agentic RAG

**Goal:** Produce grounded answers through a small observable tool-calling loop.

Deliverables:

- `search_history` exposed as the agent's only tool;
- configured maximum number of tool calls;
- support for query refinement across bounded repeated calls;
- bounded Claude-model comparison and recorded selection;
- context construction from retrieved evidence;
- answer schema with evidence references;
- citation validation;
- insufficient-evidence behaviour;
- recorded tool calls, latency, and token usage required for evaluation.

Exit criteria:

- the agent cannot answer a rationale question without retrieved evidence;
- it can perform a useful second retrieval on at least one defined test case;
- citations refer only to evidence returned during the current run;
- unanswerable seed questions produce the expected abstention behaviour.

### M5 — Evaluation

**Goal:** Measure the system rather than relying on a polished demonstration.

Deliverables:

- selected and validated evaluation corpus or corpora, indexed separately;
- a reviewed question set sized after corpus validation, including answerable,
  semantic, ambiguous, and unanswerable cases;
- ground-truth source references;
- lexical-versus-vector retrieval comparison;
- single-pass-versus-agentic retrieval comparison where appropriate;
- citation and abstention review;
- latency and cost report;
- documented failure analysis.

Exit criteria:

- experiments are repeatable from documented commands;
- results include failures and limitations, not only successful examples;
- claims in the README are supported by recorded evaluation results.

### M6 — Demonstration and public documentation

**Goal:** Make the completed system understandable and runnable without
expanding its scope.

Deliverables:

- repository input and preflight states in the local UI;
- explicit indexing confirmation and phase-level progress;
- ready-index reuse and manual rebuild choice;
- Streamlit as the only user-facing interface;
- concise public README;
- architecture diagram;
- representative query examples;
- setup and demo instructions;
- final test and evaluation run.

Exit criteria:

- a reviewer can understand the problem from the README without reading the
  internal design documents;
- a reviewer can run the supported demo path from documented commands;
- all MVP success criteria in `project.md` are either satisfied or explicitly
  reported as unmet.

## 4. Proposed implementation order within a session

Work should proceed in vertical, testable increments. A typical increment is:

1. define or confirm the relevant contract;
2. add a focused failing test or evaluation case;
3. implement the smallest behaviour that satisfies it;
4. run relevant automated checks;
5. complete Codex review of the actual diff;
6. update this plan and any other canonical document whose source-of-truth
   content changed;
7. have Codex present the exact commit scope and proposed message;
8. after explicit user approval, create that checkpoint before beginning an
   unrelated slice. If the checkpoint is deliberately deferred, record that
   fact in the current state.

## 5. Current risks

### Sparse rationale

Many repositories record what changed but not why. Corpus selection must verify
that enough explicit rationale exists before the full evaluation set is built.

### False grounding

An answer can sound grounded while its citation is merely related rather than
supportive. Evaluation must review claim-level support, not just link validity.

### Scope expansion

Additional platforms, source types, authentication, deployment, and automation
can easily dominate the core project. The non-goals in `project.md` remain in
force unless superseded by an accepted decision.

### Technology adoption load

Python, GitHub ingestion, embeddings, vector storage, tool-calling, evaluation,
and UI are individually manageable but risky when introduced together. The
milestones deliberately isolate these concerns.

### Ingestion latency and API limits

Repository size, discussion volume, GitHub pagination, network latency, and
embedding throughput make initial indexing time variable. M2 must record phase
timings and API usage, establish tested limits, require a GitHub token for
practical non-trivial runs, and reject unsupported repositories before paid
embedding work begins.

### General-agent alternative

A coding agent with direct Git and GitHub access may answer some questions as
well as the indexed system, particularly on small repositories. The project
must report this limitation honestly and use evaluation to identify where the
specialized approach adds value.

## 6. First post-MVP improvement — PostgreSQL/pgvector migration

This improvement starts only after every MVP exit criterion is satisfied.

Goal:

- replace the Chroma storage implementation with PostgreSQL/pgvector;
- rebuild vectors from normalized source documents rather than treating the
  Chroma index as canonical data;
- design relational tables, constraints, and migrations;
- evaluate PostgreSQL full-text and vector retrieval options;
- run the same retrieval evaluation before and after the change;
- document differences in correctness, latency, complexity, and operations.

This work must use the existing storage boundary. If unrelated core or agent
code must be rewritten, the boundary design should be reviewed explicitly.

## 7. Deferred ideas

The following are intentionally not scheduled:

- Azure DevOps or GitLab adapters;
- PDFs, Slack, Confluence, or Jira;
- private repositories and permission-aware retrieval;
- MCP exposure;
- automated or incremental re-indexing;
- cloud deployment;
- cross-repository search;
- multiple agents;
- production observability infrastructure.

Deferred ideas become planned work only through an explicit accepted decision.

## 8. Session handoff checklist

At the end of a meaningful implementation session:

- update the current milestone, active task, and task status;
- record the last completed work, verification performed, and any blockers;
- leave one exact next action for the next session;
- record any newly accepted or rejected product/technical decision;
- update architecture only when an implemented contract or component boundary
  changed;
- update evaluation only when a dataset, metric, experiment, or result changed;
- do not copy chat transcripts into the documentation;
- leave the repository in a state where the next session can identify one clear
  next action.
