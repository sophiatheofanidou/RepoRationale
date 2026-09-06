"""Tests for the rebuild-normalized-sources CLI's limit-flag defaulting.

Exercises only argument parsing and the shared-default/override merge
logic (`_resolve_limits`); no `GitHubClient`, network access, or token is
involved, since that machinery is already covered by the snapshot-workflow
tests.
"""

from reporationale.application.admission import (
    DEFAULT_ADMISSION_LIMITS,
    DEFAULT_RUNTIME_INGESTION_LIMITS,
)
from reporationale.rebuild_normalized_sources import _parse_args, _resolve_limits

_BASE_ARGV = ["octo-org/example-repo", "--snapshot-root", "/tmp/snapshots", "--force"]


def test_resolve_limits_uses_all_defaults_when_no_flags_supplied() -> None:
    args = _parse_args(_BASE_ARGV)

    admission_limits, runtime_limits = _resolve_limits(args)

    assert admission_limits == DEFAULT_ADMISSION_LIMITS
    assert runtime_limits == DEFAULT_RUNTIME_INGESTION_LIMITS


def test_resolve_limits_applies_only_the_supplied_individual_overrides() -> None:
    args = _parse_args(
        [
            *_BASE_ARGV,
            "--max-closed-pull-requests",
            "5",
            "--max-request-count",
            "10",
        ]
    )

    admission_limits, runtime_limits = _resolve_limits(args)

    assert admission_limits.max_closed_pull_requests == 5
    assert (
        admission_limits.max_all_issues_and_pull_requests
        == DEFAULT_ADMISSION_LIMITS.max_all_issues_and_pull_requests
    )
    assert admission_limits.max_commits == DEFAULT_ADMISSION_LIMITS.max_commits
    assert (
        admission_limits.max_tree_entries == DEFAULT_ADMISSION_LIMITS.max_tree_entries
    )

    assert runtime_limits.max_github_request_count == 10
    assert (
        runtime_limits.max_source_count
        == DEFAULT_RUNTIME_INGESTION_LIMITS.max_source_count
    )
