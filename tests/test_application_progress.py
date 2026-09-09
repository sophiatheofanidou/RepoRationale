"""Deterministic, offline tests for the indexing workflows' optional
`on_progress` reporting (`reporationale.application.progress`).

Covers: event ordering across the six named phases, that `"progress"`
events for the expensive embedding phase carry strictly increasing
`completed` counts that reach the true total, that every `"completed"`
event's counts match what was actually built, and that omitting
`on_progress` (the default) changes nothing about a workflow's own
behaviour. No real network, embedding, or Chroma network call is made;
Chroma itself runs locally against `tmp_path`, exactly as the existing
vector-retrieval tests already do.
"""

from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from github_test_support import json_response, mock_transport

import reporationale.application.vector_retrieval as vector_retrieval_module
from reporationale.adapters.github import GitHubClient, GitHubRepositoryMetadata
from reporationale.adapters.snapshot_store import publish_snapshot, snapshot_directory
from reporationale.application.admission import AdmissionLimits, RuntimeIngestionLimits
from reporationale.application.chunk_snapshot import build_chunk_snapshot
from reporationale.application.progress import ProgressEvent
from reporationale.application.snapshot_workflow import build_normalized_source_snapshot
from reporationale.application.vector_retrieval import (
    build_vector_snapshot,
    load_search_history_service,
)
from reporationale.domain.embedding import EmbeddingBatch
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

_SECRET_TOKEN = "super-secret-test-token-value"
_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_COMMIT_SHA = "c" * 40
_TREE_SHA = "d" * 40
_MAX_CHARS = 1000

_GENEROUS_LIMITS = AdmissionLimits(
    max_all_issues_and_pull_requests=1000,
    max_closed_pull_requests=1000,
    max_commits=1000,
    max_tree_entries=1000,
)
_GENEROUS_RUNTIME_LIMITS = RuntimeIngestionLimits(
    max_source_count=1000, max_github_request_count=1000
)


def _metadata() -> GitHubRepositoryMetadata:
    return GitHubRepositoryMetadata(
        identity=_IDENTITY,
        github_id=123456,
        html_url="https://github.com/octo-org/example-repo",  # type: ignore[arg-type]
        private=False,
        default_branch="main",
    )


def _empty_transport() -> httpx.MockTransport:
    """Answers every request an admission-estimated, zero-content
    `build_normalized_source_snapshot` fresh build makes."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo/commits/main":
            return json_response(
                200, {"sha": _COMMIT_SHA, "commit": {"tree": {"sha": _TREE_SHA}}}
            )
        if path in (
            "/repos/octo-org/example-repo/issues",
            "/repos/octo-org/example-repo/pulls",
            "/repos/octo-org/example-repo/commits",
        ):
            return json_response(200, [])
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(
                200, {"sha": _TREE_SHA, "truncated": False, "tree": []}
            )
        raise AssertionError(f"unexpected request: {path}")

    return mock_transport(handler)


def test_build_normalized_source_snapshot_emits_ordered_progress_for_fresh_and_reused_builds(
    tmp_path: Path,
) -> None:
    metadata = _metadata()
    fresh_events: list[ProgressEvent] = []

    result = build_normalized_source_snapshot(
        metadata,
        github_client=GitHubClient(token=_SECRET_TOKEN, transport=_empty_transport()),
        snapshot_root=tmp_path,
        admission_limits=_GENEROUS_LIMITS,
        runtime_limits=_GENEROUS_RUNTIME_LIMITS,
        on_progress=fresh_events.append,
    )

    assert [(event.phase, event.status) for event in fresh_events] == [
        ("collecting_sources", "started"),
        ("collecting_sources", "completed"),
        ("publishing_source_snapshot", "started"),
        ("publishing_source_snapshot", "completed"),
    ]
    assert fresh_events[1].completed == fresh_events[1].total == len(result.documents)
    assert (
        fresh_events[3].completed
        == fresh_events[3].total
        == result.manifest.source_count
    )

    reused_events: list[ProgressEvent] = []
    build_normalized_source_snapshot(
        metadata,
        github_client=GitHubClient(token=_SECRET_TOKEN, transport=_empty_transport()),
        snapshot_root=tmp_path,
        admission_limits=_GENEROUS_LIMITS,
        runtime_limits=_GENEROUS_RUNTIME_LIMITS,
        on_progress=reused_events.append,
    )

    assert [(event.phase, event.status) for event in reused_events] == [
        ("collecting_sources", "completed"),
        ("publishing_source_snapshot", "completed"),
    ]


def test_build_normalized_source_snapshot_without_observer_is_unchanged(
    tmp_path: Path,
) -> None:
    """Omitting `on_progress` (the default) must build exactly the same
    result as always -- proves the observer is a pure add-on."""
    metadata = _metadata()
    result = build_normalized_source_snapshot(
        metadata,
        github_client=GitHubClient(token=_SECRET_TOKEN, transport=_empty_transport()),
        snapshot_root=tmp_path,
        admission_limits=_GENEROUS_LIMITS,
        runtime_limits=_GENEROUS_RUNTIME_LIMITS,
    )
    assert result.reused_existing_snapshot is False
    assert result.manifest.source_count == 0


def _document(index: int) -> SourceDocument:
    return SourceDocument(
        source_id=f"github:octo-org/example-repo:markdown:doc{index}.md",
        platform="github",
        repository="octo-org/example-repo",
        source_type="markdown",
        text=f"Distinct rationale paragraph number {index} about the change.",
        source_url="https://github.com/octo-org/example-repo/blob/main/a.md",
    )


def _publish_source_snapshot(tmp_path: Path, *, document_count: int) -> Path:
    documents = tuple(_document(index) for index in range(document_count))
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": document_count},
        producer_version="test/0",
    )
    return snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )


def test_build_chunk_snapshot_emits_ordered_progress_for_fresh_and_reused_builds(
    tmp_path: Path,
) -> None:
    snapshot_dir = _publish_source_snapshot(tmp_path, document_count=2)

    fresh_events: list[ProgressEvent] = []
    result = build_chunk_snapshot(
        snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS, on_progress=fresh_events.append
    )

    assert [(event.phase, event.status) for event in fresh_events] == [
        ("creating_chunks", "started"),
        ("creating_chunks", "completed"),
    ]
    assert fresh_events[1].completed == fresh_events[1].total == len(result.chunks)

    reused_events: list[ProgressEvent] = []
    build_chunk_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        on_progress=reused_events.append,
    )

    assert [(event.phase, event.status) for event in reused_events] == [
        ("creating_chunks", "completed")
    ]
    assert reused_events[0].completed == reused_events[0].total == len(result.chunks)


class _CountingEmbeddingProvider:
    """A minimal fake `EmbeddingProvider`: every text maps to the same
    fixed-dimension vector (ranking is irrelevant to these tests), and
    every `embed_documents` call is counted so a test can prove the
    embedding loop makes one call per progress batch."""

    def __init__(self) -> None:
        self.document_call_count = 0

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.document_call_count += 1
        return EmbeddingBatch(
            vectors=tuple((1.0, 0.0) for _ in texts), total_tokens=len(texts)
        )

    def embed_query(self, text: str) -> EmbeddingBatch:
        return EmbeddingBatch(vectors=((1.0, 0.0),), total_tokens=1)


def test_build_vector_snapshot_reports_monotonic_embedding_batch_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With five chunks embedded two at a time, `completed` must strictly
    increase across `"progress"` events, ending at the true total -- never
    a timer- or estimate-based number."""
    monkeypatch.setattr(vector_retrieval_module, "_EMBEDDING_PROGRESS_BATCH_SIZE", 2)
    snapshot_dir = _publish_source_snapshot(tmp_path, document_count=5)
    build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS)
    provider = _CountingEmbeddingProvider()
    events: list[ProgressEvent] = []

    result = build_vector_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=provider,
        on_progress=events.append,
    )

    assert len(result.chunks) == 5
    assert provider.document_call_count == 3  # batches of 2, 2, 1

    embedding_events = [event for event in events if event.phase == "embedding_chunks"]
    assert embedding_events[0].status == "started"
    assert embedding_events[-1].status == "completed"
    assert embedding_events[-1].completed == embedding_events[-1].total == 5

    progress_events = [
        event for event in embedding_events if event.status == "progress"
    ]
    completed_values = [event.completed for event in progress_events]
    assert completed_values == [2, 4, 5]
    assert all(event.total == 5 for event in progress_events)

    building_events = [
        event for event in events if event.phase == "building_vector_index"
    ]
    assert [event.status for event in building_events] == ["started", "completed"]

    last_embedding_index = max(
        index for index, event in enumerate(events) if event.phase == "embedding_chunks"
    )
    first_building_index = min(
        index
        for index, event in enumerate(events)
        if event.phase == "building_vector_index"
    )
    assert last_embedding_index < first_building_index


def test_build_vector_snapshot_emits_reused_completed_events_without_embedding(
    tmp_path: Path,
) -> None:
    snapshot_dir = _publish_source_snapshot(tmp_path, document_count=2)
    build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS)
    build_vector_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=_CountingEmbeddingProvider(),
    )

    reuse_provider = _CountingEmbeddingProvider()
    events: list[ProgressEvent] = []
    result = build_vector_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=reuse_provider,
        on_progress=events.append,
    )

    assert result.reused_existing_artifact is True
    assert reuse_provider.document_call_count == 0
    assert [(event.phase, event.status) for event in events] == [
        ("embedding_chunks", "completed"),
        ("building_vector_index", "completed"),
    ]


def test_load_search_history_service_emits_all_three_phases_in_order(
    tmp_path: Path,
) -> None:
    snapshot_dir = _publish_source_snapshot(tmp_path, document_count=2)
    build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS)
    provider = _CountingEmbeddingProvider()
    events: list[ProgressEvent] = []

    load_search_history_service(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=provider,
        on_progress=events.append,
    )

    phases_seen_in_order: list[str] = []
    for event in events:
        if not phases_seen_in_order or phases_seen_in_order[-1] != event.phase:
            phases_seen_in_order.append(event.phase)
    assert phases_seen_in_order == [
        "embedding_chunks",
        "building_vector_index",
        "validating_vector_index",
    ]
    assert events[-1].phase == "validating_vector_index"
    assert events[-1].status == "completed"
    assert events[-1].completed == events[-1].total == 2
