# RepoRationale

> Ask why the code changed. Get the evidence behind it.

RepoRationale helps developers find the documented reasons behind technical
changes in a public GitHub repository. It indexes repository history locally
and answers questions with inspectable sources, or reports when the retrieved
evidence is insufficient.

**Engineering focus:** Python · Generative AI · Agentic RAG · Semantic
retrieval · LLM tool calling · Prompt and context engineering · LLM evaluation

[![CI](https://github.com/sophiatheofanidou/RepoRationale/actions/workflows/ci.yml/badge.svg)](https://github.com/sophiatheofanidou/RepoRationale/actions/workflows/ci.yml)

<p align="center">
  <a href="docs/product-walkthrough.md">Product walkthrough</a> ·
  <a href="docs/evaluation.md">Evaluation</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="#quick-start">Quick start</a>
</p>

<p align="center">
  <img src="docs/assets/screenshots/grounded-answer-gson.png" alt="RepoRationale answering why Gson classes were marked final, with an expanded source citation" width="900">
</p>
<p align="center"><em>An example of a grounded answer, with citations and an expanded supporting source.</em></p>

## Why RepoRationale

Source code usually shows what a system does, but not why a restriction,
workaround, or design choice exists. That rationale may be scattered across
years of pull requests, issues, commit messages, review discussions, and
documentation.

RepoRationale makes that history reusable through a local index and a bounded
evidence workflow:

- **Search before answering:** at most three searches, with searches and
  evidence-sufficiency assessments recorded in the run trace.
- **Validate citations:** each citation must resolve to evidence retrieved
  during the current question's run.
- **Make uncertainty visible:** insufficient evidence is an explicit outcome.

These controls make failures inspectable; they do not guarantee that a cited
claim is correct. A general coding agent may be simpler for a one-off question.
RepoRationale's focus is a reusable workflow with measured behaviour.

## How it works

1. Check one public repository and reuse a compatible completed snapshot when
   one is available.
2. If an index is required, collect the complete supported history only after
   explicit confirmation. Unsupported repositories are rejected rather than
   partially indexed.
3. Search first with the user's exact question, then allow the bounded agent to
   request narrower semantic searches when evidence is missing.
4. Return a citation-validated answer or report that the retrieved evidence is
   insufficient.

Example questions include:

- Why was this restriction introduced?
- Was this alternative tried or rejected before?
- Did a later decision supersede the original one?

The [product walkthrough](docs/product-walkthrough.md) shows the full interface,
including cold indexing, snapshot reuse, contextual follow-ups, explicit
abstention, rebuild confirmation, and alternative failure states.

## Measured, not just demonstrated

The held-out evaluation used [`google/gson`](https://github.com/google/gson),
Google's Java JSON library: six answerable questions and two unsupported-premise
controls, run once with a frozen configuration and no retries.

The original held-out run achieved expected-source coverage on five of the six
answerable cases. After the exact-question-first correction, targeted validation
recovered the expected source for the remaining case.

| Measure | Recorded result |
| --- | ---: |
| Expected evidence in the top five results | **Vector: 6/6; BM25 baseline: 4/6** |
| Correct `answered` / `insufficient_evidence` outcome | **8/8** |
| Expected-source coverage after the correction | **6/6 evaluated cases** |
| Malformed or unauthorized citations | **0** |
| Mean end-to-end answer latency | **15.14 s** |

Manual review found adequate claim support and citation completeness across
the original eight cases. The one answer that initially missed part of the
preferred rationale recovered that evidence in the targeted post-fix check.
Total estimated Anthropic cost for the original eight answers was **$0.52**.

**Separate indexing measurements:** a later cold build of 13,898 sources and
17,484 chunks took **6.74 minutes**; reopening took **2.97 seconds** without
document re-embedding.

The sample is small, and no head-to-head comparison with a general coding agent
was performed. The [evaluation](docs/evaluation.md) records the methodology,
failed runs, cases where BM25 won, and controlled performance experiments.

## Quick start

RepoRationale requires [uv](https://docs.astral.sh/uv/getting-started/installation/)
and three personal credentials: a GitHub token, a Voyage API key, and an
Anthropic API key.

```bash
git clone https://github.com/sophiatheofanidou/RepoRationale.git
cd RepoRationale
uv sync --locked
```

Copy `.env.example` to `.env` on macOS or Linux:

```bash
cp .env.example .env
```

Or with PowerShell:

```powershell
Copy-Item .env.example .env
```

Fill in `GITHUB_TOKEN`, `VOYAGE_API_KEY`, and `ANTHROPIC_API_KEY`, then run:

```bash
uv run streamlit run src/reporationale/streamlit_app.py
```

RepoRationale uses **BYOK (bring your own keys)**. Indexing and answering use
the credentials and usage quotas of your own provider accounts and may incur
provider charges. The local `.env`, downloaded repository data, generated
indexes, and evaluation traces are excluded from version control.

For development verification:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Architecture and engineering quality

RepoRationale is a Python modular monolith with separate interface,
application-workflow, domain, and provider-adapter boundaries. A small
project-owned loop controls retrieval, refinements, and citation validation.

- **Application:** [Python](https://www.python.org/) 3.13 and [Streamlit](https://docs.streamlit.io/).
- **Ingestion and retrieval:** [GitHub REST API](https://docs.github.com/en/rest), [Voyage 4](https://docs.voyageai.com/docs/embeddings), and locally persisted [Chroma](https://docs.trychroma.com/); [BM25](https://en.wikipedia.org/wiki/Okapi_BM25) is an evaluation-only baseline.
- **Answering:** [Anthropic Claude](https://platform.claude.com/docs/en/models/overview), using `claude-opus-5`.
- **Quality:** [uv](https://docs.astral.sh/uv/), [pytest](https://docs.pytest.org/), [Ruff](https://docs.astral.sh/ruff/), and [mypy](https://mypy-lang.org/).

The release passes 741 automated tests, Ruff linting and formatting, strict
mypy checks across 89 source files, and the same quality gates in
[GitHub Actions](https://github.com/sophiatheofanidou/RepoRationale/actions/workflows/ci.yml).
The component model and technical contracts are documented in the
[architecture](docs/architecture.md).

## First-release scope

The first release supports one public GitHub repository at a time, complete
ingestion of supported pull-request, issue, commit-message, and Markdown
history, reusable local snapshots, bounded semantic retrieval, cited answers,
explicit abstention, and contextual follow-ups within one investigation.

It deliberately excludes private-repository access, other repository
platforms, multi-repository search, automatic synchronization, partial
indexing, durable conversation history, cloud or multi-user deployment, and
repository modification. These are product boundaries, not unfinished tasks.
The full scope is in the [project definition](docs/project.md), and the reasons
behind accepted choices are in the [decision log](docs/decisions.md).

## Documentation

| Document | Covers |
| --- | --- |
| [Project definition](docs/project.md) | Product scope, target user, and first-release boundaries |
| [Product walkthrough](docs/product-walkthrough.md) | Screenshots of the complete user-visible workflow |
| [Architecture](docs/architecture.md) | Components, contracts, boundaries, and lifecycle diagrams |
| [Evaluation](docs/evaluation.md) | Methodology, measurements, results, and limitations |
| [Decision log](docs/decisions.md) | Accepted choices, superseded decisions, and considered alternatives |
| [Implementation plan](docs/plan.md) | Completed milestones and final verification |
| [AI collaboration guide](AGENTS.md) | Human authority, agent roles, review, and Git ownership rules |
| [Claude implementation guide](CLAUDE.md) | Bounded implementation and handoff protocol |

## Development approach

Development was coordinated through a documented, human-owned workflow using
two AI agents with separate responsibilities: Codex for planning,
documentation, and independent review, and Claude for bounded implementation
tasks. Product and technical decisions, Git history, commits, tags, and
publication remained under explicit owner approval. The public collaboration
guides record this process without replacing the product, architecture,
decision, or evaluation documents as sources of truth.

## License

RepoRationale is available under the [MIT License](LICENSE).
