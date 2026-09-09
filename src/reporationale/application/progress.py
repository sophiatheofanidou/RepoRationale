"""Optional, narrowly-scoped progress reporting for the indexing
workflows (`snapshot_workflow`, `chunk_snapshot`, `vector_retrieval`).

`ProgressObserver` is a pure UI/observability signal: passing one to a
workflow function never changes that function's own logic, return value,
or persisted artifacts, and every workflow call site defaults its observer
parameter to `None`, so an existing caller that never passes one keeps
working completely unchanged. `on_progress(event)` may be called any
number of times, in the fixed phase order below, and must never raise for
a well-behaved observer; a workflow does not catch an observer's own
exception.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

IndexingPhase = Literal[
    "collecting_sources",
    "publishing_source_snapshot",
    "creating_chunks",
    "embedding_chunks",
    "building_vector_index",
    "validating_vector_index",
]

# The fixed, meaningful order every `ProgressEvent` a caller receives across
# one indexing run follows -- a UI can render this as a static six-row
# checklist and place each incoming event by `phase` without guessing order.
INDEXING_PHASES: tuple[IndexingPhase, ...] = (
    "collecting_sources",
    "publishing_source_snapshot",
    "creating_chunks",
    "embedding_chunks",
    "building_vector_index",
    "validating_vector_index",
)


@dataclass(frozen=True)
class ProgressEvent:
    """One point-in-time signal about an indexing phase.

    `status="progress"` is only ever emitted mid-phase, for a phase that
    has meaningful sub-progress to report (currently only
    `embedding_chunks`, where `completed`/`total` count embedded chunks);
    `"started"` and `"completed"` bracket every phase. `completed`/`total`
    are the only counts this event ever carries -- exact, already-known
    quantities (chunks embedded, for instance), never an estimate or a
    time-based guess. `detail` is a short, human-readable, already-safe
    string (never raw repository or model content) for direct display.
    """

    phase: IndexingPhase
    status: Literal["started", "progress", "completed"]
    completed: int | None = None
    total: int | None = None
    detail: str | None = None


# The narrow callable shape every workflow's `on_progress` parameter
# accepts. A plain type alias, not a `Protocol`: nothing beyond
# call-with-one-`ProgressEvent`-argument is ever required of an observer.
ProgressObserver = Callable[[ProgressEvent], None]
