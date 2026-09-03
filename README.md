# RepoRationale

**Evidence-grounded agentic RAG for recovering the rationale behind technical
decisions from repository history.**

> Ask why the code changed. Get the evidence behind it.

## Why RepoRationale

Source code usually shows what a system does, but the reasons behind an
implementation may be scattered across pull requests, issues, commit messages,
review discussions, and documentation. Recovering that context manually is
slow, especially in a large or long-lived repository.

RepoRationale is intended to help developers understand unfamiliar code and
make better-informed changes by finding the recorded evidence behind earlier
technical decisions. It prioritizes cited evidence over plausible-sounding
explanations and treats insufficient evidence as a valid result.

## MVP workflow

The MVP workflow is deliberately narrow:

1. validate and index the complete supported history of one public GitHub
   repository;
2. ask a natural-language question about why a technical change was made;
3. retrieve relevant historical evidence through a small, bounded agent loop;
4. answer with verifiable citations, or explicitly report insufficient
   evidence.

GitHub is the source platform supported by the MVP. The internal source and
provider boundaries are designed to avoid tying the core domain to one
platform, but additional repository platforms are not currently implemented.

## Planned MVP technologies

| Technology | Role in the project |
| --- | --- |
| Python | Modular-monolith application and domain logic |
| Streamlit | Local user interface |
| GitHub REST API | Collection of supported repository history |
| Voyage 4 | Embeddings for semantic retrieval |
| Chroma | Persistent local vector index |
| Anthropic Claude | Bounded tool use and evidence-grounded answer generation |
| BM25 | Offline lexical retrieval baseline for evaluation |
| pytest and minimal CI | Automated verification of the implementation |

The core retrieval and agent loop will be project-owned rather than hidden
behind a large orchestration framework. The development foundation uses Python
3.13, uv, pytest, Ruff, and mypy.

## Project status

RepoRationale is in the early implementation stage. The product definition,
architecture, decision log, evaluation plan, and tested Python package skeleton
are established. The end-to-end application is not yet available.

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then
reproduce the locked development environment:

```bash
uv sync --locked
```

Run the current quality checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Project documents

- [Project definition](docs/project.md)
- [Architecture](docs/architecture.md)
- [Evaluation plan](docs/evaluation.md)
- [Decision log](docs/decisions.md)
