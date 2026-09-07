# RepoRationale — Project Definition

**Status:** Approved for MVP  
**Last updated:** 2026-09-04

This document is the source of truth for what RepoRationale is, who it is for,
what belongs in the MVP, and what success means. Technical implementation
details belong in `architecture.md`; the reasons behind accepted choices belong
in `decisions.md`; current progress belongs in `plan.md`.

## 1. One-sentence definition

RepoRationale is a small, evidence-grounded agentic RAG application that
retrieves the documented reasons behind technical changes in a GitHub
repository.

**Working tagline:** Ask why the code changed. Get the evidence behind it.

## 2. Target user

The primary user is a developer who needs to understand or modify an unfamiliar
part of an existing codebase.

This includes:

- a developer joining an established team;
- a maintainer working in old or unfamiliar code;
- a developer returning to a subsystem after a long time;
- a reviewer who needs historical context for a change.

RepoRationale is not aimed primarily at small, new repositories whose history
is already fully known to their authors.

### Where the product is most valuable

The product is intended for large, long-lived repositories and engineering
teams where relevant context is distributed across years of pull requests,
issues, commits, review discussions, and documentation. Its value increases
when no single developer knows the complete history and manual investigation
requires searching across many possible sources.

For a small repository with a short history, direct Git inspection or a general
coding agent may be simpler and equally effective. RepoRationale does not claim
that pre-indexed retrieval is the best solution at every repository size.

## 3. Problem

The current code shows what the system does, but it often does not show why a
particular implementation, restriction, or workaround exists. That rationale
may be scattered across pull requests, issues, commit messages, review
discussions, and design documents.

To recover it manually, a developer may need to:

1. locate the relevant lines or component;
2. inspect Git history and blame information;
3. find the introducing commit and pull request;
4. read related issues and review discussions;
5. search repository documentation;
6. ask a more experienced teammate if the written record is incomplete.

The cost is not merely historical curiosity. Missing context can lead a
developer to repeat a rejected approach, remove an intentional safeguard, or
reintroduce a previously fixed problem.

## 4. Primary job to be done

> When I need to understand or change unfamiliar code, help me recover the
> documented reasons and prior decisions behind it, so that I can make a more
> informed change without manually searching the repository history.

## 5. Questions the MVP should answer

Representative questions include:

- Why does this validation fail instead of falling back?
- Why was polling chosen instead of webhooks?
- What problem did this restriction solve?
- Which pull request introduced this behaviour?
- Was this alternative tried or rejected before?
- What documented constraints should be preserved?
- Is there a newer decision that supersedes the original one?
- Is there any documented rationale for this implementation?

The MVP focuses on questions about recorded history and decisions. It is not a
general-purpose code explanation or code-generation assistant.

## 6. Product behaviour

The user supplies one public GitHub `owner/repository`. A lightweight preflight
validates access, checks for a reusable local snapshot, and determines whether
the estimated complete supported corpus is within tested MVP limits. The UI
then reports that an index is ready, indexing is required, or the repository is
unsupported.

Indexing begins only after explicit confirmation, displays phase-level
progress, and persists its completed result locally. A compatible completed
snapshot is reused across page, application, and computer restarts. If the
repository has changed, the user can continue with the clearly identified
existing snapshot or request a manual full rebuild.

Once an index is ready, the user asks a natural-language question. The system
searches the active repository snapshot, may refine the search through a small
bounded tool-calling loop, and returns one of two outcomes:

### Evidence-grounded answer

The answer:

- addresses the question directly;
- distinguishes documented facts from synthesis;
- cites the source items that support its claims;
- links to the original pull request, issue, commit, or document;
- avoids claims that are not supported by retrieved evidence.

### Insufficient-evidence response

If the available sources identify a change but do not document its reason, the
system says so explicitly. It must not infer an author's intent from the code
alone and present that inference as fact.

## 7. Why indexed retrieval instead of direct AI exploration

A general AI coding agent with Git and GitHub access is a valid alternative. It
can inspect history and read candidate sources on demand. RepoRationale uses a
pre-indexed retrieval layer because the target setting contains more historical
material than is practical to inspect from scratch for every question.

The indexed approach is expected to provide:

- semantic retrieval when the question and historical source use different
  terminology;
- ranked evidence selection across many candidate pull requests, issues, and
  documents;
- lower repeated-search work when many questions target the same repository;
- predictable provenance and citation metadata;
- a constrained evidence set from which the answering model must work.

The bounded agent does not replace retrieval. The application first searches
with the user's exact question; after inspecting those results, the agent may
formulate a narrower refinement over the index. Whether this approach actually
outperforms lexical search or direct agentic exploration is an evaluation
question, not an assumption. Small repositories may not benefit enough to
justify indexing.

### Existing alternatives and positioning

RepoRationale does not claim to be the first way to recover change rationale.
Commercial context engines such as [Unblocked](https://docs.getunblocked.com/what-is-unblocked)
already answer engineering questions across code, pull requests, issues, and
team knowledge with source links. Research systems such as
[ARGUS](https://arxiv.org/abs/2604.10345) and the
[Kantara-based approach](https://arxiv.org/abs/2506.11005) also extract and
analyze rationale from software history, while general coding agents and native
GitHub search can investigate the same sources on demand.

Compared with a general coding agent that explores Git and GitHub on demand for
each question, RepoRationale prepares one reusable repository index and
constrains its agent to ranked evidence with explicit provenance, citation
validation, and abstention. Its intended value is more predictable, repeatable
investigation when many questions target a large, long-lived repository. Its
portfolio value is the inspectable implementation and reproducible evaluation
of that trade-off; it does not claim superiority until the comparison is
measured.

## 8. MVP scope

The MVP supports:

- user selection of one supported public GitHub repository at a time;
- preflight validation, tested ingestion limits, explicit indexing
  confirmation, and phase-level progress;
- merged and closed-unmerged pull request titles, descriptions, and supported
  conversation and review content;
- all open and closed GitHub issues;
- commit messages and source links;
- Markdown documentation stored in the repository;
- complete ingestion of those supported sources for every accepted repository;
- normalization of all supported sources into a common internal document form;
- source-aware chunking with one embedding per chunk;
- reusable local normalized snapshots and persistent vector indexes;
- a `search_history` tool over the indexed sources;
- a bounded agent that can make a small number of retrieval calls;
- natural-language answers with verifiable citations;
- explicit abstention when evidence is insufficient;
- manual, repeatable repository indexing;
- a small local demonstration interface;
- bring-your-own GitHub, Voyage, and Anthropic credentials through local secret
  configuration;
- automated tests and an evaluation harness.

## 9. Explicit non-goals

The MVP does not include:

- private-repository access or OAuth/permission-management flows;
- end-user model, chunking, or vector-store selection;
- Azure DevOps, GitLab, Bitbucket, or other Git platforms;
- Slack, Confluence, Jira, PDFs, or external document stores;
- cross-source access control or enterprise permissions;
- automatic synchronization, webhooks, or production re-indexing;
- folder-level, date-window, or silently truncated partial indexing;
- repositories beyond the measured MVP ingestion limits;
- multiple repositories queried as one knowledge base;
- background job queues or multi-user ingestion management;
- cloud deployment or production-scale infrastructure;
- MCP exposure;
- multiple collaborating agents or an open-ended autonomous agent;
- code generation, code review, or modification of repository content;
- fine-tuning an LLM;
- general questions whose answer comes only from the current source code rather
  than the repository's recorded history.

These exclusions are product boundaries, not missing tasks in the MVP plan.

## 10. Product principles

### Evidence before fluency

A less polished answer with correct evidence is preferable to a convincing but
unsupported answer.

### Abstention is a valid result

The system must distinguish "the repository does not document this" from "the
retrieval system failed to find it."

### Bounded agent behaviour

The agent may reformulate and repeat retrieval, but its tools and number of
iterations remain deliberately limited and observable.

### Source provenance is preserved

Every indexed item retains enough metadata to identify and link to its original
source.

### Small MVP, real evaluation

The project prioritizes a narrow end-to-end implementation and measured results
over a wide set of partially implemented integrations.

### Future portability without speculative implementation

GitHub-specific ingestion is kept separate from the retrieval core, and
provider-specific AI calls are isolated. No additional platform or provider is
implemented until there is a concrete reason to do so.

## 11. MVP success criteria

The MVP is complete when:

- a public GitHub repository can be indexed repeatably;
- preflight reuses a ready local snapshot, requests indexing when necessary,
  and rejects unsupported repositories before embedding;
- a completed local index remains usable after application restart;
- each indexed item retains valid provenance and a source URL;
- representative rationale questions retrieve the expected evidence;
- the bounded agent can refine a search when initial evidence is inadequate;
- answers cite only sources returned during retrieval;
- the system abstains on deliberately unanswerable questions;
- retrieval and answer behaviour are covered by automated tests;
- a documented evaluation compares at least lexical and vector retrieval;
- a reviewer can run a small local demo by following the README.

Numerical quality thresholds and the evaluation corpus will be defined in
`evaluation.md` before implementation of the final evaluation harness.

## 12. Future direction, not roadmap commitment

The internal source-document boundary should make future experiments with other
Git platforms or document types possible. The retrieval capability could also
be exposed to other AI assistants through an API or MCP-style interface.

Possible future sources include Azure DevOps, PDFs, or team knowledge systems.
They are not promised features and have no design or implementation work in the
MVP.

## 13. Known product limitations

- The system cannot recover rationale that was never recorded.
- A related citation does not automatically prove a generated claim; citation
  support must be evaluated.
- Small repositories may be served equally well by direct agentic search.
- A general coding agent with repository access is a valid alternative and an
  important comparison point.
- Repository history may contain outdated, conflicting, or superseded
  decisions.

The project should expose these limitations rather than conceal them.
