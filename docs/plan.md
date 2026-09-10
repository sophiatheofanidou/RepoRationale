# RepoRationale: Implementation Plan and Completed Milestones

RepoRationale has completed its first-release implementation plan and is ready
for release. This document records how the project progressed from product
definition to an evaluated Streamlit application. It is a completed delivery
record, not an active task tracker.

All implementation milestones are closed. Final publication actions, such as
adding the MIT license, creating the release commit and tag, and publishing the
GitHub release, are release administration. They do not keep the implementation
plan open and are not tracked here.

## 1. Milestone overview

| Milestone | Purpose | Final state |
| --- | --- | --- |
| M0 | Establish the project definition and sources of truth | Complete |
| M1 | Create the tested Python foundation | Complete |
| M2 | Build a complete, repeatable GitHub corpus | Complete |
| M3 | Add independently testable retrieval | Complete |
| M4 | Build the bounded evidence-grounded answering loop | Complete |
| M5 | Evaluate retrieval, answers, cost, latency, and failures | Complete |
| M6 | Deliver the Streamlit demonstration and public documentation | Complete |

The milestones were intentionally sequential. Each one established the
contracts, implementation, or evidence required by the next.

## 2. Completed milestones

### M0: Canonical project foundation

**Goal.** Establish a compact source of truth before implementation.

**Delivered.**

- A product definition covering the problem, target user, first-release scope,
  non-goals, product principles, and limitations.
- A decision log separating accepted choices from rejected or deferred
  alternatives.
- An initial architecture and evaluation approach.
- Shared Codex and Claude working guides defining authority, privacy, review,
  verification, Git authorship, and handoff responsibilities.
- A milestone plan that split the project into bounded, testable increments.

**Completion evidence.** Product scope and ownership were clear before coding
began. A new session could restore the project's purpose, accepted boundaries,
and next implementation step from the repository documents rather than chat
history.

### M1: Language foundation and repository skeleton

**Goal.** Create a tested project foundation without introducing retrieval or
agent complexity prematurely.

**Delivered.**

- Python 3.13 package structure under `src/`.
- Reproducible dependency management with uv and a committed lockfile.
- pytest, Ruff, formatting, strict mypy, and a minimal GitHub Actions workflow.
- Environment-based configuration and a safe bring-your-own-credentials
  pattern for GitHub, Voyage, and Anthropic.
- Git ignore rules covering credentials, private context, generated snapshots,
  indexes, traces, caches, and local environments.
- Initial provider-independent domain models with focused tests.

**Completion evidence.** The project installed from its locked dependencies,
quality checks ran from documented commands, secrets and generated data stayed
outside version control, and the core domain did not depend on GitHub SDK
response objects.

### M2: GitHub ingestion

**Goal.** Build a deterministic, complete corpus from one selected public
GitHub repository.

**Delivered.**

- Repository identity parsing, lookup, canonicalization, and revision
  resolution.
- Preflight outcomes for a ready snapshot, required indexing, unsupported
  repository, and temporary external failure.
- Collection of issues, pull requests, review summaries, discussion comments,
  commit messages, and Markdown documentation.
- Normalization into stable citation-addressable source documents.
- Deterministic local snapshot manifests and `sources.jsonl` artifacts.
- Repository-wide collection endpoints and bounded per-pull-request
  concurrency where measurements justified them.
- Measured admission limits and complete-corpus enforcement, with explicit
  rejection instead of partial indexing.
- Rebuild behaviour that protects an existing ready snapshot until the new
  result has completed successfully.
- Fixture-based tests for pagination, malformed responses, missing fields,
  repeated data, renamed repositories, and failure handling.

**Completion evidence.** Supported repositories produced complete,
deterministic local corpora with stable source identities and provenance.
Inaccessible or oversized repositories were rejected before embedding, and an
interrupted build could not be mistaken for a ready snapshot.

### M3: Retrieval foundation

**Goal.** Retrieve relevant historical evidence independently of answer
generation.

**Delivered.**

- Source-aware deterministic chunking with preserved provenance.
- An offline BM25 lexical baseline.
- Voyage document and query embeddings.
- A locally persisted Chroma vector index.
- One embedding per source-aware chunk, with stable source and position
  metadata.
- Shared ranked-evidence results across lexical and vector retrieval.
- Snapshot compatibility checks and ready-index reopening without repository
  re-embedding.
- Retrieval, persistence, digest, record-count, and provenance tests.

**Completion evidence.** BM25 and vector retrieval ran over the same persisted
chunks, expected evidence could be measured independently of generation, and a
completed index reopened with the same records and no new document embeddings.

### M4: Bounded agentic RAG

**Goal.** Produce grounded answers through a small, observable search and
answer loop.

**Delivered.**

- An application-owned semantic search using the exact user question before
  the answering model is invoked.
- A separate model-requested refinement action with a maximum of three searches
  per question.
- Model context constructed only from retrieved evidence.
- Structured outcomes for a cited answer or explicit insufficient evidence.
- Deterministic rejection of malformed citations and citations to evidence not
  returned during the current run.
- A development comparison of Claude models and selection of Claude Opus 5 for
  the first release.
- Session-scoped conversational follow-ups that help resolve later questions
  without treating previous generated answers as evidence.
- Recorded searches, latency, token usage, cost, and failures needed for
  evaluation.

**Completion evidence.** The model could not answer before retrieval, a bounded
refinement recovered evidence missed by an earlier search, invalid citations
were rejected, unsupported questions produced the abstention outcome, and each
follow-up answer remained grounded in evidence retrieved during its own turn.

### M5: Evaluation

**Goal.** Measure the system and its failure modes rather than relying on a
polished demonstration.

**Delivered.**

- A staged corpus-selection process using familiar, development, external, and
  oversized repositories for different evaluation questions.
- Manually reviewed development and held-out question sets with predeclared
  expected outcomes and evidence.
- Chunk-size calibration using retrieval quality, passage containment, and
  chunk volume.
- BM25 and Voyage/Chroma comparisons over the same questions and chunks.
- A controlled Claude model comparison.
- A one-shot, eight-question held-out Gson evaluation with six answerable and
  two unsupported-premise cases.
- Separate review of outcome accuracy, claim support, citation completeness,
  expected-source recovery, latency, usage, and cost.
- Post-evaluation diagnostics that remained separate from the published
  held-out result.
- Controlled document-embedding and GitHub-collection concurrency experiments.
- A final cold-index operating measurement and an explicit limitations section.

**Completion evidence.** The evaluation reports successful, mixed, and failed
results; explains why each metric was selected; separates deterministic checks
from manual judgment; and limits its claims to what the recorded experiments
support. The full method and results are in the
[evaluation](evaluation.md).

### M6: Demonstration and public documentation

**Goal.** Make the completed product understandable and usable without
expanding its accepted scope.

**Delivered.**

- A Streamlit interface kept as a thin presentation layer over the existing
  application workflows.
- Repository input and distinct ready, indexing-required, unsupported, and
  temporarily-unavailable states.
- Explicit indexing confirmation, cancel behaviour, and phase-level progress.
- Ready-snapshot reuse, manual rebuild confirmation, and protection of the
  working snapshot during rebuild.
- Grounded answers with expandable evidence and links to original sources.
- Explicit insufficient-evidence responses that keep related but inadequate
  sources visibly separate.
- Bounded contextual follow-ups within one browser session and repository
  snapshot.
- A consistent application theme and focused Streamlit smoke tests.
- Release screenshots covering the primary path and alternative outcomes.
- A concise [product walkthrough](product-walkthrough.md).
- Release-facing revisions of the [project definition](project.md),
  [evaluation](evaluation.md), and this completed plan.

**Completion evidence.** A reviewer can follow the complete supported path from
repository selection through indexing and grounded questioning, inspect the
original evidence, observe abstention and failure states, and understand the
workflow from the public screenshots. The implementation is covered by focused
application tests and the consolidated verification below.

## 3. Final verification

The completed implementation passes the consolidated local quality checks:

| Check | Result |
| --- | --- |
| `uv run pytest` | **741 passed** |
| `uv run ruff check .` | **Passed** |
| `uv run ruff format --check .` | **98 files already formatted** |
| `uv run mypy` | **Passed: 89 source files** |

The pytest run had no failures. Its warnings were limited to Chroma's legacy
embedding-function configuration and an inability to write the local pytest
cache in the execution environment. Neither affected the test outcomes.

The repository also contains a
[GitHub Actions workflow](../.github/workflows/ci.yml) that reproduces the
locked environment and runs linting, formatting, type checking, and tests.
Live provider measurements are not repeated during every local verification
because they incur cost and depend on mutable GitHub history and network
latency. Their recorded evidence remains in the
[evaluation](evaluation.md).

## 4. Plan closure

The first-release implementation plan is complete. No known product, code,
evaluation, or user-interface blocker remains within the accepted scope.

The remaining license, repository-hygiene, commit, tag, push, and GitHub-release
actions package and publish the completed work. They do not change the
milestone outcomes recorded here.

Future directions are summarized in the
[project definition](project.md#9-future-direction), and the rationale for
accepted or deferred choices remains in the [decision log](decisions.md). Any
post-release implementation should start from a new bounded plan rather than
reopening these completed milestones.
