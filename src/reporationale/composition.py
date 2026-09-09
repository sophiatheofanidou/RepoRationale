"""Composition root for the Streamlit demonstration.

Builds the concrete adapters the UI calls, without exposing any of this
wiring as an end-user choice. Kept separate from `streamlit_app.py` so
this wiring can be unit-tested without importing `streamlit` or making a
network call.

Every value fixed here is application/maintainer configuration, never a
value the MVP UI lets a user pick: the answering model defaults to
`claude-opus-5`, selected through a bounded development comparison, but
stays overridable through an environment variable for maintainers and
tests. The 2,000-character chunk size and the local snapshot-storage root
are fixed the same way.
"""

import os
from pathlib import Path

from reporationale.adapters.anthropic_answering import (
    AnthropicAnsweringAdapter,
    build_anthropic_answering_adapter,
)
from reporationale.adapters.github import GitHubClient
from reporationale.adapters.voyage_embeddings import (
    VoyageEmbeddingAdapter,
    build_voyage_embedding_adapter,
)
from reporationale.config import Settings

# claude-opus-5 was selected through a bounded development comparison.
# Configurable only through the environment variable below, for
# maintainers and tests; never exposed as an MVP UI choice.
DEFAULT_ANSWERING_MODEL = "claude-opus-5"
_ANSWERING_MODEL_ENV_VAR = "REPORATIONALE_ANSWERING_MODEL"

# The fixed MVP chunk size. Application configuration, not an
# end-user-selectable setting.
CHUNK_MAX_CHARS = 2000

# Generated snapshots are private local data, kept only for local reuse,
# and already excluded by .gitignore ("data/" and "snapshots/");
# overridable for maintainers and tests that need an isolated root.
DEFAULT_SNAPSHOT_ROOT = Path("data") / "snapshots"
_SNAPSHOT_ROOT_ENV_VAR = "REPORATIONALE_SNAPSHOT_ROOT"


def resolve_answering_model_id() -> str:
    """The Claude model identifier for this run: `DEFAULT_ANSWERING_MODEL`
    unless a maintainer overrides it through `REPORATIONALE_ANSWERING_MODEL`."""
    override = os.environ.get(_ANSWERING_MODEL_ENV_VAR)
    return override if override else DEFAULT_ANSWERING_MODEL


def resolve_snapshot_root() -> Path:
    """The local directory repository snapshots are stored under:
    `DEFAULT_SNAPSHOT_ROOT` unless a maintainer overrides it through
    `REPORATIONALE_SNAPSHOT_ROOT`."""
    override = os.environ.get(_SNAPSHOT_ROOT_ENV_VAR)
    return Path(override) if override else DEFAULT_SNAPSHOT_ROOT


def build_github_client(settings: Settings) -> GitHubClient:
    """Construct the one `GitHubClient` used for a single preflight or
    indexing action. Callers are responsible for closing it (or using it as
    a context manager) once that action completes."""
    return GitHubClient(token=settings.github_token.get_secret_value())


def build_embedding_provider(settings: Settings) -> VoyageEmbeddingAdapter:
    """Construct the fixed Voyage embedding provider, not user-configurable.
    Safe to construct even when no embedding call will actually be made
    this run (for example, reusing an already-built vector index)."""
    return build_voyage_embedding_adapter(
        api_key=settings.voyage_api_key.get_secret_value()
    )


def build_answering_model(settings: Settings) -> AnthropicAnsweringAdapter:
    """Construct one fresh answering-model instance for one question.

    An `AnthropicAnsweringAdapter` owns growing per-run message history and
    must never be reused across questions (see
    `reporationale.application.answering_workflow.answer_question`), so
    this must be called again for every new question.
    """
    return build_anthropic_answering_adapter(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=resolve_answering_model_id(),
    )
