"""Tests for the platform-independent source-document contract."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from reporationale.domain import SourceDocument


def make_source(**overrides: object) -> SourceDocument:
    """Create a valid source while allowing one rule to vary per test."""
    values: dict[str, object] = {
        "source_id": "github:octo/example:issue:42:description",
        "platform": "github",
        "repository": "octo/example",
        "source_type": "issue",
        "text": "The cache was introduced to reduce repeated API calls.",
        "source_url": "https://github.com/octo/example/issues/42",
        "created_at": datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 2, 11, 0, tzinfo=UTC),
        "item_number": 42,
        "title": "Reduce repeated API calls",
        "metadata": {"state": "closed", "labels": ["performance"]},
    }
    values.update(overrides)
    return SourceDocument.model_validate(values)


def test_source_document_preserves_citation_and_provenance() -> None:
    """A normalized source retains evidence text and its original link."""
    source = make_source()

    assert source.source_id == "github:octo/example:issue:42:description"
    assert str(source.source_url) == "https://github.com/octo/example/issues/42"
    assert source.text.startswith("The cache was introduced")
    assert source.metadata["state"] == "closed"


def test_source_type_remains_extensible_across_platforms() -> None:
    """The core accepts a future platform's native terminology."""
    source = make_source(
        source_id="gitlab:group/example:merge_request:7:description",
        platform="gitlab",
        repository="group/example",
        source_type="merge_request",
        source_url="https://gitlab.com/group/example/-/merge_requests/7",
        item_number=7,
    )

    assert source.platform == "gitlab"
    assert source.source_type == "merge_request"


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_source_document_rejects_blank_text(text: str) -> None:
    """Non-textual or empty repository events do not become documents."""
    with pytest.raises(ValidationError, match="non-whitespace content"):
        make_source(text=text)


def test_source_id_must_match_platform_namespace() -> None:
    """Namespacing prevents identities from colliding across platforms."""
    with pytest.raises(ValidationError, match="namespaced by platform"):
        make_source(source_id="gitlab:octo/example:issue:42:description")


def test_identifiers_reject_whitespace() -> None:
    """Stable identities remain safe to persist and compare exactly."""
    with pytest.raises(ValidationError, match="String should match pattern"):
        make_source(source_id="github:octo/example:issue 42")

    with pytest.raises(ValidationError, match="String should match pattern"):
        make_source(repository="octo / example")


def test_parent_must_use_same_platform_and_cannot_be_self() -> None:
    """Direct source relationships cannot cross or loop platform identities."""
    with pytest.raises(ValidationError, match="same platform namespace"):
        make_source(parent_source_id="gitlab:group/example:issue:42")

    source_id = "github:octo/example:issue:42:description"
    with pytest.raises(ValidationError, match="cannot be its own parent"):
        make_source(source_id=source_id, parent_source_id=source_id)


def test_timestamps_must_be_ordered_and_timezone_aware() -> None:
    """Persisted provenance times remain unambiguous and chronological."""
    with pytest.raises(ValidationError, match="include a timezone"):
        make_source(created_at=datetime(2026, 9, 1, 10, 0))

    with pytest.raises(ValidationError, match="cannot precede"):
        make_source(
            created_at=datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            updated_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        )


def test_unknown_top_level_fields_are_rejected() -> None:
    """Provider-specific response fields cannot leak into the core contract."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        make_source(github_node_id="provider-specific-value")
