"""Standalone command: explicitly rebuild one repository's normalized-source
snapshot.

Rebuilds normalized sources only. It does not chunk anything, call an
embedding provider, or touch a vector index — those are separate retrieval-
index concerns. Reads the GitHub token only through the existing local secret
configuration boundary (`reporationale.config.load_github_token`) and
never prints it; does not require a Voyage or Anthropic key. A rebuild
always replaces any existing compatible snapshot for the resolved
revision, so `--force` is mandatory rather than a default-on switch.

The six limit flags are optional overrides of the shared
`DEFAULT_ADMISSION_LIMITS`/`DEFAULT_RUNTIME_INGESTION_LIMITS` (defined once
in `reporationale.application.admission`); this command does not define or
duplicate those numbers itself. With no limit flags, the rebuild uses all
defaults; supplying one or more flags overrides only those specific
values, for controlled experiments.

Not registered as a console-script entry point; run it with
`uv run python -m reporationale.rebuild_normalized_sources ...`.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from reporationale.adapters.github import (
    GitHubClient,
    parse_github_repository_reference,
)
from reporationale.application.admission import (
    DEFAULT_ADMISSION_LIMITS,
    DEFAULT_RUNTIME_INGESTION_LIMITS,
    AdmissionLimits,
    RuntimeIngestionLimits,
)
from reporationale.application.snapshot_workflow import build_normalized_source_snapshot
from reporationale.config import load_github_token


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rebuild-normalized-sources",
        description=(
            "Rebuild one public GitHub repository's normalized-source "
            "snapshot. Does not create chunks, embeddings, or a vector "
            "index; those are separate retrieval-index work."
        ),
    )
    parser.add_argument(
        "repository", help="A public GitHub repository as owner/repository."
    )
    parser.add_argument(
        "--snapshot-root",
        required=True,
        type=Path,
        help="The local directory normalized-source snapshots are stored under.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Required: confirms this rebuild may replace an existing "
            "compatible normalized-source snapshot for this repository."
        ),
    )
    parser.add_argument(
        "--max-all-issues-and-pull-requests",
        type=int,
        default=None,
        help=(
            "Override the shared default admission limit for combined "
            "issue and pull-request roots (default: "
            f"{DEFAULT_ADMISSION_LIMITS.max_all_issues_and_pull_requests})."
        ),
    )
    parser.add_argument(
        "--max-closed-pull-requests",
        type=int,
        default=None,
        help=(
            "Override the shared default admission limit for closed "
            f"pull requests (default: "
            f"{DEFAULT_ADMISSION_LIMITS.max_closed_pull_requests})."
        ),
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        default=None,
        help=(
            "Override the shared default admission limit for commits "
            f"(default: {DEFAULT_ADMISSION_LIMITS.max_commits})."
        ),
    )
    parser.add_argument(
        "--max-tree-entries",
        type=int,
        default=None,
        help=(
            "Override the shared default admission limit for Git tree "
            f"entries (default: {DEFAULT_ADMISSION_LIMITS.max_tree_entries})."
        ),
    )
    parser.add_argument(
        "--max-source-count",
        type=int,
        default=None,
        help=(
            "Override the shared default hard cap on the actually-"
            "collected normalized source count (default: "
            f"{DEFAULT_RUNTIME_INGESTION_LIMITS.max_source_count})."
        ),
    )
    parser.add_argument(
        "--max-request-count",
        type=int,
        default=None,
        help=(
            "Override the shared default hard cap on actual GitHub "
            "requests spent collecting the corpus (default: "
            f"{DEFAULT_RUNTIME_INGESTION_LIMITS.max_github_request_count})."
        ),
    )
    return parser.parse_args(argv)


def _resolve_limits(
    args: argparse.Namespace,
) -> tuple[AdmissionLimits, RuntimeIngestionLimits]:
    """Merge the six optional CLI overrides onto the shared defaults.

    Each flag left unset (`None`) keeps its corresponding default value;
    a supplied flag overrides only that one value. Reconstructing rather
    than copying re-runs each model's own validation on the merged result.
    """
    admission_overrides = {
        "max_all_issues_and_pull_requests": args.max_all_issues_and_pull_requests,
        "max_closed_pull_requests": args.max_closed_pull_requests,
        "max_commits": args.max_commits,
        "max_tree_entries": args.max_tree_entries,
    }
    admission_limits = AdmissionLimits(
        **{
            **DEFAULT_ADMISSION_LIMITS.model_dump(),
            **{k: v for k, v in admission_overrides.items() if v is not None},
        }
    )

    runtime_overrides = {
        "max_source_count": args.max_source_count,
        "max_github_request_count": args.max_request_count,
    }
    runtime_limits = replace(
        DEFAULT_RUNTIME_INGESTION_LIMITS,
        **{k: v for k, v in runtime_overrides.items() if v is not None},
    )
    return admission_limits, runtime_limits


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.force:
        print(
            "Refusing to rebuild without --force: this replaces any existing "
            "compatible normalized-source snapshot for this repository at its "
            "resolved revision.",
            file=sys.stderr,
        )
        return 2

    identity = parse_github_repository_reference(args.repository)
    token = load_github_token()
    admission_limits, runtime_limits = _resolve_limits(args)

    with GitHubClient(
        token=token.get_secret_value() if token is not None else None
    ) as github_client:
        metadata = github_client.get_repository(identity)
        result = build_normalized_source_snapshot(
            metadata,
            github_client=github_client,
            snapshot_root=args.snapshot_root,
            admission_limits=admission_limits,
            runtime_limits=runtime_limits,
            force_rebuild=True,
        )

    print(
        f"Rebuilt normalized-source snapshot for {metadata.identity.repository} "
        f"at {result.manifest.resolved_commit_sha} "
        f"({result.manifest.source_count} sources)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
