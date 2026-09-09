"""Smoke tests for the Streamlit demonstration script itself, using
`streamlit.testing.v1.AppTest` so the top-level `render()` path is at
least exercised end to end without a browser.

Deliberately narrow: neither test makes a GitHub, Voyage, or Anthropic
call. A malformed repository reference is rejected purely by format,
before any network call
(`reporationale.adapters.github.parse_github_repository_reference`,
reached through `preflight_repository_reference`), so it can exercise the
"unsupported" stage without a fake or real GitHub client.
"""

import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from reporationale.adapters.github import GitHubRepositoryMetadata
from reporationale.application.progress import ProgressEvent
from reporationale.domain.answering import (
    AnsweredOutcome,
    Citation,
    InsufficientEvidenceOutcome,
    RunTrace,
    SearchRecord,
)
from reporationale.domain.chunk import SourceChunk
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.retrieval import RankedEvidence

_APP_PATH = str(
    Path(__file__).resolve().parents[1] / "src" / "reporationale" / "streamlit_app.py"
)

_CREDENTIAL_VARIABLES = ("GITHUB_TOKEN", "VOYAGE_API_KEY", "ANTHROPIC_API_KEY")

# Generous: the script's cold import chain (streamlit, chromadb, anthropic,
# voyageai, pydantic) can comfortably exceed AppTest's 3-second default on
# the first run in a process; a warm second run is fast regardless.
_RUN_TIMEOUT_SECONDS = 60


def _run_until_stage(at: AppTest, expected: str, *, attempts: int = 50) -> None:
    """Drive AppTest reruns while a fast background indexing stub finishes."""
    for _ in range(attempts):
        at.run(timeout=_RUN_TIMEOUT_SECONDS)
        if at.session_state["stage"] == expected:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"stage did not become {expected!r}; got {at.session_state['stage']!r}"
    )


def test_missing_credentials_shows_setup_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for variable in _CREDENTIAL_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    # Run from an empty directory so a real local `.env` (a developer's own
    # working credentials, never committed -- see `.gitignore`) cannot be
    # picked up and mask the missing-credentials path this test exercises.
    monkeypatch.chdir(tmp_path)

    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=_RUN_TIMEOUT_SECONDS)

    assert not at.exception
    assert any(
        "Missing or invalid local credentials" in error.value for error in at.error
    )


def test_malformed_repository_reference_is_rejected_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-test-value")
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-test-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")

    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    assert not at.exception

    at.text_input[0].set_value("not-a-valid-repo")
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    at.button[0].click()
    at.run(timeout=_RUN_TIMEOUT_SECONDS)

    assert not at.exception
    assert any("owner/repository" in markdown.value.lower() for markdown in at.markdown)


def test_size_rejection_uses_product_copy_and_only_the_exceeded_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in _CREDENTIAL_VARIABLES:
        monkeypatch.setenv(variable, f"{variable.lower()}-test-value")

    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    at.session_state["stage"] = "unsupported"
    at.session_state["repo_input"] = "torvalds/linux"
    at.session_state["unsupported_reason_code"] = (
        "exceeds_all_issues_and_pull_requests_limit"
    )
    at.session_state["banner_message"] = (
        "Combined issues and pull requests — estimated: at least 3,600. "
        "Current limit: 3,500."
    )
    at.run(timeout=_RUN_TIMEOUT_SECONDS)

    rendered = next(
        markdown.value
        for markdown in at.markdown
        if 'rr-info-card-title">Unsupported repository' in markdown.value
    )
    assert "torvalds/linux" in rendered
    assert "larger than RepoRationale currently supports" in rendered
    assert "Limit exceeded" in rendered
    assert "at least 3,600" in rendered
    assert "MVP" not in rendered
    assert "embedding" not in rendered.lower()


def test_recoverable_indexing_failure_offers_a_working_retry_indexing_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recoverable indexing failure (the observed Windows `PermissionError`
    during local publication, among other `OSError`s) must leave the
    already-resolved repository state usable and render a real, working
    "Retry indexing" action -- not an unactionable failure screen whose
    message merely claims a retry is possible. Simulates the failure by
    patching the normalized-source build workflow (no GitHub, Voyage, or
    Anthropic call is made) and drives the app into the pre-indexing state
    directly through `at.session_state`, the same already-resolved shape
    `_handle_check` itself would have produced from a real successful
    check.
    """
    for variable in _CREDENTIAL_VARIABLES:
        monkeypatch.setenv(variable, f"{variable.lower()}-test-value")

    def _always_fails_with_permission_error(*args: object, **kwargs: object) -> object:
        raise PermissionError("simulated Windows file lock")

    monkeypatch.setattr(
        "reporationale.application.snapshot_workflow.build_normalized_source_snapshot",
        _always_fails_with_permission_error,
    )

    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    assert not at.exception

    identity = RepositoryIdentity(platform="github", owner="octo-org", name="demo-repo")
    metadata = GitHubRepositoryMetadata(
        identity=identity,
        github_id=1,
        html_url="https://github.com/octo-org/demo-repo",  # type: ignore[arg-type]
        private=False,
        default_branch="main",
    )
    at.session_state["stage"] = "needs_indexing"
    at.session_state["identity"] = identity
    at.session_state["metadata"] = metadata
    at.session_state["resolved_commit_sha"] = "c" * 40
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    assert not at.exception

    start_button = next(
        button for button in at.button if button.label == "Start indexing"
    )
    start_button.click()
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    assert not at.exception

    _run_until_stage(at, "needs_indexing")
    assert any("Indexing problem" in markdown.value for markdown in at.markdown)
    retry_buttons = [button for button in at.button if button.label == "Retry indexing"]
    assert len(retry_buttons) == 1

    # The retry action is real: clicking it drives the same handler again
    # (still failing here, deterministically, since the workflow is still
    # patched), proving this is a working button wired to
    # the background indexing action, not inert text.
    retry_buttons[0].click()
    _run_until_stage(at, "needs_indexing")
    assert not at.exception
    assert at.session_state["stage"] == "needs_indexing"
    assert any("Indexing problem" in markdown.value for markdown in at.markdown)


def test_indexing_timer_refreshes_and_cancel_stops_the_background_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in _CREDENTIAL_VARIABLES:
        monkeypatch.setenv(variable, f"{variable.lower()}-test-value")

    def _slow_until_cancelled(*args: object, **kwargs: object) -> object:
        on_progress = kwargs["on_progress"]
        assert callable(on_progress)
        while True:
            on_progress(ProgressEvent(phase="collecting_sources", status="started"))
            time.sleep(0.02)

    monkeypatch.setattr(
        "reporationale.application.snapshot_workflow.build_normalized_source_snapshot",
        _slow_until_cancelled,
    )

    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    identity = RepositoryIdentity(platform="github", owner="octo-org", name="demo-repo")
    at.session_state["stage"] = "needs_indexing"
    at.session_state["identity"] = identity
    at.session_state["metadata"] = GitHubRepositoryMetadata(
        identity=identity,
        github_id=1,
        html_url="https://github.com/octo-org/demo-repo",  # type: ignore[arg-type]
        private=False,
        default_branch="main",
    )
    at.session_state["resolved_commit_sha"] = "c" * 40
    at.run(timeout=_RUN_TIMEOUT_SECONDS)

    next(button for button in at.button if button.label == "Start indexing").click()
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    assert at.session_state["stage"] == "indexing"
    assert any("elapsed 0s" in markdown.value for markdown in at.markdown)

    time.sleep(1.05)
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    assert any(
        "elapsed 1s" in markdown.value or "elapsed 2s" in markdown.value
        for markdown in at.markdown
    )

    cancel = next(button for button in at.button if button.label == "Cancel")
    cancel.click()
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    _run_until_stage(at, "needs_indexing")

    assert at.session_state["indexing_job"] is None
    assert any("Indexing stopped" in markdown.value for markdown in at.markdown)


def test_suggested_question_can_be_reselected_after_chat_and_investigation_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in _CREDENTIAL_VARIABLES:
        monkeypatch.setenv(variable, f"{variable.lower()}-test-value")

    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    identity = RepositoryIdentity(platform="github", owner="octo-org", name="demo-repo")
    at.session_state["stage"] = "ready"
    at.session_state["identity"] = identity
    at.run(timeout=_RUN_TIMEOUT_SECONDS)

    suggestion = "Why did demo-repo's maintainers choose its current design?"
    suggested_button = next(
        button for button in at.button if button.label == suggestion
    )
    suggested_button.click()
    at.run(timeout=_RUN_TIMEOUT_SECONDS)

    assert not at.exception
    assert at.session_state["chat_turns"] == []
    assert at.text_input[0].value == suggestion

    clear_chat = next(button for button in at.button if button.label == "Clear chat")
    clear_chat.click()
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    assert not at.exception
    assert at.text_input[0].value == ""

    suggested_button = next(
        button for button in at.button if button.label == suggestion
    )
    suggested_button.click()
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    assert not at.exception
    assert at.text_input[0].value == suggestion

    new_investigation = next(
        button for button in at.button if button.label == "New investigation"
    )
    new_investigation.click()
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    assert not at.exception
    assert at.session_state["stage"] == "idle"

    at.session_state["stage"] = "ready"
    at.session_state["identity"] = identity
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    suggested_button = next(
        button for button in at.button if button.label == suggestion
    )
    suggested_button.click()
    at.run(timeout=_RUN_TIMEOUT_SECONDS)

    assert not at.exception
    assert at.text_input[0].value == suggestion


def test_citation_code_fence_cannot_capture_card_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in _CREDENTIAL_VARIABLES:
        monkeypatch.setenv(variable, f"{variable.lower()}-test-value")

    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    identity = RepositoryIdentity(platform="github", owner="octo-org", name="demo-repo")
    citation = Citation(
        number=1,
        evidence_id="github:octo-org/demo-repo:issue:29:chunk:0",
        source_id="github:octo-org/demo-repo:issue:29",
        source_title="Issue #29",
        source_url="https://github.com/octo-org/demo-repo/issues/29",
        excerpt="Before the example\n```js\nconst answer = 42;\n```\nAfter the example",
    )
    outcome = AnsweredOutcome(answer="It is a demo [1].", citations=(citation,))
    trace = RunTrace(
        model="test-model",
        agent_version="test-agent/1",
        searches=(),
        model_call_latencies_seconds=(0.1,),
        total_search_count=0,
        total_latency_seconds=0.1,
        input_tokens=1,
        output_tokens=1,
    )
    at.session_state["stage"] = "ready"
    at.session_state["identity"] = identity
    at.session_state["chat_turns"] = [
        {
            "question": "What is this repository about?",
            "outcome": outcome,
            "trace": trace,
            "context_turns_used": 0,
        }
    ]
    at.run(timeout=_RUN_TIMEOUT_SECONDS)

    evidence_html = next(
        markdown.value
        for markdown in at.markdown
        if 'class="rr-evidence-block"' in markdown.value
    )
    assert not at.exception
    assert "```" not in evidence_html
    assert 'class="rr-citation-code"' in evidence_html
    assert "answer" in evidence_html
    assert '<span class="' in evidence_html
    assert 'class="rr-citation-link"' in evidence_html


def test_insufficient_evidence_shows_icon_refinement_count_and_related_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The insufficient-evidence card must match the approved mock: a
    circular icon next to the heading, a refinement count derived from the
    real search trace, and a "related sources found" disclosure listing
    every source the run actually retrieved (none were cited, so all of
    them are "related but insufficient" by definition)."""
    for variable in _CREDENTIAL_VARIABLES:
        monkeypatch.setenv(variable, f"{variable.lower()}-test-value")

    at = AppTest.from_file(_APP_PATH)
    at.run(timeout=_RUN_TIMEOUT_SECONDS)
    identity = RepositoryIdentity(platform="github", owner="octo-org", name="demo-repo")
    chunk = SourceChunk(
        chunk_id="github:octo-org/demo-repo:commit:4a7c1e2:chunk:0",
        source_id="github:octo-org/demo-repo:commit:4a7c1e2",
        chunk_index=0,
        text="Migrate build to Gradle",
        platform="github",
        repository="octo-org/demo-repo",
        source_type="commit",
        source_url="https://github.com/octo-org/demo-repo/commit/4a7c1e2",
    )
    evidence = RankedEvidence(
        evidence_id=chunk.chunk_id,
        rank=1,
        score=0.42,
        score_kind="cosine_distance",
        chunk=chunk,
    )
    outcome = InsufficientEvidenceOutcome(explanation="No rationale was found.")
    trace = RunTrace(
        model="test-model",
        agent_version="test-agent/1",
        searches=(
            SearchRecord(
                query="Why did demo-repo switch build tools?",
                evidence_ids=(chunk.chunk_id,),
                latency_seconds=0.1,
                assessment="insufficient",
                missing_information="need rationale",
                next_query="demo-repo build tooling rationale",
            ),
            SearchRecord(
                query="demo-repo build tooling rationale",
                evidence_ids=(chunk.chunk_id,),
                latency_seconds=0.1,
                assessment="insufficient",
                missing_information="No rationale was found.",
            ),
        ),
        model_call_latencies_seconds=(0.1, 0.1),
        total_search_count=2,
        total_latency_seconds=0.2,
        input_tokens=1,
        output_tokens=1,
    )
    at.session_state["stage"] = "ready"
    at.session_state["identity"] = identity
    at.session_state["chat_turns"] = [
        {
            "question": "Why did demo-repo switch build tools?",
            "outcome": outcome,
            "trace": trace,
            "context_turns_used": 0,
            "retrieved_evidence": (evidence,),
        }
    ]
    at.run(timeout=_RUN_TIMEOUT_SECONDS)

    outcome_html = next(
        markdown.value
        for markdown in at.markdown
        if 'class="rr-outcome-card"' in markdown.value
    )
    assert not at.exception
    assert "◎" in outcome_html
    assert "Insufficient evidence" in outcome_html
    assert "(1 refinement)" in outcome_html
    assert 'class="rr-related-toggle"' in outcome_html
    assert "Commit 4a7c1e2" in outcome_html
