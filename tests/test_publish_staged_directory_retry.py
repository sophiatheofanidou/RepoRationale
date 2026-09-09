"""Tests for `publish_staged_directory`'s bounded retry around a
transient Windows `PermissionError` on a directory rename (see its
module-level `_RENAME_RETRY_ATTEMPTS` comment): the observed real
failure was a `PermissionError` renaming a freshly built vector-index
staging directory into place, traced to Windows/Chroma releasing an
OS-level file handle slightly after `client.close()` returns.

Every test injects a fake `rename` (and a non-sleeping fake `sleep`, so
no test actually waits) that intercepts only the specific staging/target
transition under test and delegates every other call to the real
`os.rename`, so filesystem state stays real and directly assertable.
"""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from reporationale.adapters.snapshot_store import publish_staged_directory

_MAX_ATTEMPTS = 5  # must match the module's own _RENAME_RETRY_ATTEMPTS


def _record_sleep() -> tuple[list[float], Callable[[float], None]]:
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, sleep


def test_transient_permission_error_succeeds_after_retry(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "marker.txt").write_text("new", encoding="utf-8")
    target = tmp_path / "target"

    failures_remaining = 2
    call_count = 0

    def flaky_rename(source: Path, destination: Path) -> None:
        nonlocal failures_remaining, call_count
        call_count += 1
        if failures_remaining > 0:
            failures_remaining -= 1
            raise PermissionError("simulated transient Windows file lock")
        os.rename(source, destination)

    sleep_calls, sleep = _record_sleep()

    publish_staged_directory(staging, target, rename=flaky_rename, sleep=sleep)

    assert (target / "marker.txt").read_text(encoding="utf-8") == "new"
    assert not staging.exists()
    assert call_count == 3
    assert sleep_calls == [0.2, 0.2]


def test_persistent_permission_error_propagates(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "target"

    call_count = 0

    def always_fails(source: Path, destination: Path) -> None:
        nonlocal call_count
        call_count += 1
        raise PermissionError("simulated persistent Windows file lock")

    sleep_calls, sleep = _record_sleep()

    with pytest.raises(PermissionError):
        publish_staged_directory(staging, target, rename=always_fails, sleep=sleep)

    assert call_count == _MAX_ATTEMPTS
    assert sleep_calls == [0.2] * (_MAX_ATTEMPTS - 1)
    # Nothing was renamed: the staging directory this call did not create
    # is left exactly where the caller put it, for the caller's own
    # cleanup, and no target was ever created.
    assert staging.exists()
    assert not target.exists()


def test_replacement_preserves_previous_target_on_persistent_failure(
    tmp_path: Path,
) -> None:
    """A persistent failure replacing an *existing* completed artifact
    must restore the original target content -- the atomic backup/restore
    guarantee -- rather than leaving it missing or replaced with a
    half-published directory."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "marker.txt").write_text("new", encoding="utf-8")

    target = tmp_path / "target"
    target.mkdir()
    (target / "marker.txt").write_text("old", encoding="utf-8")

    def selectively_flaky_rename(source: Path, destination: Path) -> None:
        # Only the staging -> target transition is made to fail
        # persistently; the target -> backup move and the final
        # backup -> target restore both use the real rename, so the
        # filesystem state is real and directly assertable.
        if source == staging and destination == target:
            raise PermissionError("simulated persistent Windows file lock")
        os.rename(source, destination)

    sleep_calls, sleep = _record_sleep()

    with pytest.raises(PermissionError):
        publish_staged_directory(
            staging, target, rename=selectively_flaky_rename, sleep=sleep
        )

    assert target.is_dir()
    assert (target / "marker.txt").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "target.backup").exists()
    assert sleep_calls == [0.2] * (_MAX_ATTEMPTS - 1)
