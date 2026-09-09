"""Build, reuse, or explicitly rebuild the normalized-source snapshot
for one already-validated public repository.

Combines one-time revision resolution, normalized-snapshot lookup,
admission estimation/policy, complete corpus assembly, runtime ingestion-
limit enforcement, and atomic persistence into one typed workflow that
never returns a partial corpus or a partially-published snapshot.
Chunking, embeddings, and a searchable vector index are downstream concerns;
nothing here produces or claims them.

The revision is resolved exactly once, here, and that same
`resolved_commit_sha`/`resolved_tree_sha` pair is threaded through every
revision-sensitive step below (admission estimation, corpus assembly,
snapshot lookup, and snapshot publication) — no stage resolves its own,
possibly different, revision.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from reporationale.adapters.github import (
    GitHubClient,
    GitHubRepositoryMetadata,
    GitHubRequestBudgetExceeded,
)
from reporationale.adapters.snapshot_store import (
    SnapshotLookupResult,
    load_snapshot,
    look_up_normalized_source_snapshot,
    publish_snapshot,
    snapshot_directory,
)
from reporationale.application.admission import (
    DEFAULT_ADMISSION_LIMITS,
    DEFAULT_RUNTIME_INGESTION_LIMITS,
    AdmissionDecision,
    AdmissionLimits,
    RuntimeIngestionLimitExceeded,
    RuntimeIngestionLimits,
    check_runtime_ingestion_limits,
    estimate_repository_workload,
    evaluate_admission,
)
from reporationale.application.corpus import (
    RepositoryCorpus,
    assemble_repository_corpus,
)
from reporationale.application.progress import ProgressEvent, ProgressObserver
from reporationale.domain.snapshot import SnapshotManifest
from reporationale.domain.source_document import SourceDocument


class NormalizedSourceBuildRejected(Exception):
    """Admission rejected the repository's estimated workload before any
    collection was attempted.

    Distinct from an operational (`GitHubAdapterError`) failure: this
    means the estimate was successfully computed and compared against the
    supplied `AdmissionLimits`, and did not fit.
    """

    def __init__(self, decision: AdmissionDecision) -> None:
        super().__init__(decision.message)
        self.decision = decision


@dataclass(frozen=True)
class PhaseTiming:
    """Elapsed time and GitHub request count for one named workflow phase."""

    phase: str
    elapsed_seconds: float
    github_request_count: int


@dataclass(frozen=True)
class BuildMeasurement:
    """Lightweight, non-persisted build measurements for benchmarking.

    Totals are computed from `phases` on read rather than stored
    separately, so they can never drift out of sync with the per-phase
    values they summarize.
    """

    phases: tuple[PhaseTiming, ...]
    normalized_source_counts_by_type: dict[str, int]

    @property
    def total_elapsed_seconds(self) -> float:
        return sum(phase.elapsed_seconds for phase in self.phases)

    @property
    def total_github_request_count(self) -> int:
        return sum(phase.github_request_count for phase in self.phases)


@dataclass(frozen=True)
class NormalizedSourceBuildResult:
    """The typed outcome of one build/reuse/rebuild run. Fresh and reused
    results share this exact same shape."""

    manifest: SnapshotManifest
    documents: tuple[SourceDocument, ...]
    reused_existing_snapshot: bool
    measurement: BuildMeasurement


def _default_wall_clock() -> datetime:
    return datetime.now(UTC)


def _default_producer_version() -> str:
    try:
        return f"reporationale/{version('reporationale')}"
    except PackageNotFoundError:
        return "reporationale/0+unknown"


def _measure[T](
    phase: str,
    *,
    github_client: GitHubClient,
    monotonic_clock: Callable[[], float],
    action: Callable[[], T],
) -> tuple[T, PhaseTiming]:
    """Run `action`, timing it with `monotonic_clock` and counting how many
    GitHub requests it made, without recording anything else about it (no
    credential, header, or request/response object is ever in scope)."""
    start_time = monotonic_clock()
    start_requests = github_client.request_count
    result = action()
    elapsed_seconds = monotonic_clock() - start_time
    request_count = github_client.request_count - start_requests
    return result, PhaseTiming(
        phase=phase, elapsed_seconds=elapsed_seconds, github_request_count=request_count
    )


def build_normalized_source_snapshot(
    metadata: GitHubRepositoryMetadata,
    *,
    github_client: GitHubClient,
    snapshot_root: Path,
    admission_limits: AdmissionLimits | None = None,
    runtime_limits: RuntimeIngestionLimits | None = None,
    producer_version: str | None = None,
    force_rebuild: bool = False,
    wall_clock: Callable[[], datetime] = _default_wall_clock,
    monotonic_clock: Callable[[], float] = time.monotonic,
    on_progress: ProgressObserver | None = None,
) -> NormalizedSourceBuildResult:
    """Build, or reuse a compatible existing, normalized-source snapshot.

    `metadata` is the result of an already-completed
    `GitHubClient.get_repository` lookup. `admission_limits` and
    `runtime_limits` each default to the shared
    `reporationale.application.admission.DEFAULT_ADMISSION_LIMITS`/
    `DEFAULT_RUNTIME_INGESTION_LIMITS` when omitted; an explicit caller
    value always overrides its default. Steps: resolve the default
    branch exactly once; look up a compatible normalized-source snapshot
    at that revision (skipped entirely when `force_rebuild=True`); if
    compatible, reuse it; otherwise estimate admission workload and apply
    `admission_limits` (raising `NormalizedSourceBuildRejected` if the
    estimate does not fit), assemble the complete supported corpus, apply
    `runtime_limits` to what was actually collected (raising
    `RuntimeIngestionLimitExceeded` — publishing nothing — if exceeded),
    persist it atomically, and reload/validate the persisted result.

    `on_progress`, when supplied, is called with a `collecting_sources`
    then a `publishing_source_snapshot` `ProgressEvent` (each `"started"`
    then `"completed"` for a fresh build; `"completed"` alone, reporting
    the reused counts, when a compatible snapshot is reused) -- see
    `reporationale.application.progress` for the full contract. Omitted
    (the default), this function's behaviour is unchanged.

    Raises whatever `GitHubClient` or the snapshot store raises for any
    request, pagination, validation, or filesystem failure (propagated
    unchanged); never returns a partial corpus or a partially-published
    snapshot.
    """
    resolved_admission_limits = (
        admission_limits if admission_limits is not None else DEFAULT_ADMISSION_LIMITS
    )
    resolved_runtime_limits = (
        runtime_limits
        if runtime_limits is not None
        else DEFAULT_RUNTIME_INGESTION_LIMITS
    )
    resolved_producer_version = producer_version or _default_producer_version()
    phases: list[PhaseTiming] = []
    identity = metadata.identity

    (resolved_commit_sha, resolved_tree_sha), phase = _measure(
        "revision_resolution",
        github_client=github_client,
        monotonic_clock=monotonic_clock,
        action=lambda: github_client.resolve_commit_and_tree(
            identity, metadata.default_branch
        ),
    )
    phases.append(phase)

    lookup: SnapshotLookupResult = look_up_normalized_source_snapshot(
        root=snapshot_root,
        identity=identity,
        resolved_commit_sha=resolved_commit_sha,
        force_rebuild=force_rebuild,
    )

    if lookup.kind == "compatible":
        loaded, phase = _measure(
            "snapshot_reuse",
            github_client=github_client,
            monotonic_clock=monotonic_clock,
            action=lambda: load_snapshot(
                lookup.directory,
                expected_identity=identity,
                expected_commit_sha=resolved_commit_sha,
            ),
        )
        phases.append(phase)
        if on_progress is not None:
            reused_detail = f"reused {loaded.manifest.source_count:,} sources"
            on_progress(
                ProgressEvent(
                    phase="collecting_sources",
                    status="completed",
                    completed=loaded.manifest.source_count,
                    total=loaded.manifest.source_count,
                    detail=reused_detail,
                )
            )
            on_progress(
                ProgressEvent(
                    phase="publishing_source_snapshot",
                    status="completed",
                    detail=reused_detail,
                )
            )
        return NormalizedSourceBuildResult(
            manifest=loaded.manifest,
            documents=loaded.documents,
            reused_existing_snapshot=True,
            measurement=BuildMeasurement(
                phases=tuple(phases),
                normalized_source_counts_by_type=dict(
                    loaded.manifest.counts_by_source_type
                ),
            ),
        )

    estimate, phase = _measure(
        "admission_estimation",
        github_client=github_client,
        monotonic_clock=monotonic_clock,
        action=lambda: estimate_repository_workload(
            metadata=metadata,
            resolved_commit_sha=resolved_commit_sha,
            resolved_tree_sha=resolved_tree_sha,
            admission_limits=resolved_admission_limits,
            github_client=github_client,
        ),
    )
    phases.append(phase)

    decision = evaluate_admission(estimate, resolved_admission_limits)
    if not decision.admitted:
        raise NormalizedSourceBuildRejected(decision)

    # The request budget is scoped to exactly this collection call, so a
    # runaway pagination/redirect sequence is stopped mid-collection —
    # request `max_github_request_count + 1` is never sent — rather than
    # merely being detected afterwards. Repository lookup, revision
    # resolution, and admission estimation above are not charged against
    # it: the budget is entered here and cleared on exit regardless of
    # outcome, so it can never affect any other phase.
    if on_progress is not None:
        on_progress(ProgressEvent(phase="collecting_sources", status="started"))

    corpus: RepositoryCorpus | None = None
    collection_phase: PhaseTiming | None = None
    request_budget_exceeded = False
    try:
        with github_client.limit_requests(
            resolved_runtime_limits.max_github_request_count
        ):
            corpus, collection_phase = _measure(
                "source_collection",
                github_client=github_client,
                monotonic_clock=monotonic_clock,
                action=lambda: assemble_repository_corpus(
                    identity,
                    default_branch=metadata.default_branch,
                    resolved_commit_sha=resolved_commit_sha,
                    resolved_tree_sha=resolved_tree_sha,
                    github_client=github_client,
                ),
            )
    except GitHubRequestBudgetExceeded:
        request_budget_exceeded = True

    if request_budget_exceeded:
        # No partial corpus was ever collected far enough to reach
        # `check_runtime_ingestion_limits` below, so the same application-
        # level rejection reason is raised directly here instead; nothing
        # is published either way.
        raise RuntimeIngestionLimitExceeded(
            reason_code="exceeds_actual_request_count_limit",
            message=(
                "Collecting the complete corpus exceeded the runtime GitHub "
                "request-count limit."
            ),
            measured=resolved_runtime_limits.max_github_request_count + 1,
            limit=resolved_runtime_limits.max_github_request_count,
        )
    if corpus is None or collection_phase is None:
        raise AssertionError("unreachable: corpus or collection_phase must be set")
    phases.append(collection_phase)
    if on_progress is not None:
        on_progress(
            ProgressEvent(
                phase="collecting_sources",
                status="completed",
                completed=len(corpus.documents),
                total=len(corpus.documents),
                detail=f"{len(corpus.documents):,} sources collected",
            )
        )

    # Enforced on what was *actually* collected — catches whatever the
    # cheap admission estimate could not predict. Raised before any
    # publish call, so a corpus that exceeds a runtime limit is never
    # partially or fully written to disk.
    check_runtime_ingestion_limits(
        corpus,
        request_count=collection_phase.github_request_count,
        limits=resolved_runtime_limits,
    )

    if on_progress is not None:
        on_progress(ProgressEvent(phase="publishing_source_snapshot", status="started"))

    replace_existing = lookup.kind in ("rebuild_requested", "incompatible")
    _manifest, phase = _measure(
        "snapshot_publication",
        github_client=github_client,
        monotonic_clock=monotonic_clock,
        action=lambda: publish_snapshot(
            root=snapshot_root,
            repository=corpus.repository,
            default_branch=corpus.default_branch,
            resolved_commit_sha=corpus.resolved_commit_sha,
            documents=corpus.documents,
            counts_by_source_type=corpus.counts_by_source_type,
            producer_version=resolved_producer_version,
            force=replace_existing,
            clock=wall_clock,
        ),
    )
    phases.append(phase)

    published_directory = snapshot_directory(
        root=snapshot_root,
        identity=corpus.repository,
        resolved_commit_sha=corpus.resolved_commit_sha,
    )
    loaded, phase = _measure(
        "snapshot_validation",
        github_client=github_client,
        monotonic_clock=monotonic_clock,
        action=lambda: load_snapshot(
            published_directory,
            expected_identity=corpus.repository,
            expected_commit_sha=corpus.resolved_commit_sha,
        ),
    )
    phases.append(phase)
    if on_progress is not None:
        on_progress(
            ProgressEvent(
                phase="publishing_source_snapshot",
                status="completed",
                completed=loaded.manifest.source_count,
                total=loaded.manifest.source_count,
                detail=f"{loaded.manifest.source_count:,} sources published",
            )
        )

    return NormalizedSourceBuildResult(
        manifest=loaded.manifest,
        documents=loaded.documents,
        reused_existing_snapshot=False,
        measurement=BuildMeasurement(
            phases=tuple(phases),
            normalized_source_counts_by_type=dict(
                loaded.manifest.counts_by_source_type
            ),
        ),
    )


def rebuild_normalized_source_snapshot(
    metadata: GitHubRepositoryMetadata,
    *,
    github_client: GitHubClient,
    snapshot_root: Path,
    admission_limits: AdmissionLimits | None = None,
    runtime_limits: RuntimeIngestionLimits | None = None,
    producer_version: str | None = None,
    wall_clock: Callable[[], datetime] = _default_wall_clock,
    monotonic_clock: Callable[[], float] = time.monotonic,
    on_progress: ProgressObserver | None = None,
) -> NormalizedSourceBuildResult:
    """Explicit full normalized-source rebuild.

    An explicit caller choice, never automatic: bypasses compatible-
    snapshot reuse unconditionally, rebuilds the complete supported corpus,
    and publishes the replacement only after complete success and
    validation. A failed rebuild leaves the previous completed snapshot
    intact (see `reporationale.adapters.snapshot_store.publish_snapshot`).
    Replacement is scoped to exactly this one repository/revision snapshot;
    there is no incremental, background, partial, or cross-repository
    rebuild mode. `admission_limits`/`runtime_limits` default to the
    shared `DEFAULT_ADMISSION_LIMITS`/`DEFAULT_RUNTIME_INGESTION_LIMITS`
    (see `build_normalized_source_snapshot`) when omitted. `on_progress`
    is forwarded unchanged; see `build_normalized_source_snapshot`.
    """
    return build_normalized_source_snapshot(
        metadata,
        github_client=github_client,
        snapshot_root=snapshot_root,
        admission_limits=admission_limits,
        runtime_limits=runtime_limits,
        producer_version=producer_version,
        force_rebuild=True,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
        on_progress=on_progress,
    )
