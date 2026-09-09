"""RepoRationale's Streamlit demonstration interface.

A thin presentation layer: every ingestion, retrieval, and answering
behaviour is delegated to the existing application workflows
(`reporationale.application.preflight`, `.readiness`, `.snapshot_workflow`,
`.chunk_snapshot`, `.vector_retrieval`, `.answering_workflow`) through the
composition root in `reporationale.composition`. This module never calls
GitHub, Voyage, Anthropic, or Chroma directly.

Visual structure and copy follow the approved design reference,
`.local/mockups/streamlit-m6/session-memory.html`, as closely as native
Streamlit widgets and `static/theme.css`'s `data-testid` styling allow;
see that file's own module docstring for exactly which selectors are
confirmed live versus inferred by naming convention.

Session (`st.session_state`) ownership implements bounded, session-scoped
conversational follow-up support: at most the three most recent completed
chat turns are supplied as `ConversationContext` to each new question, and
that context (along with the open vector-index handle and the visible
chat history) is reset on a repository change, an index rebuild, an
explicit "New investigation", or "Clear chat" -- never persisted beyond
this running process.

Run with `uv run streamlit run src/reporationale/streamlit_app.py`.
"""

from __future__ import annotations

import html
import logging
import re
import time
from importlib.resources import files
from typing import Any

import streamlit as st
from pydantic import ValidationError
from pygments import highlight  # type: ignore[import-untyped]
from pygments.formatters import HtmlFormatter  # type: ignore[import-untyped]
from pygments.lexers import TextLexer, get_lexer_by_name  # type: ignore[import-untyped]
from pygments.util import ClassNotFound  # type: ignore[import-untyped]

from reporationale import composition
from reporationale.adapters.anthropic_answering import AnthropicAnswerError
from reporationale.adapters.github import GitHubAdapterError, GitHubRepositoryMetadata
from reporationale.adapters.snapshot_store import SnapshotStoreError, snapshot_directory
from reporationale.adapters.voyage_embeddings import VoyageEmbeddingError
from reporationale.application.admission import RuntimeIngestionLimitExceeded
from reporationale.application.answering_workflow import (
    AnsweringWorkflowError,
    answer_question,
)
from reporationale.application.chunk_snapshot import build_chunk_snapshot
from reporationale.application.preflight import (
    PreflightUnavailable,
    inspect_repository_reference,
)
from reporationale.application.progress import (
    INDEXING_PHASES,
    IndexingPhase,
    ProgressEvent,
)
from reporationale.application.readiness import (
    RepositoryReadiness,
    check_repository_readiness,
)
from reporationale.application.snapshot_workflow import (
    NormalizedSourceBuildRejected,
    build_normalized_source_snapshot,
    rebuild_normalized_source_snapshot,
)
from reporationale.application.vector_retrieval import (
    SearchHistoryContractError,
    SearchHistoryService,
    load_search_history_service,
)
from reporationale.config import Settings, load_settings
from reporationale.domain.answering import (
    AnsweredOutcome,
    AnswerOutcome,
    Citation,
    ConversationContext,
    ConversationTurn,
    RunTrace,
)
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.retrieval import RankedEvidence

_logger = logging.getLogger(__name__)

_SNAPSHOT_ROOT = composition.resolve_snapshot_root()
_MAX_CONTEXT_TURNS = 3
# CSS class names, matching `static/theme.css`'s `.rr-dot-*` rules -- not
# colors: the colors themselves live in the stylesheet, so this file only
# ever names which state a stage is in. "checking" is not a persisted
# `session_state.stage`; it is painted transiently (see
# `_render_setup_section`) while `_handle_check` runs.
_STATE_DOT_CLASSES = {
    "idle": "rr-dot-idle",
    "checking": "rr-dot-checking",
    "unsupported": "rr-dot-unsupported",
    "operational_failure": "rr-dot-operational_failure",
    "needs_indexing": "rr-dot-needs_indexing",
    "ready": "rr-dot-ready",
}


def _inject_custom_css() -> None:
    css = (
        files("reporationale")
        .joinpath("static", "theme.css")
        .read_text(encoding="utf-8")
    )
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def _dots_html() -> str:
    """The mock's pulsing three-dot "still working" indicator (see
    `static/theme.css`'s `.rr-dot-loader`/`@keyframes rr-dot-pulse`). Pure
    CSS animation: once this HTML reaches the browser it keeps animating
    even while this module's Python is blocked on a real GitHub/Voyage
    call, the same way `st.spinner` already does."""
    return '<span class="rr-dot-loader"><span></span><span></span><span></span></span>'


def _init_state() -> None:
    defaults: dict[str, Any] = {
        "stage": "idle",
        "repo_input": "",
        "identity": None,
        "metadata": None,
        "resolved_commit_sha": None,
        "readiness": None,
        "search_service": None,
        "chat_turns": [],
        "banner_message": None,
        "confirm_rebuild": False,
        "ask_error": None,
        "question_input_revision": 0,
        "question_input_value": "",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _close_search_service() -> None:
    service: SearchHistoryService | None = st.session_state.get("search_service")
    if service is not None:
        service.close()
    st.session_state.search_service = None


def _replace_question_input(value: str) -> None:
    """Render the next composer with a fresh widget identity and value.

    Streamlit forms can clear a text box in the browser while retaining the
    keyed widget's previous server-side value. Re-selecting the same starter
    question then produces no state change. A monotonically increasing key
    makes every programmatic replacement explicit, including an identical
    suggestion selected after Clear chat or a new investigation.
    """
    revision = int(st.session_state.get("question_input_revision", 0))
    st.session_state.question_input_revision = revision + 1
    st.session_state.question_input_value = value


def _reset_investigation() -> None:
    _close_search_service()
    st.session_state.stage = "idle"
    st.session_state.identity = None
    st.session_state.metadata = None
    st.session_state.resolved_commit_sha = None
    st.session_state.readiness = None
    st.session_state.chat_turns = []
    st.session_state.banner_message = None
    st.session_state.confirm_rebuild = False
    st.session_state.ask_error = None
    st.session_state.repo_input = ""
    _replace_question_input("")


def _clear_chat() -> None:
    """Clear the visible chat history. Also clears the follow-up context,
    since keeping context for a turn the user can no longer see would let
    an invisible prior turn resolve a pronoun with nothing on screen to
    explain why -- the same "implies continuity it cannot honor" problem a
    visible-history-only extreme would create."""
    st.session_state.chat_turns = []
    st.session_state.ask_error = None
    _replace_question_input("")


def _load_settings() -> Settings | None:
    try:
        return load_settings()
    except ValidationError:
        return None


def _short_sha(sha: str) -> str:
    return sha[:8]


_LOCAL_SNAPSHOT_PROBLEM_MESSAGE = (
    "The local snapshot for this repository could not be read (it may be "
    "corrupted or from an incompatible build). Indexing will rebuild "
    "whatever is missing or invalid. Full details were written to the "
    "local logs."
)
_VECTOR_INDEX_OPEN_PROBLEM_MESSAGE = (
    "The local vector index for this repository could not be opened. "
    "Indexing will attempt to rebuild it. Full details were written to "
    "the local logs."
)


def _handle_check(raw: str, settings: Settings) -> None:
    _close_search_service()
    st.session_state.chat_turns = []
    st.session_state.banner_message = None
    st.session_state.confirm_rebuild = False

    with composition.build_github_client(settings) as client:
        try:
            inspection = inspect_repository_reference(
                raw, github_client=client, snapshot_root=_SNAPSHOT_ROOT
            )
        except PreflightUnavailable as error:
            st.session_state.stage = "operational_failure"
            st.session_state.banner_message = error.message
            return

        preflight = inspection.result
        if preflight.status == "unsupported":
            st.session_state.stage = "unsupported"
            st.session_state.banner_message = preflight.message
            return

        identity = preflight.repository
        metadata = inspection.metadata
        resolved_commit_sha = inspection.resolved_commit_sha
        # Guaranteed by a non-"unsupported" result reached with
        # `snapshot_root` supplied (always the case here): the workflow
        # only reaches this outcome after a successful repository lookup
        # and revision resolution, both already reused above rather than
        # repeated.
        assert identity is not None
        assert metadata is not None
        assert resolved_commit_sha is not None

    try:
        readiness = check_repository_readiness(
            identity=identity,
            resolved_commit_sha=resolved_commit_sha,
            snapshot_root=_SNAPSHOT_ROOT,
            max_chars=composition.CHUNK_MAX_CHARS,
        )
    except SnapshotStoreError:
        _logger.exception("Reading the local snapshot for readiness failed.")
        st.session_state.identity = identity
        st.session_state.metadata = metadata
        st.session_state.resolved_commit_sha = resolved_commit_sha
        st.session_state.readiness = None
        st.session_state.stage = "needs_indexing"
        st.session_state.banner_message = _LOCAL_SNAPSHOT_PROBLEM_MESSAGE
        return

    st.session_state.identity = identity
    st.session_state.metadata = metadata
    st.session_state.resolved_commit_sha = resolved_commit_sha
    st.session_state.readiness = readiness

    if not readiness.ready:
        st.session_state.stage = "needs_indexing"
        return

    try:
        _open_search_service(settings, readiness)
    except SnapshotStoreError:
        _logger.exception("Opening the existing vector index failed.")
        st.session_state.stage = "needs_indexing"
        st.session_state.banner_message = _VECTOR_INDEX_OPEN_PROBLEM_MESSAGE
        return
    st.session_state.stage = "ready"


def _open_search_service(settings: Settings, readiness: RepositoryReadiness) -> None:
    embedding_provider = composition.build_embedding_provider(settings)
    st.session_state.search_service = load_search_history_service(
        snapshot_dir=readiness.snapshot_dir,
        max_chars=composition.CHUNK_MAX_CHARS,
        embedding_provider=embedding_provider,
    )


_PHASE_LABELS: dict[IndexingPhase, str] = {
    "collecting_sources": "Collecting and normalizing sources",
    "publishing_source_snapshot": "Publishing the source snapshot",
    "creating_chunks": "Splitting into searchable chunks",
    "embedding_chunks": "Creating embeddings",
    "building_vector_index": "Building the vector index",
    "validating_vector_index": "Validating the persisted index",
}


class _IndexingProgressPainter:
    """Tracks the six-phase indexing checklist and a truthful completion
    percentage across one `_run_indexing_pipeline` call, derived only from
    the `ProgressEvent`s the application workflows actually emit -- a
    completed phase always counts as one whole unit out of six, and the
    only fractional credit within a phase comes from the expensive
    `embedding_chunks` phase's own real batch-completion counts. Never a
    timer or an otherwise fabricated number.
    """

    def __init__(self) -> None:
        self._states: dict[IndexingPhase, str] = {
            phase: "pending" for phase in INDEXING_PHASES
        }
        self._details: dict[IndexingPhase, str] = {
            phase: "waiting" for phase in INDEXING_PHASES
        }
        self._embedding_fraction = 0.0

    def apply(self, event: ProgressEvent) -> None:
        phase_index = INDEXING_PHASES.index(event.phase)
        for earlier in INDEXING_PHASES[:phase_index]:
            self._states[earlier] = "done"

        if event.status == "completed":
            self._states[event.phase] = "done"
            self._details[event.phase] = event.detail or "done"
        else:
            self._states[event.phase] = "active"
            if event.detail:
                self._details[event.phase] = event.detail
            elif event.completed is not None and event.total:
                self._details[event.phase] = f"{event.completed:,} of {event.total:,}"
            else:
                self._details[event.phase] = "starting…"

        if event.phase == "embedding_chunks":
            if event.status == "completed":
                self._embedding_fraction = 1.0
            elif event.total:
                self._embedding_fraction = (event.completed or 0) / event.total
            else:
                self._embedding_fraction = 0.0

    def rows(self) -> list[tuple[str, str, str]]:
        return [
            (self._states[phase], _PHASE_LABELS[phase], self._details[phase])
            for phase in INDEXING_PHASES
        ]

    def percent(self) -> int:
        done_count = sum(
            1 for phase in INDEXING_PHASES if self._states[phase] == "done"
        )
        fractional = (
            self._embedding_fraction
            if self._states["embedding_chunks"] == "active"
            else 0.0
        )
        return round((done_count + fractional) / len(INDEXING_PHASES) * 100)


def _phase_row_html(state: str, name: str, detail: str) -> str:
    if state == "done":
        icon = '<span class="rr-phase-icon rr-phase-done">✓</span>'
        name_class = "rr-phase-name-done"
    elif state == "active":
        icon = (
            '<span class="rr-phase-icon rr-phase-active">'
            '<span class="rr-phase-active-inner"></span></span>'
        )
        name_class = "rr-phase-name-active"
    else:
        icon = '<span class="rr-phase-icon rr-phase-pending"></span>'
        name_class = "rr-phase-name-pending"
    return (
        '<div class="rr-phase-row"><span class="rr-phase-name">'
        f'{icon}<span class="{name_class}">{html.escape(name)}</span></span>'
        f'<span class="rr-phase-detail">{html.escape(detail)}</span></div>'
    )


def _elapsed_label(elapsed_seconds: float) -> str:
    total_seconds = max(0, round(elapsed_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"elapsed {minutes}m {seconds:02d}s" if minutes else f"elapsed {seconds}s"


def _progress_view_html(
    heading: str, painter: _IndexingProgressPainter, *, elapsed_seconds: float = 0.0
) -> str:
    percent = painter.percent()
    rows = "".join(
        _phase_row_html(state, name, detail) for state, name, detail in painter.rows()
    )
    return (
        f'<div class="rr-progress-view"><div class="rr-progress-heading">'
        f"{html.escape(heading)}</div>"
        f'<div class="rr-phase-list">{rows}</div>'
        f'<div class="rr-progress-bar-track">'
        f'<div class="rr-progress-bar-fill" style="width:{percent}%"></div></div>'
        f'<div class="rr-progress-meta"><span>{percent}% complete</span>'
        f"<span>{_elapsed_label(elapsed_seconds)}</span></div></div>"
    )


def _run_indexing_pipeline(
    settings: Settings,
    metadata: GitHubRepositoryMetadata,
    *,
    force_rebuild: bool,
    status_placeholder: Any,
) -> bool:
    """Run (or reuse) normalized-source, chunk, and vector-index building,
    painting a live six-phase progress view (matching the mock's pulsing
    active-phase indicator and percentage bar) into a dedicated placeholder
    as the application workflows report real `ProgressEvent`s. Returns
    `True` on success; on a handled failure it records a banner message,
    sets the matching stage, and returns `False` without raising.
    """
    st.session_state.banner_message = None
    repo = metadata.identity.repository
    action = "Rebuilding" if force_rebuild else "Indexing"
    heading = f"{action} {repo}"
    painter = _IndexingProgressPainter()
    progress_placeholder = st.empty()
    started_at = time.monotonic()

    def repaint() -> None:
        percent = painter.percent()
        status_placeholder.markdown(
            f'<div class="rr-status-line-text"><span class="rr-dot rr-dot-indexing">'
            f'</span><span class="rr-status-copy">{html.escape(repo)} · '
            f"{action.lower()} {percent}%{_dots_html()}</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        progress_placeholder.markdown(
            _progress_view_html(
                heading, painter, elapsed_seconds=time.monotonic() - started_at
            ),
            unsafe_allow_html=True,
        )

    def on_progress(event: ProgressEvent) -> None:
        painter.apply(event)
        repaint()

    repaint()

    try:
        with composition.build_github_client(settings) as client:
            if force_rebuild:
                build_result = rebuild_normalized_source_snapshot(
                    metadata,
                    github_client=client,
                    snapshot_root=_SNAPSHOT_ROOT,
                    on_progress=on_progress,
                )
            else:
                build_result = build_normalized_source_snapshot(
                    metadata,
                    github_client=client,
                    snapshot_root=_SNAPSHOT_ROOT,
                    on_progress=on_progress,
                )

        snapshot_dir = snapshot_directory(
            root=_SNAPSHOT_ROOT,
            identity=metadata.identity,
            resolved_commit_sha=build_result.manifest.resolved_commit_sha,
        )
        build_chunk_snapshot(
            snapshot_dir=snapshot_dir,
            max_chars=composition.CHUNK_MAX_CHARS,
            force_rebuild=force_rebuild,
            on_progress=on_progress,
        )

        embedding_provider = composition.build_embedding_provider(settings)
        search_service = load_search_history_service(
            snapshot_dir=snapshot_dir,
            max_chars=composition.CHUNK_MAX_CHARS,
            embedding_provider=embedding_provider,
            force_rebuild=force_rebuild,
            on_progress=on_progress,
        )
    except NormalizedSourceBuildRejected as error:
        st.session_state.stage = "unsupported"
        st.session_state.banner_message = error.decision.message
        return False
    except RuntimeIngestionLimitExceeded as error:
        st.session_state.stage = "unsupported"
        st.session_state.banner_message = error.message
        return False
    except GitHubAdapterError as error:
        _logger.exception("Indexing failed due to a GitHub adapter error.")
        st.session_state.stage = "operational_failure"
        st.session_state.banner_message = str(error)
        return False
    except OSError as error:
        # Covers a transient local filesystem/permission failure while
        # publishing a snapshot artifact -- observed in practice as a
        # Windows `PermissionError` renaming the freshly built vector
        # index into place (see `adapters.snapshot_store`'s bounded
        # rename retry, which already absorbs most transient cases; this
        # is the fallback for whatever a bounded retry does not resolve).
        # Never surfaced as a raw traceback to the UI; always logged.
        _logger.exception(
            "Indexing failed while publishing local snapshot artifacts: %s", error
        )
        st.session_state.stage = "needs_indexing"
        st.session_state.banner_message = (
            "Indexing failed while saving the local index (a filesystem "
            "or permission problem, often transient on Windows). Sources "
            "and chunks already collected were kept; you can retry "
            "indexing. Full details were written to the local logs."
        )
        return False
    except SnapshotStoreError as error:
        _logger.exception("Indexing failed due to a local snapshot-store problem.")
        st.session_state.stage = "needs_indexing"
        st.session_state.banner_message = (
            "Indexing failed while reading or writing a local artifact "
            f"(a data-integrity problem: {error}). You can retry "
            "indexing. Full details were written to the local logs."
        )
        return False
    except VoyageEmbeddingError as error:
        _logger.exception("Indexing failed due to a Voyage embedding problem.")
        st.session_state.stage = "needs_indexing"
        st.session_state.banner_message = (
            f"Indexing failed while creating embeddings ({error}). Sources "
            "and chunks already collected were kept; you can retry "
            "indexing. Full details were written to the local logs."
        )
        return False

    st.session_state.resolved_commit_sha = build_result.manifest.resolved_commit_sha
    st.session_state.chat_turns = []
    try:
        readiness = check_repository_readiness(
            identity=metadata.identity,
            resolved_commit_sha=build_result.manifest.resolved_commit_sha,
            snapshot_root=_SNAPSHOT_ROOT,
            max_chars=composition.CHUNK_MAX_CHARS,
        )
    except SnapshotStoreError:
        _logger.exception("Re-checking readiness after a completed build failed.")
        search_service.close()
        st.session_state.stage = "needs_indexing"
        st.session_state.banner_message = _LOCAL_SNAPSHOT_PROBLEM_MESSAGE
        return False

    st.session_state.search_service = search_service
    st.session_state.readiness = readiness
    st.session_state.stage = "ready"
    return True


def _handle_start_indexing(settings: Settings, status_placeholder: Any) -> None:
    metadata = st.session_state.metadata
    _run_indexing_pipeline(
        settings, metadata, force_rebuild=False, status_placeholder=status_placeholder
    )


def _handle_rebuild(settings: Settings, status_placeholder: Any) -> None:
    metadata = st.session_state.metadata
    _close_search_service()
    st.session_state.confirm_rebuild = False
    _run_indexing_pipeline(
        settings, metadata, force_rebuild=True, status_placeholder=status_placeholder
    )


class _EvidenceRecordingSearchHistory:
    """Wraps a `SearchHistoryService` to additionally capture every
    `RankedEvidence` a run actually retrieves, in first-seen order.

    Needed only so the UI can show which sources were found but judged
    insufficient (the mock's "related sources found" list) -- it never
    changes what `search_history` returns or what `answer_question` sees;
    it only observes. `answer_question` depends structurally on a plain
    `search_history(query) -> tuple[RankedEvidence, ...]` method
    (`SearchHistoryCapability`), which this satisfies without importing
    that protocol.
    """

    def __init__(self, inner: SearchHistoryService) -> None:
        self._inner = inner
        self.seen: dict[str, RankedEvidence] = {}

    def search_history(self, query: str) -> tuple[RankedEvidence, ...]:
        results = self._inner.search_history(query)
        for result in results:
            self.seen.setdefault(result.evidence_id, result)
        return results


def _handle_ask(settings: Settings, question: str) -> None:
    search_service = st.session_state.search_service
    recorder = _EvidenceRecordingSearchHistory(search_service)
    recent_turns = st.session_state.chat_turns[-_MAX_CONTEXT_TURNS:]
    context = (
        ConversationContext(
            turns=tuple(
                ConversationTurn.from_outcome(
                    question=turn["question"], outcome=turn["outcome"]
                )
                for turn in recent_turns
            )
        )
        if recent_turns
        else None
    )
    model = composition.build_answering_model(settings)
    try:
        run_result = answer_question(
            question, search_history=recorder, model=model, context=context
        )
    except (
        AnsweringWorkflowError,
        AnthropicAnswerError,
        SearchHistoryContractError,
        SnapshotStoreError,
        VoyageEmbeddingError,
    ) as error:
        _logger.exception("Answering failed.")
        st.session_state.ask_error = str(error)
        return

    st.session_state.ask_error = None
    st.session_state.chat_turns.append(
        {
            "question": question,
            "outcome": run_result.outcome,
            "trace": run_result.trace,
            "context_turns_used": len(recent_turns),
            "retrieved_evidence": tuple(recorder.seen.values()),
        }
    )


def _ref_row_html(label: str, value: str, dot_class: str) -> str:
    return (
        '<div class="rr-ref-row"><span class="rr-ref-key">'
        f'<span class="rr-dot {html.escape(dot_class)}"></span>'
        f"{html.escape(label)}</span>"
        f'<span class="rr-ref-val">{html.escape(value)}</span></div>'
    )


def _panel_html(title: str, items: list[str], *, open_by_default: bool = False) -> str:
    open_attr = " open" if open_by_default else ""
    items_html = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return (
        f'<details class="rr-panel"{open_attr}>'
        f"<summary>{html.escape(title)}</summary>"
        f"<ul>{items_html}</ul></details>"
    )


def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown(
            '<div class="rr-brand-top">'
            '<div class="rr-brand-mark">RR</div>'
            '<div class="rr-brand-name">RepoRationale</div>'
            "</div>"
            '<div class="rr-brand-tagline">'
            "Ask why the code changed. Get the evidence behind it."
            "</div>",
            unsafe_allow_html=True,
        )

        stage = st.session_state.stage
        identity: RepositoryIdentity | None = st.session_state.identity
        readiness: RepositoryReadiness | None = st.session_state.readiness
        dot_class = _STATE_DOT_CLASSES.get(stage, "rr-dot-idle")

        rows = [
            _ref_row_html(
                "Repository",
                identity.repository if identity is not None else "none selected",
                dot_class,
            ),
            _ref_row_html(
                "Status",
                {
                    "idle": "no repository",
                    "unsupported": "unsupported",
                    "operational_failure": "temporarily unavailable",
                    "needs_indexing": "indexing required",
                    "ready": "ready",
                }.get(stage, stage),
                dot_class,
            ),
        ]
        if readiness is not None and readiness.source_manifest is not None:
            rows.append(
                _ref_row_html(
                    "Snapshot",
                    _short_sha(readiness.source_manifest.resolved_commit_sha),
                    dot_class,
                )
            )
            rows.append(
                _ref_row_html(
                    "Sources",
                    f"{readiness.source_manifest.source_count:,}",
                    dot_class,
                )
            )
        st.markdown(
            '<div class="rr-panel-label">Current state</div>'
            f'<div class="rr-ref-block">{"".join(rows)}</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            _panel_html(
                "What it does",
                [
                    "Retrieves and reads through a public GitHub "
                    "repository's issues, pull requests, commits, and "
                    "markdown docs.",
                    "Answers questions about why things were built the "
                    "way they were, not just what the code does today, "
                    "with links back to the original source.",
                    "If the history doesn't explain something, it says so "
                    "instead of guessing.",
                    "Very large repositories or very long histories may "
                    "not be fully supported.",
                ],
                open_by_default=True,
            )
            + _panel_html(
                "How it works",
                [
                    "Enter a public repository and start indexing.",
                    "Wait for it to finish. This only happens once per repository.",
                    "Ask your question.",
                    "Ask follow-ups if you like. They can build on what "
                    "you just asked.",
                ],
            )
            + _panel_html(
                "Good to know",
                [
                    "It works with one repository at a time. Picking a "
                    "new one starts fresh.",
                    "Only public repositories are supported.",
                    "Follow-up context is temporary. It resets when you "
                    "refresh, switch repositories, rebuild the index, or "
                    "start a new investigation.",
                ],
            ),
            unsafe_allow_html=True,
        )

        st.markdown(
            '<div class="rr-sidebar-footnote">'
            "grounded in repository history · linked evidence · honest uncertainty"
            "</div>",
            unsafe_allow_html=True,
        )


def _render_home_fab() -> None:
    """Offer a compact way back without consuming sidebar header space."""
    if st.session_state.stage == "idle":
        return
    if st.button(
        "Home",
        key="home_button",
        help="Choose another repository",
        icon=":material/home:",
    ):
        _reset_investigation()
        st.rerun()


def _render_missing_credentials_screen() -> None:
    st.markdown(
        '<div class="rr-brand-top">'
        '<div class="rr-brand-mark">RR</div>'
        '<div class="rr-brand-name">RepoRationale</div>'
        "</div>",
        unsafe_allow_html=True,
    )
    st.error(
        "Missing or invalid local credentials. Copy `.env.example` to "
        "`.env` and fill in your GitHub token, Voyage API key, and "
        "Anthropic API key, then restart the app."
    )


def _status_dot_and_text_html(stage: str) -> str:
    identity: RepositoryIdentity | None = st.session_state.identity
    readiness: RepositoryReadiness | None = st.session_state.readiness
    dot_class = _STATE_DOT_CLASSES.get(stage, "rr-dot-idle")

    if (
        stage == "ready"
        and readiness is not None
        and readiness.source_manifest is not None
        and identity is not None
    ):
        manifest = readiness.source_manifest
        text = (
            f"{identity.repository} · ready · snapshot "
            f"{_short_sha(manifest.resolved_commit_sha)} · "
            f"{manifest.source_count:,} sources"
        )
    elif stage == "needs_indexing" and identity is not None:
        text = f"{identity.repository} · indexing required"
    elif stage == "unsupported":
        text = "Unsupported repository"
    elif stage == "operational_failure":
        text = "Temporarily unavailable"
    else:
        text = "No repository selected"

    return (
        f'<span class="rr-dot {dot_class}"></span>'
        f'<span class="rr-status-copy" title="{html.escape(text)}">'
        f"{html.escape(text)}</span>"
    )


def _write_status_line(placeholder: Any, stage: str) -> None:
    placeholder.markdown(
        f'<div class="rr-status-line-text">{_status_dot_and_text_html(stage)}</div>',
        unsafe_allow_html=True,
    )


def _render_status_row() -> tuple[Any, bool]:
    """Render the status line: text on the left, and -- only once ready,
    matching the mock's `.status-line-actions` sitting inline with the
    status text rather than on their own row below it -- Rebuild index /
    New investigation tight against the right edge of that same row.
    ("Clear chat" lives beside the composer instead; see
    `_render_ready_content`, since it acts on the chat below, not on the
    repository/snapshot this status line describes.)

    Returns the placeholder the status text was written to (for later
    live updates during checking/indexing) and `True` exactly when the
    user just confirmed a rebuild -- the caller runs the actual rebuild
    pipeline itself, outside this function's `st.columns` layout.
    """
    stage = st.session_state.stage
    with st.container(key="status_row"):
        if stage == "ready":
            col_text, col_actions = st.columns(
                [3, 2], gap="small", vertical_alignment="center"
            )
            with col_actions:
                with st.container(
                    horizontal=True,
                    horizontal_alignment="right",
                    vertical_alignment="center",
                    gap="small",
                ):
                    rebuild_clicked = st.button(
                        "Rebuild index",
                        key="rebuild_index_button",
                        type="primary",
                        width="content",
                    )
                    new_investigation_clicked = st.button(
                        "New investigation",
                        key="new_investigation_button",
                        type="primary",
                        width="content",
                    )
        else:
            col_text = st.container()
            rebuild_clicked = new_investigation_clicked = False

        status_placeholder = col_text.empty()
        _write_status_line(status_placeholder, stage)

    if rebuild_clicked:
        st.session_state.confirm_rebuild = True
        st.rerun()
    if new_investigation_clicked:
        _reset_investigation()
        st.rerun()

    confirmed = False
    if st.session_state.confirm_rebuild:
        st.markdown(
            '<div class="rr-confirm-panel">'
            '<div class="rr-info-card-title">Rebuild index</div>'
            '<div class="rr-info-card-body">Rebuilding replaces this '
            "repository's local snapshot with a freshly collected one. "
            "This can take a while for a large repository, and a failed "
            "rebuild never replaces a working snapshot. Starting a "
            "rebuild also begins a fresh conversation.</div></div>",
            unsafe_allow_html=True,
        )
        confirm_col, _spacer, cancel_col = st.columns([1.5, 5, 1], gap="small")
        with confirm_col:
            confirmed = st.button(
                "Yes, rebuild",
                key="confirm_rebuild_button",
                type="primary",
                width="stretch",
            )
        with cancel_col:
            cancelled = st.button(
                "Cancel", key="cancel_rebuild_button", width="stretch"
            )
        if cancelled:
            st.session_state.confirm_rebuild = False
            st.rerun()
    return status_placeholder, confirmed


def _render_setup_section(settings: Settings, status_placeholder: Any) -> None:
    """The repository setup card: always shown until the repository is
    ready, matching the mock's `.setup-card` (present in the initial,
    checking, unsupported, and index-required states alike), with a
    contextual info card for whichever outcome the last check produced."""
    # A plain styled `<div>`, not `<h1>`: Streamlit auto-adds a hover
    # anchor-link icon to every literal heading tag it renders (even one
    # injected via raw HTML), which is what made this look clickable.
    st.markdown(
        '<div class="rr-setup-heading">Get started</div>', unsafe_allow_html=True
    )
    st.markdown(
        '<p class="rr-setup-sub">Enter a public GitHub repository to check '
        "whether it's ready, needs indexing, or isn't supported.</p>",
        unsafe_allow_html=True,
    )

    with st.form("repository_form", clear_on_submit=False):
        col_input, col_button = st.columns([4, 1])
        with col_input:
            raw = st.text_input(
                "Public GitHub repository",
                value=st.session_state.repo_input,
                placeholder="owner/repository",
                label_visibility="collapsed",
            )
        with col_button:
            submitted = st.form_submit_button("Check", type="primary", width="stretch")
    st.session_state.repo_input = raw

    stage = st.session_state.stage
    if stage == "unsupported" and st.session_state.banner_message:
        st.markdown(
            '<div class="rr-info-card rr-variant-danger">'
            '<div class="rr-info-card-title">Unsupported repository</div>'
            f'<div class="rr-info-card-body">'
            f"{html.escape(st.session_state.banner_message)}</div></div>",
            unsafe_allow_html=True,
        )
    elif stage == "operational_failure" and st.session_state.banner_message:
        st.markdown(
            '<div class="rr-info-card rr-variant-danger">'
            '<div class="rr-info-card-title">Temporarily unavailable</div>'
            f'<div class="rr-info-card-body">'
            f"{html.escape(st.session_state.banner_message)}</div></div>",
            unsafe_allow_html=True,
        )
    elif stage == "needs_indexing":
        identity: RepositoryIdentity = st.session_state.identity
        problem_message = st.session_state.banner_message
        if problem_message:
            # A previous indexing attempt (or the readiness check right
            # before it) failed in a recoverable way -- the repository and
            # its already-resolved metadata are still valid, so a real
            # retry is offered here rather than the plain "not indexed
            # yet" card below.
            st.markdown(
                '<div class="rr-info-card rr-variant-danger">'
                '<div class="rr-info-card-title">Indexing problem</div>'
                f'<div class="rr-info-card-body">{html.escape(problem_message)}'
                "</div></div>",
                unsafe_allow_html=True,
            )
            button_label = "Retry indexing"
        else:
            st.markdown(
                '<div class="rr-info-card rr-variant-warn">'
                '<div class="rr-info-card-title">Indexing required</div>'
                '<div class="rr-info-card-body">No compatible local snapshot '
                f"was found for <code>{html.escape(identity.repository)}</code>. "
                "Indexing collects its complete supported history once and "
                "reuses the result afterward.</div></div>",
                unsafe_allow_html=True,
            )
            button_label = "Start indexing"
        start_indexing = st.button(button_label, type="primary")
        if start_indexing:
            _handle_start_indexing(settings, status_placeholder)
            st.rerun()

    if submitted and raw.strip():
        checking_repo = raw.strip()
        status_placeholder.markdown(
            '<div class="rr-status-line-text"><span class="rr-dot rr-dot-checking">'
            f"</span>Checking {html.escape(checking_repo)}{_dots_html()}</div>",
            unsafe_allow_html=True,
        )
        _handle_check(checking_repo, settings)
        st.rerun()


_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
_CITATION_MARKER_PATTERN = re.compile(r"\[(\d+)\]")
_FENCED_CODE_PATTERN = re.compile(
    r"```(?P<language>[A-Za-z0-9_+.-]*)[^\S\r\n]*\r?\n"
    r"(?P<code>.*?)```",
    re.DOTALL,
)
_CODE_FORMATTER = HtmlFormatter(nowrap=True)


def _answer_html(answer: str) -> str:
    """Escape model-generated answer text first, then apply only the
    formatting this module itself controls (bold, citation markers) as
    our own styled tags -- never raw, unescaped model or repository text."""
    escaped = html.escape(answer)
    escaped = _BOLD_PATTERN.sub(r"<strong>\1</strong>", escaped)
    escaped = _CITATION_MARKER_PATTERN.sub(
        r'<span class="rr-cite-mark">[\1]</span>', escaped
    )
    return escaped.replace("\n", "<br>")


def _citation_prose_html(text: str) -> str:
    """Render repository prose literally inside the citation card."""
    escaped = html.escape(html.unescape(text)).replace("`", "&#96;")
    return f'<div class="rr-citation-prose">{escaped}</div>'


def _citation_excerpt_html(excerpt: str) -> str:
    """Render fenced code with highlighting without exposing Markdown fences.

    The surrounding card still goes through ``st.markdown`` for layout, so
    literal triple-backtick sequences must never reach that parser. Pygments
    produces already-escaped, presentation-only HTML for fenced code; prose
    remains escaped and whitespace-preserving.
    """
    rendered: list[str] = []
    cursor = 0
    for match in _FENCED_CODE_PATTERN.finditer(excerpt):
        prose = excerpt[cursor : match.start()]
        if prose:
            rendered.append(_citation_prose_html(prose))

        language = match.group("language") or "text"
        code = html.unescape(match.group("code")).rstrip("\r\n")
        try:
            lexer = get_lexer_by_name(language, stripall=False)
        except ClassNotFound:
            lexer = TextLexer(stripall=False)
        highlighted = highlight(code, lexer, _CODE_FORMATTER).rstrip("\r\n")
        highlighted = highlighted.replace("`", "&#96;")
        rendered.append(
            f'<div class="rr-citation-code"><code>{highlighted}</code></div>'
        )
        cursor = match.end()

    trailing_prose = excerpt[cursor:]
    if trailing_prose or not rendered:
        rendered.append(_citation_prose_html(trailing_prose))
    return "".join(rendered)


def _source_label(source_id: str, title: str | None) -> tuple[str, str]:
    """Derive a human-readable `(title, source-type label)` pair from a
    chunk's `source_id` and optional stored `title` -- shared by citation
    cards and the related-sources list so both describe the same kind of
    source the same way."""
    source_parts = source_id.split(":", 3)
    source_kind = source_parts[2] if len(source_parts) >= 3 else "source"
    source_tail = source_parts[3] if len(source_parts) == 4 else source_id
    source_type = {
        "markdown": "Markdown documentation",
        "issue": "Issue",
        "issue_comment": "Issue comment",
        "pull_request": "Pull request",
        "pull_request_comment": "Pull request comment",
        "pull_request_review": "Pull request review",
        "pull_request_review_comment": "Review comment",
        "commit": "Commit",
    }.get(source_kind, source_kind.replace("_", " ").title())
    if source_kind == "markdown":
        fallback_title = source_tail.rsplit("/", 1)[-1]
    elif source_kind in {"issue", "issue_comment"}:
        fallback_title = f"Issue #{source_tail.split(':', 1)[0]}"
    elif source_kind.startswith("pull_request"):
        fallback_title = f"Pull request #{source_tail.split(':', 1)[0]}"
    elif source_kind == "commit":
        fallback_title = f"Commit {source_tail[:8]}"
    else:
        fallback_title = source_tail
    return title or fallback_title, source_type


def _citation_html(citation: Citation) -> str:
    # Citation text originates from indexed repository content (issue/PR/
    # commit/markdown text), not from us -- it must be escaped before this
    # raw-HTML card renders it.
    title_text, source_type = _source_label(citation.source_id, citation.source_title)
    title = html.escape(title_text)
    source_type_html = html.escape(source_type)
    excerpt = _citation_excerpt_html(citation.excerpt)
    url = html.escape(citation.source_url, quote=True)
    display_url = html.escape(re.sub(r"^https?://", "", citation.source_url))
    return (
        '<details class="rr-citation">'
        f'<summary><span class="rr-citation-num">[{citation.number}]</span>'
        f'<span class="rr-citation-title">{title}</span>'
        f'<span class="rr-citation-type">{source_type_html}</span>'
        '<span class="rr-citation-chevron" aria-hidden="true">›</span></summary>'
        '<div class="rr-citation-detail">'
        f'<div class="rr-citation-excerpt">{excerpt}</div>'
        f'<a class="rr-citation-link" href="{url}" target="_blank" '
        f'rel="noopener noreferrer"><span aria-hidden="true">↗</span>'
        f"<span>{display_url}</span></a>"
        "</div></details>"
    )


def _meta_html(trace: RunTrace, context_turns_used: int) -> str:
    refinement_count = sum(
        1 for record in trace.searches if record.next_query is not None
    )
    caption = f"{trace.total_search_count} search"
    caption += "es" if trace.total_search_count != 1 else ""
    if refinement_count:
        caption += (
            f" ({refinement_count} refinement{'s' if refinement_count != 1 else ''})"
        )
    context_html = ""
    if context_turns_used:
        context_html = (
            ' · <span class="rr-context-flag">used context from '
            f"{context_turns_used} earlier turn"
            f"{'s' if context_turns_used != 1 else ''}</span>"
        )
    return f'<div class="rr-answer-meta">{html.escape(caption)}{context_html}</div>'


def _related_source_item_html(evidence: RankedEvidence) -> str:
    chunk = evidence.chunk
    title_text, source_type = _source_label(chunk.source_id, chunk.title)
    return (
        f"<li>{html.escape(title_text)} "
        f'<span class="rr-related-type">({html.escape(source_type)})</span></li>'
    )


def _related_sources_html(retrieved_evidence: tuple[RankedEvidence, ...]) -> str:
    """The mock's "related sources found (not sufficient to answer this)"
    disclosure: every source this run actually retrieved -- an
    insufficient-evidence outcome cites none of them, so every retrieved
    source is "related but insufficient" by definition. Omitted entirely
    (not an empty toggle) when nothing was retrieved at all."""
    if not retrieved_evidence:
        return ""
    items = "".join(_related_source_item_html(item) for item in retrieved_evidence)
    return (
        '<details class="rr-related-toggle">'
        "<summary>Show related sources found (not sufficient to answer this)"
        "</summary>"
        f'<ul class="rr-related-list">{items}</ul>'
        "</details>"
    )


def _render_qa_turn(
    question: str,
    outcome: AnswerOutcome,
    trace: RunTrace,
    context_turns_used: int,
    retrieved_evidence: tuple[RankedEvidence, ...] = (),
) -> None:
    st.markdown(
        '<div class="rr-qa-question"><span class="rr-q-label">Question</span>'
        f"{html.escape(question)}</div>",
        unsafe_allow_html=True,
    )
    if isinstance(outcome, AnsweredOutcome):
        st.markdown(
            '<div class="rr-answer-card">'
            f'<div class="rr-answer-text">{_answer_html(outcome.answer)}</div>'
            f"{_meta_html(trace, context_turns_used)}"
            "</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="rr-evidence-block">'
            '<div class="rr-evidence-header"><span>▣</span>Evidence</div>'
            f"{''.join(_citation_html(citation) for citation in outcome.citations)}"
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="rr-outcome-card">'
            '<div class="rr-outcome-head">'
            '<span aria-hidden="true">◎</span> Insufficient evidence</div>'
            f'<div class="rr-outcome-body">{html.escape(outcome.explanation)}</div>'
            f"{_meta_html(trace, context_turns_used)}"
            f"{_related_sources_html(retrieved_evidence)}"
            "</div>",
            unsafe_allow_html=True,
        )


def _render_ready_content() -> None:
    if not st.session_state.chat_turns:
        identity: RepositoryIdentity = st.session_state.identity
        st.markdown(
            '<div class="rr-empty-state"><div class="rr-empty-state-heading">'
            "Ask your first question about "
            f"{html.escape(identity.repository)}</div>"
            "<p>Try one of these, or ask your own. Answers are grounded in "
            "this snapshot's issues, pull requests, commits, and docs, and "
            "follow-ups can build on whatever you ask next.</p></div>",
            unsafe_allow_html=True,
        )
        repository_name = identity.name
        suggested_questions = (
            f"Why did {repository_name}'s maintainers choose its current design?",
            f"Which change introduced {repository_name}'s main behavior, and why?",
            "What alternatives or trade-offs were considered for a key design decision?",
        )
        with st.container(key="suggested_questions"):
            for index, suggestion in enumerate(suggested_questions):
                st.button(
                    suggestion,
                    key=f"suggested_question_{index}",
                    icon=":material/arrow_forward:",
                    on_click=_select_suggested_question,
                    args=(suggestion,),
                    width="stretch",
                )
    else:
        for turn in st.session_state.chat_turns:
            _render_qa_turn(
                turn["question"],
                turn["outcome"],
                turn["trace"],
                turn["context_turns_used"],
                turn.get("retrieved_evidence", ()),
            )

    if st.session_state.ask_error:
        st.error(st.session_state.ask_error)


def _select_suggested_question(question: str) -> None:
    """Copy a starter question into the composer without submitting it."""
    _replace_question_input(question)


def _render_composer(settings: Settings, stage: str) -> None:
    """Render one stable bottom composer for both idle and ready states.

    A native ``st.chat_input`` changes its internal DOM while a submission
    is in flight. That makes a separately positioned Clear action and a
    CSS-relabelled send icon jump apart at precisely the moment the user
    clicks Ask. Keeping all three controls in one form gives Streamlit one
    stable horizontal layout before, during, and after submission.
    """
    ready = stage == "ready"
    clear_requested = False

    with st.container(key="composer_shell"):
        with st.form(
            "question_form",
            clear_on_submit=False,
            enter_to_submit=True,
            border=False,
        ):
            submit_on_enter = st.form_submit_button(
                "Submit question",
                key="submit_question_on_enter",
                disabled=not ready,
            )
            if ready:
                col_clear, col_question, col_ask = st.columns(
                    [1.15, 7.2, 0.9], gap="small", vertical_alignment="center"
                )
                with col_clear:
                    clear_requested = st.form_submit_button(
                        "Clear chat", width="stretch"
                    )
            else:
                col_question, col_ask = st.columns(
                    [8.35, 0.9], gap="small", vertical_alignment="center"
                )

            with col_question:
                question = st.text_input(
                    "Question about repository history",
                    value=st.session_state.question_input_value,
                    key=(f"question_input_{st.session_state.question_input_revision}"),
                    placeholder=(
                        "Ask a question about this repository's history…"
                        if ready
                        else "Ask a question… (available once ready)"
                    ),
                    disabled=not ready,
                    label_visibility="collapsed",
                )
            with col_ask:
                ask_requested = st.form_submit_button(
                    "Ask", type="primary", disabled=not ready, width="stretch"
                )

    if clear_requested:
        _clear_chat()
        st.rerun()
    if (submit_on_enter or ask_requested) and question and question.strip():
        with st.spinner("Searching and answering…"):
            _handle_ask(settings, question.strip())
        _replace_question_input("")
        st.rerun()


def render() -> None:
    st.set_page_config(
        page_title="RepoRationale", page_icon="\U0001f50e", layout="wide"
    )
    _inject_custom_css()
    _init_state()

    settings = _load_settings()
    if settings is None:
        _render_missing_credentials_screen()
        st.stop()

    _render_sidebar()
    _render_home_fab()

    status_placeholder, rebuild_confirmed = _render_status_row()
    if rebuild_confirmed:
        _handle_rebuild(settings, status_placeholder)
        st.rerun()

    stage = st.session_state.stage
    if stage in ("idle", "unsupported", "operational_failure", "needs_indexing"):
        _render_setup_section(settings, status_placeholder)
    elif stage == "ready":
        _render_ready_content()

    _render_composer(settings, stage)


render()
