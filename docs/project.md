# RepoRationale: Project Definition

This document is the source of truth for what RepoRationale is, who it is for,
and what belongs in its first release. The release implements the project's MVP
(minimum viable product): the smallest complete version that solves the core
user problem and can be evaluated end to end.

## 1. One-sentence definition

RepoRationale is a small, evidence-grounded agentic RAG application that
retrieves the documented reasons behind technical changes in a GitHub
repository.

**Tagline:** Ask why the code changed. Get the evidence behind it.

## 2. Target user

The primary user is a developer who needs to understand or modify an unfamiliar
part of an existing codebase.

This includes:

- a developer joining an established team;
- a maintainer working in old or unfamiliar code;
- a developer returning to a subsystem after a long time;
- a reviewer who needs historical context for a change.

The product is most valuable in large, long-lived repositories and engineering
teams where relevant context is distributed across years of pull requests,
issues, commits, review discussions, and documentation. Its value increases
when no single developer knows the complete history and manual investigation
requires searching across many possible sources.

## 3. Problem

When developers read an unfamiliar codebase, the code usually shows what the
system does but not why a particular implementation, restriction, or workaround
exists. That rationale may be scattered across pull requests, issues, commit
messages, review discussions, and design documents.

To recover it manually, a developer may need to:

1. locate the relevant lines or component;
2. inspect Git history and blame information;
3. find the introducing commit and pull request;
4. read related issues and review discussions;
5. search repository documentation;
6. ask a more experienced teammate if the written record is incomplete.

Missing this context can lead a developer to repeat a rejected approach, remove
an intentional safeguard, or reintroduce a previously fixed problem.

RepoRationale's core job is to recover the documented reasons and prior
decisions behind unfamiliar code, so that a developer can make a more informed
change without manually searching the repository's history.

## 4. Example questions

Representative questions include:

- Why does this validation fail instead of falling back?
- Why was polling chosen instead of webhooks?
- What problem did this restriction solve?
- Which pull request introduced this behaviour?
- Was this alternative tried or rejected before?
- What documented constraints should be preserved?
- Is there a newer decision that supersedes the original one?
- Is there any documented rationale for this implementation?

RepoRationale may also answer broader repository questions when the supported
history contains sufficient evidence. The boundary is evidentiary: it is not a
general-purpose assistant that explains the current source code or answers from
unsupported model knowledge alone.

## 5. Why a dedicated evidence workflow

A general coding agent with repository access can investigate history on demand
and may be the simplest option for a one-off question or a small repository.
RepoRationale is not designed on the assumption that it will always search
better. It provides a dedicated workflow in which the evidence available to the
model, its search behaviour, citations, abstentions, and failures are bounded
and inspectable.

The workflow makes several guarantees explicit:

- retrieval happens before the model can answer;
- every search and evidence-sufficiency assessment is recorded;
- the number of agent-requested refinements is limited;
- citations must resolve to evidence retrieved during the current run;
- insufficient evidence is a valid structured result rather than an invitation
  to guess.

These controls do not make hallucinations impossible or prove that every cited
claim is correct. They create a stable evidence boundary around the model and
make retrieval failures, grounding failures, and answer failures easier to
distinguish, test, and evaluate.

The reusable index supports this workflow by providing one consistent evidence
corpus, semantic retrieval across historical source types, and less repeated
exploration when many questions target the same repository. It introduces an
up-front indexing cost and has not been shown to outperform direct agentic
exploration.

### Existing alternatives and positioning

RepoRationale does not claim to be the first way to recover change rationale.
Commercial context engines such as
[Unblocked](https://docs.getunblocked.com/what-is-unblocked) already answer
engineering questions across code, pull requests, issues, and team knowledge
with source links. Research systems such as
[ARGUS](https://arxiv.org/abs/2604.10345) and the
[Kantara-based approach](https://arxiv.org/abs/2506.11005) also analyze
rationale from software history, while general coding agents and native GitHub
search can investigate the same sources on demand.

RepoRationale's contribution is deliberately narrower: an inspectable
implementation and reproducible evaluation of a bounded, citation-validated
evidence workflow built on a reusable index. It does not claim superiority
beyond what the recorded [evaluation](evaluation.md) supports.

## 6. Product behaviour

The user supplies one public GitHub `owner/repository`. A lightweight preflight
validates access, checks for a reusable local snapshot, and determines whether
the estimated complete supported corpus is within tested limits. The UI then
reports that an index is ready, indexing is required, or the repository is
unsupported.

Temporary external failures are reported separately from unsupported
repositories so the user can retry without receiving a false compatibility
result.

Indexing begins only after explicit confirmation, displays phase-level
progress, and persists its completed result locally. A compatible completed
snapshot is reused across page, application, and computer restarts. If the
repository has changed, the user can continue with the clearly identified
existing snapshot or request a manual full rebuild.

Once an index is ready, the user asks a natural-language question. The
application searches the active repository snapshot with that exact question.
After inspecting the results, the bounded agent may request a small number of
narrower searches before producing one of two outcomes.

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

### Follow-up questions

During the current investigation, the user may ask bounded follow-up questions
that refer to earlier turns. Context is limited to the active repository
snapshot and is not persisted; it is cleared by a refresh, restart, rebuild, or
repository change. Prior answers may resolve references, but they never count
as evidence. Every answer is grounded only in evidence retrieved for that run.

The complete indexing and question-answering workflows are described in the
[architecture](architecture.md).

<p align="center">
  <img
    src="assets/screenshots/grounded-answer-gson.png"
    alt="RepoRationale answering a question about Gson with an expanded source citation"
    width="900"
  />
</p>

<p align="center"><em>A grounded answer from a reusable Gson snapshot, with
the supporting repository evidence available for direct inspection.</em></p>

## 7. First-release scope

The first release supports:

- one public GitHub repository at a time;
- complete ingestion of supported pull request, issue, commit-message, and
  Markdown-document history for every accepted repository;
- preflight validation, tested ingestion limits, explicit indexing
  confirmation, and phase-level progress;
- reusable local snapshots, persistent indexes, and manual full rebuilds;
- semantic retrieval and a bounded number of agent-requested refinements;
- natural-language answers with verifiable source links;
- explicit abstention when evidence is insufficient;
- bounded follow-up questions within one repository investigation;
- a local Streamlit demonstration interface;
- user-supplied GitHub, Voyage, and Anthropic credentials;
- automated tests and a reproducible evaluation harness.

The first release deliberately excludes private-repository authentication,
additional Git platforms or external knowledge systems, multiple repositories
queried as one corpus, automatic synchronization, partial or silently
truncated indexing, repositories beyond the tested limits, durable conversation
history, cloud or multi-user deployment, open-ended or collaborating agents,
MCP exposure, and code-generation or repository-modification capabilities.

These are product boundaries, not unfinished first-release tasks. Technical
components and provider responsibilities are documented in the
[architecture](architecture.md), while the reasoning behind the boundaries is
recorded in the [decision log](decisions.md).

## 8. Product principles

### Evidence before fluency

A less polished answer with correct evidence is preferable to a convincing but
unsupported answer.

### Abstention is a valid result

The system must not turn either missing documentation or unsuccessful retrieval
into a fabricated rationale. When the retrieved evidence is insufficient, it
reports that limitation explicitly without claiming that the repository
contains no answer.

### Bounded agent behaviour

The agent may reformulate and repeat retrieval, but its tools and number of
iterations remain deliberately limited and observable.

### Source provenance is preserved

Every indexed item retains enough metadata to identify and link to its original
source.

### Small product, real evaluation

The project prioritizes a narrow end-to-end implementation and measured results
over a wide set of partially implemented integrations.

### Future portability without speculative implementation

Source-specific ingestion is kept separate from the retrieval core, and
provider-specific AI calls are isolated. Additional platforms and providers are
not implemented without a concrete reason.

## 9. Future direction

Later versions could explore:

- raising or adapting repository limits after further performance measurement,
  while preserving the complete-corpus-or-reject guarantee;
- replacing the local vector store with PostgreSQL and pgvector;
- using GitHub GraphQL or incremental synchronization to reduce collection
  work;
- indexing additional GitHub content such as Discussions;
- supporting other platforms and sources, including Azure DevOps, Slack, and
  PDFs;
- persisting investigation and conversation history across sessions;
- exposing retrieval through an API or MCP-compatible interface.

These are possible extensions, not roadmap commitments. They require explicit
product and technical decisions before implementation.

## 10. Known product limitations

- The system cannot recover rationale that was never recorded.
- A related citation does not automatically prove a generated claim; citation
  support must still be evaluated.
- Direct repository inspection or a general coding agent may be equally
  effective on small repositories.
- Repository history may contain outdated, conflicting, or superseded
  decisions.
- Initial indexing time and cost grow with repository history and external API
  performance.
- The first release is a local, single-user demonstration rather than a
  production service.

The project exposes these limitations rather than concealing them. Their
measured impact is reported in the [evaluation](evaluation.md).
