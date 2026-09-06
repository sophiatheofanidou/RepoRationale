"""Tests for the normalized-source snapshot store: deterministic
serialization, atomic publication, loading/validation, and lookup.

Every test writes under `tmp_path`; no generated snapshot is ever written
into the repository.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from reporationale.adapters.snapshot_store import (
    SnapshotAlreadyExists,
    SnapshotCorrupted,
    SnapshotIncompatible,
    SnapshotNotFound,
    load_snapshot,
    look_up_normalized_source_snapshot,
    publish_snapshot,
    serialize_sources_jsonl,
    snapshot_directory,
)
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.snapshot import SnapshotManifest
from reporationale.domain.source_document import SourceDocument

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_COMMIT_SHA = "c" * 40


def _document(source_id: str, text: str = "hello") -> SourceDocument:
    return SourceDocument(
        source_id=source_id,
        platform="github",
        repository="octo-org/example-repo",
        source_type="markdown",
        text=text,
        source_url="https://github.com/octo-org/example-repo/blob/main/a.md",
    )


def test_serialize_sources_jsonl_is_deterministic_and_digest_matches() -> None:
    documents = (
        _document("github:octo-org/example-repo:markdown:a.md"),
        _document("github:octo-org/example-repo:markdown:b.md"),
    )

    first = serialize_sources_jsonl(documents)
    second = serialize_sources_jsonl(documents)

    assert first == second
    assert first.endswith(b"\n")
    assert first.count(b"\n") == 2
    assert serialize_sources_jsonl(()) == b""


@pytest.mark.parametrize("owner", ["..", "o@w", "."])
def test_snapshot_directory_rejects_unsafe_owner_segment(
    owner: str, tmp_path: Path
) -> None:
    """`RepositoryIdentity` itself only forbids whitespace and `/`; the
    snapshot store independently enforces a stricter filesystem-safe
    allowlist rather than trusting that upstream validation is enough."""
    identity = RepositoryIdentity(platform="github", owner=owner, name="example-repo")
    with pytest.raises(ValueError):
        snapshot_directory(
            root=tmp_path, identity=identity, resolved_commit_sha=_COMMIT_SHA
        )


def test_publish_and_load_snapshot_round_trip(tmp_path: Path) -> None:
    documents = (
        _document("github:octo-org/example-repo:markdown:a.md"),
        _document("github:octo-org/example-repo:markdown:b.md"),
    )

    manifest = publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": 2},
        producer_version="test/0",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert manifest.status == "sources_complete"
    assert manifest.source_count == 2

    directory = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    loaded_manifest, loaded_documents = load_snapshot(
        directory, expected_identity=_IDENTITY, expected_commit_sha=_COMMIT_SHA
    )
    assert loaded_manifest == manifest
    assert loaded_documents == documents

    lookup = look_up_normalized_source_snapshot(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    assert lookup.kind == "compatible"
    assert lookup.manifest == manifest

    forced_rebuild_lookup = look_up_normalized_source_snapshot(
        root=tmp_path,
        identity=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        force_rebuild=True,
    )
    assert forced_rebuild_lookup.kind == "rebuild_requested"

    missing_lookup = look_up_normalized_source_snapshot(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha="f" * 40
    )
    assert missing_lookup.kind == "missing"


@pytest.mark.parametrize(
    ("corrupt", "expected_error"),
    [
        ("missing_manifest", SnapshotNotFound),
        ("bad_json_manifest", SnapshotCorrupted),
        ("wrong_schema_version", SnapshotIncompatible),
        ("digest_mismatch", SnapshotCorrupted),
        ("blank_line", SnapshotCorrupted),
        ("wrong_identity", SnapshotCorrupted),
    ],
)
def test_load_snapshot_rejects_corrupt_or_incompatible_variants(
    corrupt: str, expected_error: type[Exception], tmp_path: Path
) -> None:
    documents = (_document("github:octo-org/example-repo:markdown:a.md"),)
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": 1},
        producer_version="test/0",
    )
    directory = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    manifest_path = directory / "manifest.json"
    sources_path = directory / "sources.jsonl"
    expected_identity = _IDENTITY

    if corrupt == "missing_manifest":
        manifest_path.unlink()
    elif corrupt == "bad_json_manifest":
        manifest_path.write_text("not json", encoding="utf-8")
    elif corrupt == "wrong_schema_version":
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["source_schema_version"] = 999
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    elif corrupt == "digest_mismatch":
        sources_path.write_bytes(b'{"tampered": true}\n')
    elif corrupt == "blank_line":
        sources_path.write_bytes(sources_path.read_bytes() + b"\n")
    elif corrupt == "wrong_identity":
        expected_identity = RepositoryIdentity(
            platform="github", owner="other-org", name="other-repo"
        )

    with pytest.raises(expected_error):
        load_snapshot(
            directory,
            expected_identity=expected_identity,
            expected_commit_sha=_COMMIT_SHA,
        )


def test_publish_snapshot_preserves_previous_snapshot_when_rebuild_fails(
    tmp_path: Path,
) -> None:
    documents = (_document("github:octo-org/example-repo:markdown:a.md"),)

    original_manifest = publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": 1},
        producer_version="test/0",
    )

    # An unforced publish over an existing completed snapshot must refuse
    # rather than overwrite it implicitly.
    with pytest.raises(SnapshotAlreadyExists):
        publish_snapshot(
            root=tmp_path,
            repository=_IDENTITY,
            default_branch="main",
            resolved_commit_sha=_COMMIT_SHA,
            documents=documents,
            counts_by_source_type={"markdown": 1},
            producer_version="test/0",
        )

    # A forced replacement whose manifest contradicts its own documents
    # fails before publication; the previous completed snapshot must
    # survive untouched, and no partial/broken replacement is published.
    with pytest.raises(ValidationError):
        publish_snapshot(
            root=tmp_path,
            repository=_IDENTITY,
            default_branch="main",
            resolved_commit_sha=_COMMIT_SHA,
            documents=documents,
            counts_by_source_type={"markdown": 999},
            producer_version="test/0",
            force=True,
        )

    directory = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    reloaded_manifest, reloaded_documents = load_snapshot(
        directory, expected_identity=_IDENTITY, expected_commit_sha=_COMMIT_SHA
    )
    assert reloaded_manifest == original_manifest
    assert reloaded_documents == documents
    assert not any(
        entry.name.startswith(".staging-") or entry.name.endswith(".backup")
        for entry in directory.parent.iterdir()
    )


def test_publish_snapshot_forced_replacement_succeeds_and_leaves_no_backup(
    tmp_path: Path,
) -> None:
    """The normal (uninterrupted) forced-replacement path actually swaps in
    the new snapshot and cleans up its own backup."""
    first_documents = (_document("github:octo-org/example-repo:markdown:a.md"),)
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=first_documents,
        counts_by_source_type={"markdown": 1},
        producer_version="test/0",
    )

    second_documents = (
        _document("github:octo-org/example-repo:markdown:a.md"),
        _document("github:octo-org/example-repo:markdown:b.md"),
    )
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=second_documents,
        counts_by_source_type={"markdown": 2},
        producer_version="test/1",
        force=True,
    )

    directory = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    manifest, documents = load_snapshot(directory)
    assert manifest.source_count == 2
    assert documents == second_documents
    assert not (directory.parent / f"{directory.name}.backup").exists()


def test_recovery_restores_previous_snapshot_when_target_missing_but_backup_valid(
    tmp_path: Path,
) -> None:
    """Simulates a crash between the two replacement renames: `target` is
    absent, but the fixed backup path still holds the valid previous
    snapshot. The next lookup or publish call must restore it, not treat
    the repository as having no snapshot at all."""
    documents = (_document("github:octo-org/example-repo:markdown:a.md"),)
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": 1},
        producer_version="test/0",
    )
    directory = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    backup = directory.parent / f"{directory.name}.backup"
    directory.rename(backup)
    assert not directory.exists()

    lookup = look_up_normalized_source_snapshot(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )

    assert lookup.kind == "compatible"
    assert directory.exists()
    assert not backup.exists()
    _manifest, loaded_documents = load_snapshot(directory)
    assert loaded_documents == documents


def test_recovery_discards_stale_backup_when_target_already_valid(
    tmp_path: Path,
) -> None:
    """Simulates a crash between the second replacement rename and the
    final backup cleanup: both `target` and a backup exist, and `target`
    is already the valid new snapshot. Recovery must discard the stale
    backup rather than leaving it (or restoring it over the newer target)."""
    documents = (_document("github:octo-org/example-repo:markdown:a.md"),)
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": 1},
        producer_version="test/0",
    )
    directory = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    backup = directory.parent / f"{directory.name}.backup"
    shutil.copytree(directory, backup)

    lookup = look_up_normalized_source_snapshot(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )

    assert lookup.kind == "compatible"
    assert directory.exists()
    assert not backup.exists()


@pytest.mark.parametrize("field", ["created_at", "completed_at"])
def test_snapshot_manifest_rejects_timezone_naive_timestamps(field: str) -> None:
    kwargs: dict[str, object] = {
        "manifest_schema_version": 1,
        "source_schema_version": 1,
        "repository": _IDENTITY,
        "resolved_commit_sha": _COMMIT_SHA,
        "default_branch": "main",
        "status": "sources_complete",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "completed_at": datetime(2026, 1, 1, tzinfo=UTC),
        "source_count": 0,
        "counts_by_source_type": {},
        "sources_digest": "0" * 64,
        "producer_version": "test/0",
    }
    kwargs[field] = datetime(2026, 1, 1)  # naive: no tzinfo

    with pytest.raises(ValidationError):
        SnapshotManifest(**kwargs)
