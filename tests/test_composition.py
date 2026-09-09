"""Tests for the Streamlit composition root's fixed defaults.

The accepted MVP answering model, claude-opus-5, requires that the
composition root introducing it defaults to that model and carries a test
confirming that default; `streamlit_app.py` is that composition root's
caller. These tests exercise only the plain-Python wiring in
`composition.py`, never `streamlit` itself and never a real network call.
"""

from pathlib import Path

import pytest

from reporationale.composition import (
    CHUNK_MAX_CHARS,
    DEFAULT_ANSWERING_MODEL,
    DEFAULT_SNAPSHOT_ROOT,
    build_answering_model,
    build_embedding_provider,
    build_github_client,
    resolve_answering_model_id,
    resolve_snapshot_root,
)
from reporationale.config import Settings


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("GITHUB_TOKEN", "github-test-value")
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-test-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    return Settings(_env_file=None)


def test_default_answering_model_is_claude_opus_5() -> None:
    assert DEFAULT_ANSWERING_MODEL == "claude-opus-5"
    assert resolve_answering_model_id() == "claude-opus-5"


def test_built_answering_model_defaults_to_claude_opus_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = build_answering_model(_settings(monkeypatch))
    assert model.model_id == "claude-opus-5"


def test_answering_model_override_is_read_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    monkeypatch.setenv("REPORATIONALE_ANSWERING_MODEL", "claude-sonnet-5")
    assert resolve_answering_model_id() == "claude-sonnet-5"
    model = build_answering_model(settings)
    assert model.model_id == "claude-sonnet-5"


def test_snapshot_root_defaults_and_is_overridable_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REPORATIONALE_SNAPSHOT_ROOT", raising=False)
    assert resolve_snapshot_root() == DEFAULT_SNAPSHOT_ROOT

    monkeypatch.setenv("REPORATIONALE_SNAPSHOT_ROOT", "custom/snapshots")
    assert resolve_snapshot_root() == Path("custom/snapshots")


def test_chunk_max_chars_matches_the_accepted_mvp_chunk_size() -> None:
    assert CHUNK_MAX_CHARS == 2000


def test_build_github_client_and_embedding_provider_do_not_require_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    client = build_github_client(settings)
    try:
        assert client.request_count == 0
    finally:
        client.close()

    # Constructing the provider must not itself make a network call.
    build_embedding_provider(settings)
