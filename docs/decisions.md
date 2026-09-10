# RepoRationale: Decision Log

This file records important product and engineering decisions and the reasons
behind them. It is intentionally a single lightweight log rather than a folder
of individual ADR files.

Statuses:

- **Proposed:** under active consideration; implementation must not assume it is
  final.
- **Accepted:** part of the current source of truth.
- **Rejected:** considered and deliberately not selected.
- **Superseded:** previously accepted but replaced by a later decision.

When a decision changes, preserve the old entry, mark it as superseded, and
link to the replacement.

Current status notes:

- Decisions are accepted unless their entry says otherwise.
- [D-017](#d-017-defer-final-evaluation-corpus-selection) and
  [D-026](#d-026-defer-final-corpus-selection-with-size-tiered-candidates) were
  superseded when [D-029](#d-029-select-googlegson-as-the-main-evaluation-corpus)
  selected the evaluation corpus.
- [D-004](#d-004-make-bounded-tool-calling-part-of-the-core-mvp) remains
  accepted except for its original model-generated first search, which
  [D-030](#d-030-use-the-exact-user-question-for-the-first-semantic-search)
  replaced with an application-owned exact-question search.

## D-001: Focus on documented technical rationale

- **Status:** Accepted
- **Date:** 2026-08-31

### Context

General repository chat and code explanation are already broad capabilities of
AI coding assistants. The project needs a narrow, understandable purpose and a
strong evidence contract.

### Decision

RepoRationale will focus on retrieving the documented reasons, alternatives,
constraints, and historical context behind technical changes. It will not be a
general code assistant.

### Consequences

The primary corpus is repository history and decision-bearing documentation.
The system must be able to say that a reason was not documented.

## D-002: Limit the MVP to one public GitHub repository

- **Status:** Accepted
- **Date:** 2026-08-31

### Context

Private repositories, multiple Git platforms, and enterprise authentication
would add substantial complexity without being necessary to demonstrate the
core retrieval and agent behaviour.

### Decision

The MVP will index one public GitHub repository at a time.

### Consequences

Azure DevOps, GitLab, private-repository authentication, and cross-repository
search remain outside the MVP.

## D-003: Use repository-native decision sources only in the MVP

- **Status:** Accepted
- **Date:** 2026-08-31

### Context

Technical rationale can exist in many systems, but Slack, PDFs, Confluence, and
similar sources introduce parsing, synchronization, permission, and provenance
problems that would dominate a small, bounded application.

### Decision

The GitHub MVP corpus will consist of:

- all open and closed GitHub issues;
- all merged pull requests;
- all pull requests closed without merging;
- issue titles, descriptions, and all non-empty comments;
- pull-request titles, descriptions, general comments, non-empty review
  summaries, inline review comments, and replies;
- commit messages; and
- Markdown documentation stored in the repository.

Open pull requests are excluded because they represent active, changing work.
Standalone GitHub Discussions are excluded by D-025. Preserve platform-native
states and reasons when available, together with relevant lifecycle timestamps
and merge metadata. Do not translate `closed` into rejected, abandoned,
superseded, or completed unless retrieved textual evidence supports that
interpretation.

Store direct parent relationships, closing-issue links, duplicate links, review
states, and resolved or outdated review-thread state as metadata when
available. Do not turn reactions, assignments, label changes, review requests,
CI checks, branch updates, merge-queue activity, or other non-textual timeline
events into searchable documents. Ingest the current available version of
non-empty textual comments regardless of author type, preserving author type
as metadata; do not attempt to reconstruct edit history or unavailable deleted
content.

### Consequences

External document and communication systems are mentioned only as possible
future directions. No MVP architecture work may depend on implementing them.
A future platform adapter defines its own supported native source types, states,
and ingestion rules without changing the shared retrieval, citation, or agent
contracts.

## D-004: Make bounded tool-calling part of the core MVP

- **Status:** Accepted
- **Date:** 2026-08-31
- **Amended:** 2026-09-07

The original model-generated first-search design is superseded by D-030. The
structured refinement, answer, abstention, search-budget, and validation
contracts remain accepted.

### Context

A retrieve-once-and-answer pipeline cannot recover when the first results lack
the necessary evidence. Some historical questions require one or two narrower
searches, but an open-ended agent would make behaviour, cost, and evaluation
hard to bound.

The first implementation also asked Claude to combine tool calls with
free-form JSON text for refinements and final outcomes. Live development runs
showed that models could omit, blank, or inconsistently shape those text
blocks. More prescriptive prompting could reduce the failures but could not
make the response contract deterministic. Schema-validated tools provided a
stronger boundary. The model comparison and observed failures are reported in
the [evaluation](evaluation.md).

### Decision

After the application-owned first search established by D-030, Claude may call
exactly one schema-validated action per turn:

- `refine_search(query, missing_information)` to request a narrower semantic
  search and state what evidence is still missing;
- `provide_answer(answer, citations)` to return a grounded answer; or
- `report_insufficient_evidence(explanation)` to abstain.

The application forces a single eligible tool call and disables extended
thinking for these structured turns. `missing_information` is required and
non-blank. The agent may use at most three searches in total, including the
application-owned first search. It may stop earlier when the evidence is
sufficient; after the third search it must answer from the retrieved evidence
or abstain.

### Consequences

The loop is bounded, observable, and testable. Searches, evidence-sufficiency
assessments, and refinement reasons are recorded for evaluation. The model
receives no filesystem, shell, code-modification, arbitrary external, or
structured filtering capability. Citation validation and provider-neutral
outcome contracts remain application responsibilities, while Anthropic-specific
request and response shapes stay behind the provider boundary in D-007.

## D-005: Require evidence-backed answers and explicit abstention

- **Status:** Accepted
- **Date:** 2026-08-31

### Context

An LLM can generate a plausible explanation even when the repository does not
record the author's reason. Merely attaching a related citation does not make
such an explanation grounded.

### Decision

The system may make factual claims about rationale only from evidence returned
by its retrieval tool. The agent returns a structured result with either
`answered` or `insufficient_evidence` status, answer text, and numbered
citations. Each citation maps an answer reference to a stable evidence ID
returned during the current agent run and includes the source title, direct
URL, and supporting excerpt. An insufficient-evidence result contains no
unsupported rationale claims.

### Consequences

The Streamlit UI renders the answer and citations without exposing the internal
serialization format. Citation validation must reject references to evidence
that was not returned during the current run. Citation correctness,
claim-support quality, and abstention behaviour are first-class evaluation
targets, not UI polish.

## D-006: Keep indexing manual in the MVP

- **Status:** Accepted
- **Date:** 2026-08-31

### Context

Automated re-indexing has no coherent lifecycle until the application has a
deployed persistent store. A GitHub Action that creates a temporary or committed
vector index would add machinery without solving the local application's
freshness problem.

### Decision

The MVP will expose an explicit, repeatable indexing operation and a full
rebuild path. GitHub Actions will be used for tests and quality checks, not for
production-style index synchronization. After successful preflight and user
confirmation, indexing runs synchronously in the local application with
phase-level progress. The active operation must complete before that UI session
can answer questions from the new snapshot.

### Consequences

Only a fully completed snapshot becomes ready, as defined by D-020. An
interrupted or failed build is not resumable in the MVP and requires a new full
run; any previously ready snapshot remains separately reusable. Background
workers, durable job queues, incremental indexing, webhooks, and scheduled
refresh remain post-MVP topics.

## D-007: Design one narrow portability boundary

- **Status:** Accepted
- **Date:** 2026-08-31
- **Amended:** 2026-09-10

### Context

The project may later experiment with Azure DevOps, other documents, or other
AI tools, but implementing generalized connector and provider frameworks now
would be speculative overengineering.

### Decision

Supported sources will be normalized into one common internal source-document
shape. AI provider calls will be isolated from the retrieval and domain logic.
The sole MVP source adapter will collect GitHub data through the versioned REST
API with explicit pagination and rate-limit handling; it will not maintain a
parallel GraphQL ingestion path. No additional adapters or providers will be
implemented in the MVP.

Within GitHub collection, run the repository-wide pull-request review-comment
request concurrently with the independent per-pull-request review-summary
collection after the relevant pull-request numbers are known. Preserve
deterministic output ordering, the shared request budget, and all-or-nothing
failure behaviour.

### Consequences

The core must not depend directly on GitHub response objects or provider SDK
types, but it also must not introduce unused plugin infrastructure. The scoped
two-operation overlap was retained only after a controlled comparison produced
the same ordered source artifact with lower collection time. Request
concurrency remains bounded so completeness, ordering, failure behaviour, and
GitHub rate limits stay observable. The comparison is recorded in the
[evaluation](evaluation.md#82-github-collection-concurrency).

## D-008: Use Python as the implementation language

- **Status:** Accepted
- **Date:** 2026-08-31

### Context

Python has a mature ecosystem for document processing, embeddings, vector
retrieval, model SDKs, and evaluation. TypeScript remains technically viable,
but using two implementation languages would add complexity without serving the
bounded MVP.

### Decision

Implement the project in Python.

### Consequences

The implementation will introduce unfamiliar components incrementally. The
language decision does not select a web framework, UI, vector store, package
manager, or model provider; those remain separate decisions.

## D-009: Use Chroma as the MVP vector store

- **Status:** Accepted
- **Date:** 2026-08-31
- **Amended:** 2026-09-10

### Context

The MVP needs persisted vector retrieval but does not require a deployed,
multi-user relational database. Introducing PostgreSQL, pgvector, schema
migrations, and database operations at the same time as the first Python RAG
implementation would increase the initial scope substantially.

### Decision

Use a local persisted Chroma vector store for the MVP. Access it through a small
project-owned storage/retrieval boundary rather than exposing Chroma types
throughout the core application.

### Consequences

The vector index is derived data and must be rebuildable from normalized source
documents. PostgreSQL and pgvector remain outside the MVP and are one possible
future storage direction; no post-release implementation is currently
prioritized. The demonstration interface remains a separate decision.

## D-010: Implement the core RAG and agent loop without a large framework

- **Status:** Accepted
- **Date:** 2026-08-31

### Context

Retrieval, context construction, tool-calling, citations, and bounded agent
control are core system behaviours. A large framework may hide those mechanisms
and make them harder to inspect and test. The MVP has one retrieval tool and a
small fixed iteration limit, so it does not require a graph-based orchestration
runtime.

### Decision

Use the selected model provider's official SDK and a small project-owned bounded
agent loop. LangChain, LangGraph, LlamaIndex, and similar orchestration
frameworks will not be used in the MVP. Focused libraries for GitHub access,
storage, validation, and testing remain allowed.

### Consequences

The project owns a small amount of orchestration code and must test loop
termination, tool errors, evidence handling, citations, and abstention. Model
provider calls remain isolated behind the boundary established in D-007. This
decision should be revisited only if the workflow grows beyond one bounded
retrieval tool.

## D-011: Maintain a lean canonical documentation set

- **Status:** Accepted
- **Date:** 2026-08-31

### Context

A large collection of detailed design documents can become burdensome to read,
keep consistent, and maintain. RepoRationale still needs persistent context that
can be shared across new development sessions and tools.

### Decision

The canonical sources of truth are the [project definition](project.md),
[architecture](architecture.md), [implementation plan](plan.md),
[evaluation](evaluation.md), and this decision log. Tool-specific instruction
files may point to these documents but must not duplicate their content.

### Consequences

Supporting presentation documents may be added when they serve a distinct
reader need without becoming a new source of truth. The
[product walkthrough](product-walkthrough.md), for example, explains the user
experience through screenshots while deferring product, technical, and
evaluation claims to the canonical documents. Brainstorming and external
reference material are not sources of truth.

## D-012: Use pre-indexed retrieval as the primary evidence-access path

- **Status:** Accepted
- **Date:** 2026-08-31
- **Amended:** 2026-09-10

### Context

A general AI coding agent with Git and GitHub tools can investigate repository
history directly. This is likely sufficient for small repositories. In a large,
long-lived repository, however, repeatedly exploring many pull requests,
issues, commits, and documents may require numerous sequential tool calls and
may miss semantically related sources that use different terminology.

### Decision

RepoRationale will pre-process supported sources into a searchable index and
rank evidence for each question. The application-owned first search and later
model-requested refinements both use the `search_history` application service.
The model can request only the bounded `refine_search` action and cannot freely
explore Git or GitHub.

The MVP product path is vector RAG: `search_history` will use Voyage embeddings
and the persisted Chroma vector index. During evaluation, a lean offline BM25
lexical baseline will run independently over the same persisted chunks and
question set to measure whether and where semantic retrieval adds value. The
baseline will not select or replace the product retriever, act as a prefilter,
or create a parallel or hybrid production retrieval path.

### Consequences

The system requires an indexing lifecycle and retrieval-quality evaluation.
The lexical baseline needs only enough implementation to support a fair,
repeatable offline comparison; it is not a product feature. Its results may
identify vector-retrieval limitations but do not silently change the accepted
MVP architecture. Direct agentic exploration remains a valid broader baseline.
The project must not claim that indexed or semantic retrieval is superior until
measured, and it must acknowledge that its value is primarily expected in
larger repositories and repeated-use settings.

## D-013: Use Streamlit as the only MVP user-facing interface

- **Status:** Accepted
- **Date:** 2026-09-01
### Context

The completed project needs a small visual interface, but it does not require a
shared HTTP service, a separate frontend deployment, or two user-facing ways to
perform the same operations. A polished CLI would add argument parsing, output
formatting, documentation, and tests without serving a distinct MVP user need.

### Decision

Use Streamlit as the MVP's only user-facing interface. Keep ingestion,
retrieval, agent behaviour, and evaluation in a UI-independent Python core.
Use internal Python modules or development scripts for repeatable indexing and
evaluation. Do not build a separate CLI, FastAPI service, or frontend in the
MVP.

Run the local product as a modular monolith: Streamlit is a thin presentation
layer that calls application services directly in the same Python runtime.
Application workflows, domain models, retrieval and agent logic, and
infrastructure adapters remain separate modules with explicit dependency
boundaries. Streamlit must not call GitHub, Voyage, Anthropic, Chroma, or local
snapshot APIs directly.

### Consequences

The Streamlit layer must contain presentation and interaction logic only. It
must call the same application services used by tests and internal scripts.
Sharing one runtime does not permit UI and infrastructure responsibilities to
be mixed. A separate HTTP backend, independent service deployment, or job
queue can be reconsidered only when a real second client, remote deployment,
multi-user, scaling, or durable background-processing need exists.
FastAPI or a CLI can be reconsidered only if a concrete external-client,
automation, or multi-user service need appears.

## D-014: Make PostgreSQL/pgvector the first post-MVP improvement

- **Status:** Superseded by
  [D-032](#d-032-defer-selection-of-the-first-post-release-implementation)
- **Date:** 2026-08-31

### Context

Chroma keeps the MVP focused, while PostgreSQL with pgvector offers a valuable
next step toward relational metadata, SQL, full-text search, and a more
production-oriented persistence model. The change should test whether the
storage boundary is real rather than merely theoretical.

### Decision

After the MVP is complete, replace the Chroma storage implementation with a
PostgreSQL/pgvector implementation and rebuild the index from normalized source
documents. This is the first prioritized post-MVP improvement, not an MVP exit
criterion.

### Consequences

The migration will require schema design, migrations, PostgreSQL lifecycle,
vector indexing, and retrieval comparison. Chroma-specific assumptions must not
leak into the domain or agent layers. The same evaluation queries will be run
before and after the migration to compare correctness, latency, and operational
complexity.

## D-015: Use Anthropic Claude and select the exact model through evaluation

- **Status:** Accepted
- **Date:** 2026-09-01
- **Amended:** 2026-09-07

### Context

The generation model must interpret rationale questions, refine a query when
necessary after the application-owned first search, assess evidence
sufficiency, and produce a cited answer or abstain. Different Claude tiers
trade off tool-use reliability, answer quality, latency, and cost, so the exact
model required project-specific evidence.

A fixed development comparison tested Claude Haiku 4.5, Sonnet 5, and Opus 5
against the same four reviewed cases after the structured tool-calling contract
in D-004 was established. Haiku and Opus completed all hard workflow
requirements; Sonnet produced citation and grounding failures that the
application correctly rejected. Opus was the only candidate that also matched
the expected outcome and evidence in every case. Later diagnostics reproduced
Sonnet's grounding failures and found unsupported elaboration in a Haiku
answer, so they did not justify reopening the selection. The method, measured
results, and limitations are recorded in the [evaluation](evaluation.md).

### Decision

Use Anthropic's official Python SDK and keep the Claude model identifier
configurable for maintainers and tests; do not expose model selection in the
MVP UI. `claude-opus-5` is the selected MVP answering model, chosen because it
was the only candidate with no hard requirement failure that also matched the
predeclared expected outcome and expected evidence on all four comparison
cases. Isolate Anthropic request and response types behind the provider
boundary established in D-007.

Claude Haiku 4.5 remains a meaningfully cheaper alternative, but is not the
selected model because it missed predeclared decision evidence in the original
comparison and later added unsupported elaborations in the exact-query probe.
Claude Sonnet 5 remains excluded because its two hard citation/grounding
failures recurred with the exact first query.

### Consequences

The MVP requires an Anthropic API key and incurs usage-based generation cost.
The application composition defaults to `claude-opus-5`, while the identifier
remains configurable at the provider boundary for maintainers and tests.
Model selection is not exposed in the UI.

This was a small, project-specific development comparison, not a statistical
study. It selects the model for this bounded agent loop and evidence set; it
does not establish that Opus is universally superior to Haiku or Sonnet.
Another Claude model can still be substituted later without changing
retrieval or domain logic.

## D-016: Use Voyage 4 for embeddings

- **Status:** Accepted
- **Date:** 2026-09-01
- **Amended:** 2026-09-10

### Context

The indexed corpus contains mixed natural-language material: pull-request and
issue discussions, commit messages, and Markdown documentation. The embedding
model therefore needs general-purpose semantic retrieval rather than a model
specialized primarily for source-code retrieval.

### Decision

Use Voyage AI's official Python SDK with `voyage-4` and its default embedding
dimension. Embed indexed chunks as documents and user searches as queries.
Keep the model identifier configurable for maintainers and tests, but do not
expose embedding-model selection in the MVP UI. Isolate Voyage types behind a
small embedding-provider boundary.

Split document embeddings into bounded batches and allow up to four document
requests in flight concurrently. Preserve input order when results are joined,
keep query embedding as one synchronous request, and do not expose concurrency
as an end-user setting.

### Consequences

The MVP requires a separate Voyage API key in addition to the Anthropic key.
This deliberately keeps generation and embedding responsibilities independent
without implementing a general multi-provider plugin system. Retrieval quality
must be measured on the project corpus; `voyage-4-large`, `voyage-4-lite`, or a
different provider can be tested later if the evaluation exposes a need.

Four-way document concurrency was retained after a controlled comparison
reduced elapsed embedding time while preserving the corpus, request count,
accepted tokens, estimated cost, and result order. This is an indexing
performance decision, not a retrieval-quality claim. The comparison is
recorded in the
[evaluation](evaluation.md#81-document-embedding-concurrency).

## D-017: Defer final evaluation-corpus selection

- **Status:** Superseded by
  [D-029](#d-029-select-googlegson-as-the-main-evaluation-corpus)
- **Date:** 2026-09-01

### Context

A credible evaluation requires enough understanding of a repository to verify
ground-truth answers, not merely judge whether generated answers sound
plausible. [`Cross-PR Integration Risk Analyzer`](https://github.com/sophiatheofanidou/cross-pr-integration-risk-analyzer),
an author-owned repository created by the same developer as RepoRationale, has
useful design documents and commit history. Its value during development was
that its rationale was familiar and directly reviewable, not that it was a
dogfooding corpus. It is a separate product and has no pull-request history.
An unfamiliar third-party repository may offer richer history but requires
more domain study before its answers can be reviewed reliably.

### Decision

Do not select the final evaluation repository during initial planning. Make the
selection during final evaluation after the application works and the available
candidate corpora can be inspected. The provisional shortlist and repositories
considered but not carried forward were recorded in D-026.

Do not manufacture pull requests, issues, or rationale solely to make a corpus
appear richer. Repositories must be indexed separately; this decision does not
introduce multi-repository search.

### Consequences

Ingestion development may use fixtures and a convenient repository to validate
GitHub ingestion without treating that repository as the final evaluation
corpus. The final number and composition of reviewed questions will be set
after corpus validation rather than fixed in advance. This deferral ended when
D-029 selected Gson as the main evaluation corpus.

## D-018: Let the user select the repository in Streamlit

- **Status:** Accepted
- **Date:** 2026-09-01

### Context

A preconfigured repository-specific assistant is less reusable and demonstrates
less of the ingestion lifecycle than an application that can prepare a public
repository selected by its user. The alternative is operator-only setup before
the UI starts.

### Decision

The Streamlit MVP will accept a public GitHub `owner/repository` selected by the
user. Repository identity is an input to the application rather than a value
hard-coded in the implementation. The user can initiate indexing for the
selected repository and then query its active index.

### Consequences

Only one repository index is queried at a time. Self-service selection adds a
small onboarding workflow to the Streamlit UI but does not introduce
cross-repository search or a generalized connector platform. Corpus admission,
local reuse, progress, and credential ownership are governed elsewhere.

## D-019: Index the complete supported corpus or reject the repository

- **Status:** Accepted
- **Date:** 2026-09-01

### Context

Silently omitting old history or part of a repository can make an
insufficient-evidence response misleading: the rationale may exist outside the
indexed scope. Folder filtering is also ambiguous because pull requests,
issues, commits, and design documents frequently cross directory boundaries.

### Decision

For an accepted repository, index the complete set of MVP-supported sources
defined by D-003. This does not mean indexing all source-code files or
pull-request diffs.

The MVP accepts repository-level input only. It does not support folder URLs,
date-window indexing, arbitrary partial history, or silent truncation. A light
preflight validates public access and compares the estimated corpus with limits
established by ingestion benchmarks. A repository beyond those limits is rejected
before paid embedding work begins.

### Consequences

The product promise becomes "supported public GitHub repositories within tested
MVP limits," not repositories of unlimited size. The UI must explain rejection
reasons. Preflight limits must account for all issues and for both merged and
closed-unmerged pull requests. Scoped and incremental indexes remain possible
future work, but would require scope-aware answers and abstention language.

## D-020: Persist reusable repository snapshots locally

- **Status:** Accepted
- **Date:** 2026-09-01

### Context

Initial ingestion can be slow and consumes API quota. Repeating it after a page
refresh, application restart, or computer restart would make the local product
unnecessarily expensive and frustrating. The project also needs inspectable
normalized data that can rebuild the vector store or support a future storage
migration.

### Decision

Persist each completed repository snapshot in an ignored local data directory,
identified by repository and resolved commit SHA. Store:

- a manifest describing provenance, status, counts, versions, and timestamps;
- a normalized `sources.jsonl` corpus;
- a deterministic derived `chunks.jsonl` corpus; and
- a Chroma directory containing chunk text, embeddings, metadata, and vector
  index data.

The normalized `sources.jsonl` corpus is the canonical rebuild input.
`chunks.jsonl` and the Chroma index are derived artifacts. Lexical retrieval
reads the persisted chunks, and Chroma records use the same stable chunk IDs,
so lexical and vector evaluation operate on identical chunk boundaries. The
manifest records source and chunk schema versions, the chunker version and
parameters, counts, and the embedding and index configuration required for
compatibility checks.

Only a completely built snapshot is marked ready. A ready snapshot survives UI
refreshes and process or computer restarts. If the remote repository has moved
forward, the user may use the clearly identified existing snapshot or request a
manual full rebuild. Internal compatibility failures appear simply as a rebuild
requirement, not as model or schema choices.

### Consequences

Generated corpora, chunks, embeddings, and Chroma files must not be committed.
Only small deterministic test fixtures, configuration examples, evaluation
inputs, and aggregate results belong in Git. Separate local indexes may exist,
but the application queries one active repository snapshot at a time.

## D-021: Use source-aware 2,000-character chunks and one embedding per chunk

- **Status:** Accepted
- **Date:** 2026-09-01
- **Amended:** 2026-09-06

### Context

A Markdown file, issue, or pull-request discussion can contain several distinct
topics. One embedding for the entire source can blur those topics and retrieve
too much irrelevant context. Chunks that are too small can instead separate a
decision from its reason.

### Decision

Normalize sources first, then split them according to their structure. Markdown
chunking will preserve heading hierarchy and prefer section, paragraph, list,
table, and code-block boundaries. Pull requests, issues, comments, and commit
messages will use source-appropriate boundaries. Each chunk receives one
embedding and retains its source ID, URL, type, position, and relevant heading
or discussion metadata.

Use a maximum of 2,000 characters per chunk with no overlap for the MVP. This
value was selected through a development-only calibration over two completed
repository snapshots. Among the viable candidates, it preserved every reviewed
supporting passage within one chunk, matched the best measured top-five
retrieval result, and produced substantially fewer chunks than the
1,000-character alternative. It therefore retained more source-local context
without showing a loss in the calibration. The candidates, metrics, and full
results are recorded in the [evaluation](evaluation.md).

### Consequences

One source may produce one or many searchable vectors. Retrieval returns chunks
while citations continue to identify the original GitHub source. Chunking
quality and its failure cases must be included in retrieval evaluation.
The selected size is an application configuration rather than an end-user
choice. The six development cases are not the held-out final evaluation and do
not establish vector-retrieval or answer quality; later evaluation must report
failures and may motivate a separately reviewed change rather than silently
tuning this value after results are known.

## D-022: Use bring-your-own credentials

- **Status:** Accepted
- **Date:** 2026-09-01

### Context

Indexing and answering use external GitHub, Voyage, and Anthropic APIs. Using
project-owner credentials for people who clone or run the portfolio project
would expose secrets, transfer their usage cost to the owner, and require an
account or credential-distribution service outside the MVP.

### Decision

Users provide their own GitHub token, Voyage API key, and Anthropic API key
through local secret configuration. Secrets are never committed or stored in
repository snapshot manifests. The application selects the generation model,
embedding model, chunking, and vector-store configuration; the MVP UI does not
offer those as end-user choices.

### Consequences

Running the complete application requires accounts or API access with all three
providers. A GitHub token supplies practical rate limits for public-repository
ingestion but does not add private-repository support or an OAuth flow. The
  [README](../README.md) and [`.env.example`](../.env.example) must document prerequisites and
secret handling without containing real credentials.

## D-023: Normalize citation-addressable items as separate sources

- **Status:** Accepted
- **Date:** 2026-09-02

### Context

A pull request or issue can contain a description, general comments, reviews,
and inline review comments. Combining the entire discussion into one normalized
document would simplify ingestion, but it would weaken source identity,
deduplication, and citation precision. These source boundaries are separate
from chunk boundaries: one normalized source may still produce one or many
retrieval chunks.

### Decision

Normalize each independently identifiable and citation-addressable repository
item as a separate source document. This includes pull-request and issue
descriptions, individual supported comments and reviews, commit messages, and
each Markdown file. Preserve relationships through metadata such as parent
source identity, repository item number, title, position, and source URL.

Markdown sections remain chunks of a single file-level source rather than
separate source documents. Source-aware chunking may also split a long pull
request or issue description into multiple chunks. Child discussion items
retain enough parent metadata to remain understandable without merging the
whole discussion into one document.

### Consequences

The normalized corpus contains more records and must preserve parent-child
relationships, but citations can identify the exact item that supplied the
evidence. Retrieval may later add bounded parent or neighbouring-context
expansion if evaluation shows that isolated comments lack necessary context;
such expansion is not required by this decision.

## D-024: Preserve platform-native source terminology behind one adapter boundary

- **Status:** Accepted
- **Date:** 2026-09-02

### Context

Future platform support should not require changes throughout ingestion,
indexing, retrieval, or answer generation. Replacing familiar terms such as
pull request, issue, and commit with abstract names such as change proposal,
work item, and revision would make the domain harder to understand without
providing necessary MVP behaviour.

### Decision

Use one platform-independent source-document contract and isolate each hosting
platform behind a source adapter. Preserve clear platform-native source-type
names such as `pull_request`, `issue`, `review_comment`, `commit`, and
`markdown_document`, together with an explicit platform identifier and
namespaced source identity.

Source types are extensible rather than a closed list assumed to exist on every
platform. A future adapter may therefore emit platform-appropriate names such
as `merge_request` or `work_item`. Core indexing, storage, retrieval, citation,
and agent behaviour must not depend on provider SDK response types or require
every platform to expose equivalent source types.

### Consequences

The GitHub MVP remains understandable in familiar GitHub terminology while its
core contracts stay reusable. Adding a future platform requires an adapter and
source mappings, plus any genuinely new source-specific processing, but not a
rewrite of the retrieval or agent layers. The MVP will not add a connector
registry, dynamic plugin system, second platform adapter, or speculative common
taxonomy.

## D-025: Keep standalone GitHub Discussions outside the MVP

- **Status:** Accepted
- **Date:** 2026-09-02

### Context

GitHub Discussions are repository-level forum topics and are distinct from the
conversation and code-review content attached to a pull request. Supporting
them would add another container type, categories, nested replies, pagination,
relationship rules, admission estimates, fixtures, and evaluation cases. Some
candidate repositories rely heavily on Discussions, but the MVP can instead
select evaluation repositories with sufficient rationale in its already
supported source types.

### Decision

Do not ingest the standalone GitHub Discussions feature in the MVP. Ingest the
pull-request conversation and review content defined by D-003. Use the term
`pull-request conversation and review content` rather than `pull-request
discussions` when ambiguity is possible.

### Consequences

Repositories whose relevant rationale lives primarily in GitHub Discussions
may be unsuitable for the main evaluation corpus. The platform-independent
source contract remains extensible, so a future GitHub adapter version can add
a clear platform-native discussion source type without changing retrieval,
storage, citation, or agent contracts.

## D-026: Defer final corpus selection with size-tiered candidates

- **Status:** Superseded by
  [D-029](#d-029-select-googlegson-as-the-main-evaluation-corpus)
- **Date:** 2026-09-02

### Context

The evaluation needs recognizable repositories with explicit rationale,
supported repository-native sources, and enough reviewed material for credible
ground truth. It must also exercise repository admission rather than assuming
that every public repository fits the MVP. Current UI counts alone do not
measure the complete historical corpus or rationale quality.

### Decision

Keep final evaluation-corpus selection deferred to the evaluation work as
established by D-017. Carry forward a provisional, size-tiered shortlist for
measured validation:

- [`psf/black`](https://github.com/psf/black) as the leading smaller
  main-corpus candidate;
- [`BurntSushi/ripgrep`](https://github.com/BurntSushi/ripgrep) as a recognizable
  non-Python, smaller-to-moderate alternative with rationale-bearing history
  and Markdown documentation;
- [`pydantic/pydantic`](https://github.com/pydantic/pydantic) as a larger stress
  candidate that may or may not fit the measured limits;
- [`prettier/prettier`](https://github.com/prettier/prettier) as a recognizable
  non-Python, larger stress candidate whose admissibility must be measured; and
- [`microsoft/vscode`](https://github.com/microsoft/vscode) as a deliberately
  oversized candidate expected to produce a clear preflight rejection before
  embedding.

The following repositories were considered but are not carried forward in the
current shortlist:

- [`encode/httpx`](https://github.com/encode/httpx), because its source mix
  relies too heavily on standalone GitHub Discussions excluded from the MVP;
- [`pallets/flask`](https://github.com/pallets/flask), because the supported
  issue and pull-request material observed during candidate review was not rich
  enough for the intended evaluation.

[`Cross-PR Integration Risk Analyzer`](https://github.com/sophiatheofanidou/cross-pr-integration-risk-analyzer)
remained useful as a controlled development and secondary evaluation corpus
because its Markdown decisions and commit history were familiar and
reviewable. It was not a candidate for the main full-scope corpus because it
had no pull-request history and could not exercise the complete supported
source mix by itself.

The shortlist is not limited to Python repositories and may be refined with
recognizable candidates from the wider software-development community before
final evaluation. Each repository must be indexed separately.

### Consequences

Shortlisted meant "evaluate later," not "approved as the final corpus."
Exclusion was not a judgment about repository quality, and candidate status
did not waive corpus limits or establish ground truth. D-029 records the final
selection and closes this shortlist.

## D-027: Use Python 3.13 and uv for project and dependency management

- **Status:** Accepted
- **Date:** 2026-09-04

### Context

The project needs one repeatable way to select Python, create an isolated
environment, declare dependencies, reproduce exact resolved versions, and run
development tools locally and in CI. A workflow based only on `venv` and `pip`
would require dependency declarations and installed versions to be maintained
through separate manual steps.

### Decision

Use Python 3.13 and uv as the project and dependency manager. Keep project
metadata and direct dependency constraints in `pyproject.toml`, commit the
cross-platform `uv.lock`, pin the project Python line in `.python-version`, and
keep the generated `.venv/` outside version control.

Use the uv development dependency group for pytest, Ruff, and mypy. Run project
tools through `uv run`, and use `uv sync --locked` when reproducing the checked-in
environment without changing dependency versions.

### Consequences

Adding or removing a dependency uses uv so the project declaration, lockfile,
and local environment stay synchronized. Dependency upgrades remain explicit
rather than occurring silently. Contributors need uv, but do not need to
manually create or activate a virtual environment for the documented workflow.

## D-028: Set conservative initial ingestion limits from measured builds

- **Status:** Accepted
- **Date:** 2026-09-06
- **Amended:** 2026-09-07

### Context

The complete-corpus promise in D-019 requires the MVP to reject repositories
whose supported history has not been shown to fit its synchronous local
ingestion path. Authenticated builds first established a conservative envelope
on development repositories. A later complete Gson build demonstrated that a
substantially larger repository could be collected, indexed, published, and
reopened safely, so the initial limits were expanded with measured headroom.
The supporting corpus and performance measurements are recorded in the
[evaluation](evaluation.md).

### Decision

Use these measured MVP limits:

| Limit | Value |
| --- | ---: |
| Combined issue and pull-request roots | 3,500 |
| Closed pull requests | 1,500 |
| Commits | 2,500 |
| Git tree entries | 500 |
| Actual normalized sources | 15,000 |
| Actual source-collection requests | 2,000 |

Keep pull-request review-summary concurrency bounded at eight workers. Define
the numeric defaults once as application policy and use the same defaults for
preflight, snapshot building, and the rebuild command. Tests may inject smaller
limits to exercise boundaries without changing product policy.

The 2,000-request cap is a total source-collection budget, not a claim that
total requests and GitHub's per-minute secondary rate limit are
interchangeable. It provides measured headroom over Gson's 1,371 collection
requests. Concurrency remains fixed at eight rather than increasing with the
larger total budget.

### Consequences

Preflight rejects a repository when a measurable root dimension exceeds its
admission threshold. During collection, the hard request budget stops the
request after the permitted maximum before it is sent, and the actual source
cap is checked before publication. No limit failure produces a partial or ready
snapshot.

These values define the supported local MVP envelope, not general production
capacity. Gson is the largest repository whose complete supported corpus has
been successfully exercised through the current workflow. Other repositories
must still pass every admission and runtime dimension; exceeding any ceiling
remains an explicit rejection rather than partial indexing. Further expansion
requires new measurements and a reviewed decision.

## D-029: Select google/gson as the main evaluation corpus

- **Status:** Accepted
- **Date:** 2026-09-07

### Context

The deferred corpus selection was revisited after read-only preflight checks
across recognizable repositories of several sizes and languages. The main
evaluation needed enough long-lived and varied history to exercise the product
where indexed retrieval is intended to be useful, while remaining small enough
for complete local ingestion and manual evidence review.

[`google/gson`](https://github.com/google/gson) is widely recognizable, offers
history across the supported source types, and contains explicit repository-native
Markdown rationale, including a design document that records alternatives and
trade-offs. Its measured workload is materially above the initial envelope but
well below the largest stress candidates considered. It therefore exercises
the scaling question while still supporting reviewable ground truth.

### Decision

Use `google/gson` as the main external evaluation corpus, pinned to an exact
repository commit for reproducibility. Evaluate it in gated stages: complete
source collection, reviewed question and evidence sets, retrieval comparison,
answer evaluation, and operational measurement. The staged method and results
belong in the [evaluation](evaluation.md). The successful full build supports
the shared limits recorded in D-028; further expansion requires new evidence
and a reviewed decision.

### Consequences

The earlier shortlist in D-026 is no longer active. Gson fits the measured MVP
envelope; repositories beyond it remain unsupported rather than partially
indexed. Generated snapshots, indexes, detailed traces, and credentials remain
private local artifacts.

## D-030: Use the exact user question for the first semantic search

- **Status:** Accepted
- **Date:** 2026-09-07

### Context

The original bounded-agent design forced Claude to formulate the first vector
query before seeing evidence. Prompt guidance asked it to preserve distinctive
wording, but the held-out Gson semantic case showed that a close paraphrase can
still move an expected source out of the top five. The unchanged user question
retrieved the intended design-document chunk at rank two; Opus's generated
query did not, and two refinements moved further away from the collection and
type-system rationale.

A bounded post-evaluation diagnostic showed that sending the unchanged question
to the same index recovered the expected source and allowed a grounded answer
in one search and one model call. This isolated the first-query rewrite as an
orchestration problem rather than a missing-corpus problem. The diagnostic and
its limits are recorded in the [evaluation](evaluation.md).

### Decision

The application performs the mandatory first semantic search with the user's
exact, non-blank question. Only after receiving those ranked results does the
Claude agent run. It may provide a cited answer, report insufficient evidence,
or request `refine_search(query, missing_information)`. The total budget remains
three searches, so at most two model-generated refinements can follow the
application-owned first search.

Remove `search_history` from the model's tool surface. The underlying
application search service remains the single vector-retrieval capability;
BM25 remains an offline evaluation baseline and does not enter the product
path. Keep `claude-opus-5` as the MVP answering model.

### Consequences

The model cannot alter the user's intent before the first retrieval and no
longer consumes a provider call merely to restate the question. The workflow
still guarantees retrieval before answering, preserves bounded agentic
refinement, records a sufficiency assessment for every search, and validates
citations against all evidence returned during the run. The diagnostic is
evidence for this orchestration correction, not a replacement held-out run or
a statistically powered model comparison.

## D-031: Support bounded, session-scoped conversational follow-ups

- **Status:** Accepted
- **Date:** 2026-09-08

### Context

Rationale questions within one investigation are rarely fully independent. A
user who has just been told why polling was chosen instead of webhooks
naturally continues with "was that alternative reconsidered later?" rather
than restating the rejected alternative by name. The single-question design
established by D-030 answers such a follow-up literally: its exact,
unresolved pronoun becomes the mandatory first search, and the run has no way
to know what "that" refers to.

Visible chat history without model context would imply continuity while leaving
ambiguous follow-ups unresolved. Durable or unbounded memory would instead
weaken the bounded-agent and evidence-only citation rules in D-004, D-005, and
D-010 and move the product toward a general chatbot. The chosen middle ground
is a small, explicitly non-evidentiary context window. A live Gson diagnostic
confirmed that it could resolve an ambiguous follow-up while keeping all cited
evidence within the current run; the measured result is recorded in the
[evaluation](evaluation.md).

### Decision

Add a small provider-neutral `ConversationContext` of at most the three most
recent completed turns of the current session, ordered oldest to newest. Each
turn carries only the prior question, whether that turn's outcome was
`answered` or `insufficient_evidence`, and the resulting answer or abstention
explanation. It never carries citation excerpts, raw evidence, execution
traces, provider messages, usage data, or SDK objects. Empty context preserves
single-question behaviour.

`answer_question()` accepts this context as an optional argument and passes
it to the answering provider only on the first model turn, alongside the
current question and that turn's initial search evidence, in a structure the
Anthropic adapter labels clearly as non-evidentiary. The mandatory first
semantic search established by D-030 is unchanged: it still uses the user's
exact current question. Claude may use prior turns only to resolve references,
ellipsis, or topic and may request a standalone context-resolved refinement
within the existing three-search limit. Prior turns never count as evidence.
Only evidence returned during the current run can be cited, and the validator
rejects every other evidence ID.

The Streamlit session owns the visible history and supplies at most the three
most recent completed turns. Context is cleared by a browser refresh, new
session or tab, application restart, repository or snapshot change, or index
rebuild. Conversation turns are not written to snapshots, manifests, or other
persistent artifacts; only completed repository indexes persist. Each question
still creates an independent answering run.

### Consequences

A user can ask a natural pronoun-based or topically continuous follow-up
within one session and one repository snapshot without restating prior
context by hand, while every answer, including a follow-up's, remains
grounded only in evidence retrieved during its own run. The held-out evaluation
remains unchanged because it used independent questions and conversation
context is optional. This is session-scoped follow-up support, not durable chat
history, cross-repository memory, or general-purpose conversational memory.

## D-032: Defer selection of the first post-release implementation

- **Status:** Accepted
- **Date:** 2026-09-10

### Context

The completed first release exposes several possible directions for further
work, including repository-limit changes, alternative persistence, more
efficient source collection, additional content sources, durable conversation
history, and external interfaces. The project has not yet established which of
these would provide the most useful next increment. Prioritizing a storage
migration before that comparison would turn a learning possibility into an
unsupported roadmap commitment.

### Decision

Do not select a first post-release implementation yet. Evaluate candidate work
against demonstrated user value, measured limitations, implementation cost, and
the evidence needed to judge its outcome. Once a candidate is selected, record
the product and technical choice explicitly and create a new bounded plan before
implementation begins.

This decision supersedes [D-014](#d-014-make-postgresqlpgvector-the-first-post-mvp-improvement).
PostgreSQL and pgvector remain a possible storage direction, not a committed
next step.

### Consequences

The completed first-release plan remains closed. No future direction listed in
the [project definition](project.md#9-future-direction) has priority merely
because it was considered during the initial build. Post-release work begins
with evaluation and an explicit owner decision rather than an inherited
roadmap assumption.
