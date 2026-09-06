"""Unit tests for `reporationale.adapters.github.pull_request`: the root
record, general comments, review summaries, and inline review
comments/replies. Client-level pagination/ingestion behaviour is covered
in `test_github_source_ingestion_client.py`.
"""

import json

import pytest
from github_test_support import load_fixture

from reporationale.adapters.github.errors import GitHubMalformedResponse
from reporationale.adapters.github.pull_request import (
    parse_closed_pull_request,
    parse_pull_request_general_comment,
    parse_pull_request_review,
    parse_pull_request_review_comment,
)
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_PR_NUMBER = 42
_MISSING = object()


def _apply(raw: dict[str, object], overrides: dict[str, object]) -> dict[str, object]:
    result = dict(raw)
    for key, value in overrides.items():
        if value is _MISSING:
            del result[key]
        else:
            result[key] = value
    return result


# --- Shared guards (identity/number checks are identical across every ------
# --- parser in this module, so each is exercised once, not per-parser). ----


def test_rejects_non_github_identity_directly() -> None:
    non_github = RepositoryIdentity(platform="gitlab", owner="o", name="r")
    with pytest.raises(ValueError, match="platform must be 'github'"):
        parse_closed_pull_request(
            load_fixture("pull_request_merged.json"), identity=non_github
        )


@pytest.mark.parametrize("pull_request_number", [True, 0, -1, "42"])
def test_rejects_invalid_pull_request_number_directly(
    pull_request_number: object,
) -> None:
    with pytest.raises(ValueError, match="pull_request_number"):
        parse_pull_request_review(
            load_fixture("pull_request_review.json"),
            identity=_IDENTITY,
            pull_request_number=pull_request_number,  # type: ignore[arg-type]
        )


# --- Root ------------------------------------------------------------------


def test_parse_merged_pull_request() -> None:
    """A merged pull request normalizes title, body, and merge metadata,
    and round-trips through serialization."""
    raw = load_fixture("pull_request_merged.json")

    document = parse_closed_pull_request(raw, identity=_IDENTITY)

    assert (
        document.source_id == "github:octo-org/example-repo:pull_request:42:description"
    )
    assert document.source_type == "pull_request"
    assert document.item_number == 42
    assert document.text == (
        "Fix flaky retry logic\n\n"
        "This fixes the flaky retry logic described in #40.\n\n"
        "See discussion below."
    )
    assert document.metadata["merged"] is True
    assert document.metadata["merge_commit_sha"] == (
        "abc123def456abc123def456abc123def456abc1"
    )
    assert document.metadata["author_login"] == "octocat"

    dumped_json = document.model_dump_json()
    assert SourceDocument.model_validate(json.loads(dumped_json)) == document
    assert SourceDocument.model_validate_json(dumped_json) == document


def test_parse_closed_unmerged_pull_request_with_null_body() -> None:
    """A closed-unmerged pull request with a null body renders title-only
    text and does not fabricate merge information."""
    raw = load_fixture("pull_request_closed_unmerged.json")

    document = parse_closed_pull_request(raw, identity=_IDENTITY)

    assert document.text == "Attempt to add experimental cache"
    assert document.metadata["merged"] is False
    assert document.metadata["merged_at"] is None
    assert document.metadata["merge_commit_sha"] is None


def test_legacy_empty_merge_commit_sha_normalizes_to_none() -> None:
    raw = _apply(
        load_fixture("pull_request_closed_unmerged.json"), {"merge_commit_sha": ""}
    )

    document = parse_closed_pull_request(raw, identity=_IDENTITY)

    assert document.metadata["merge_commit_sha"] is None


def test_realistic_list_response_without_merged_field_is_accepted() -> None:
    """A payload shaped exactly like the documented "List pull requests"
    response — which has `merged_at` but no `merged` boolean (that field
    exists only on the single-item "Get a pull request" response) — is
    accepted and normalized without a per-item detail request."""
    raw = {
        "id": 555000111,
        "number": 15,
        "state": "closed",
        "title": "Realistic list-endpoint payload",
        "body": "Body text from the list endpoint.",
        "html_url": "https://github.com/octo-org/example-repo/pull/15",
        "created_at": "2024-02-01T00:00:00Z",
        "updated_at": "2024-02-03T00:00:00Z",
        "closed_at": "2024-02-03T00:00:00Z",
        "merged_at": "2024-02-03T00:00:00Z",
        "merge_commit_sha": "f" * 40,
        "user": {"login": "octocat", "type": "User"},
    }

    document = parse_closed_pull_request(raw, identity=_IDENTITY)

    assert document.metadata["merged"] is True


@pytest.mark.parametrize("key", ["body", "merged_at", "merge_commit_sha", "user"])
def test_pull_request_nullable_vs_missing_key(key: str) -> None:
    """A genuinely missing key for a field that may legitimately be `null`
    is rejected — not silently treated as an explicit `null` — while an
    actual explicit `null` remains valid."""
    base = load_fixture("pull_request_merged.json")

    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_closed_pull_request(_apply(base, {key: _MISSING}), identity=_IDENTITY)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None

    assert parse_closed_pull_request(_apply(base, {key: None}), identity=_IDENTITY)


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "987654321"},
        {"id": 0},
        {"id": True},
        {"number": 0},
        {"state": "open"},
        {"title": ""},
        {"created_at": "2024-01-10T09:00:00"},
        {"merge_commit_sha": "   "},
        {"user": {"login": "", "type": "User"}},
    ],
)
def test_parse_closed_pull_request_rejects_malformed_fields(
    overrides: dict[str, object],
) -> None:
    raw = _apply(load_fixture("pull_request_merged.json"), overrides)
    with pytest.raises(GitHubMalformedResponse):
        parse_closed_pull_request(raw, identity=_IDENTITY)


@pytest.mark.parametrize(
    "html_url",
    [
        "https://github.com/octo-org/example-repo/pull/99",
        "https://github.com/wrong-owner/example-repo/pull/42",
        "http://github.com/octo-org/example-repo/pull/42",
        "https://github.com/octo-org/example-repo/pull/42?tab=files",
    ],
)
def test_parse_closed_pull_request_rejects_non_canonical_html_url(
    html_url: str,
) -> None:
    raw = _apply(load_fixture("pull_request_merged.json"), {"html_url": html_url})
    with pytest.raises(GitHubMalformedResponse):
        parse_closed_pull_request(raw, identity=_IDENTITY)


def test_parse_closed_pull_request_rejects_non_object_input() -> None:
    with pytest.raises(GitHubMalformedResponse):
        parse_closed_pull_request("not-an-object", identity=_IDENTITY)


# --- General comments (GitHub's shared "issue comment" object) -------------


def test_parse_non_empty_comment() -> None:
    raw = load_fixture("pull_request_comment.json")

    source_id, document = parse_pull_request_general_comment(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert source_id == "github:octo-org/example-repo:pull_request:42:comment:2001"
    assert document is not None
    assert document.source_type == "pull_request_comment"
    assert document.parent_source_id == (
        "github:octo-org/example-repo:pull_request:42:description"
    )
    assert document.text == "This looks good to me, thanks for fixing it!"
    assert str(document.source_url) == (
        "https://github.com/octo-org/example-repo/pull/42#issuecomment-2001"
    )
    assert document.metadata["author_login"] == "reviewer1"


def test_parse_comment_with_missing_author_preserves_null() -> None:
    raw = load_fixture("pull_request_comment_no_author.json")

    _source_id, document = parse_pull_request_general_comment(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert document is not None
    assert document.metadata["author_login"] is None


@pytest.mark.parametrize("body", [None, "", "   ", "\t\n"])
def test_null_empty_or_whitespace_only_comment_body_is_skipped(body: object) -> None:
    raw = _apply(load_fixture("pull_request_comment.json"), {"body": body})

    source_id, document = parse_pull_request_general_comment(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert source_id
    assert document is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "2001"},
        {"id": 0},
        {"id": True},
        {"author_association": ""},
        {"user": {"login": "", "type": "User"}},
        {"created_at": "2024-01-11T10:00:00"},
        {"updated_at": "2024-01-11T09:00:00Z"},
    ],
)
def test_parse_comment_rejects_malformed_fields(overrides: dict[str, object]) -> None:
    raw = _apply(load_fixture("pull_request_comment.json"), overrides)
    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_pull_request_general_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize("key", ["body", "user", "html_url", "issue_url"])
def test_parse_comment_rejects_missing_required_key(key: str) -> None:
    raw = load_fixture("pull_request_comment.json")
    del raw[key]
    with pytest.raises(GitHubMalformedResponse):
        parse_pull_request_general_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "html_url",
            "https://github.com/octo-org/example-repo/pull/99#issuecomment-2001",
        ),
        ("html_url", "https://github.com/octo-org/example-repo/pull/42"),
        (
            "url",
            "https://api.github.com/repos/octo-org/example-repo/issues/comments/9999",
        ),
        ("issue_url", "https://api.github.com/repos/octo-org/wrong-repo/issues/42"),
    ],
)
def test_parse_comment_rejects_non_canonical_urls(field: str, value: str) -> None:
    """`html_url`, `url`, and `issue_url` each carry documented canonical
    forms; one representative violation per field is enough to prove the
    shared URL-validation helper is actually wired up here."""
    raw = _apply(load_fixture("pull_request_comment.json"), {field: value})
    with pytest.raises(GitHubMalformedResponse):
        parse_pull_request_general_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )


# --- Review summaries --------------------------------------------------


def test_parse_review_with_non_empty_body() -> None:
    raw = load_fixture("pull_request_review.json")

    source_id, document = parse_pull_request_review(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert source_id == "github:octo-org/example-repo:pull_request:42:review:501"
    assert document is not None
    assert document.source_type == "pull_request_review"
    assert document.metadata["state"] == "APPROVED"
    assert document.metadata["commit_id"] == "a" * 40


@pytest.mark.parametrize("body", [None, "", "   "])
def test_review_with_empty_body_is_skipped(body: object) -> None:
    raw = _apply(load_fixture("pull_request_review.json"), {"body": body})

    source_id, document = parse_pull_request_review(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert source_id
    assert document is None


@pytest.mark.parametrize("state", ["APPROVED", "DISMISSED", "PENDING"])
def test_documented_review_states_are_accepted(state: str) -> None:
    raw = _apply(load_fixture("pull_request_review.json"), {"state": state})

    _source_id, document = parse_pull_request_review(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert document is not None
    assert document.metadata["state"] == state


def test_pending_review_may_have_a_null_submitted_at() -> None:
    raw = _apply(
        load_fixture("pull_request_review.json"),
        {"state": "PENDING", "submitted_at": None},
    )

    _source_id, document = parse_pull_request_review(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert document is not None
    assert document.created_at is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "501"},
        {"id": 0},
        {"state": "UNKNOWN_STATE"},
        {"commit_id": "   "},
        {"submitted_at": "2024-01-11T11:00:00"},
    ],
)
def test_review_rejects_malformed_fields(overrides: dict[str, object]) -> None:
    raw = _apply(load_fixture("pull_request_review.json"), overrides)
    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_pull_request_review(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize("key", ["id", "body", "state", "commit_id", "user"])
def test_review_rejects_missing_required_key(key: str) -> None:
    raw = load_fixture("pull_request_review.json")
    del raw[key]
    with pytest.raises(GitHubMalformedResponse):
        parse_pull_request_review(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )


@pytest.mark.parametrize(
    "html_url",
    [
        "https://github.com/octo-org/example-repo/pull/42#pullrequestreview-999",
        "https://github.com/octo-org/example-repo/pull/42",
    ],
)
def test_review_rejects_non_canonical_html_url(html_url: str) -> None:
    raw = _apply(load_fixture("pull_request_review.json"), {"html_url": html_url})
    with pytest.raises(GitHubMalformedResponse):
        parse_pull_request_review(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )


# --- Inline review comments/replies ------------------------------------


def test_top_level_review_comment_uses_pr_description_as_parent() -> None:
    """Regression test: GitHub's real endpoint (observed on
    `pallets/itsdangerous` PR 419) omits `in_reply_to_id` entirely for a
    top-level comment rather than sending it as documented `null`; the
    fixture reflects that real, key-omitted shape, and it must be accepted
    and normalized identically to an explicit `null` — the pull request's
    own description as parent."""
    raw = load_fixture("pull_request_review_comment.json")
    assert "in_reply_to_id" not in raw

    source_id, comment_id, in_reply_to_id, document = parse_pull_request_review_comment(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert (
        source_id == "github:octo-org/example-repo:pull_request:42:review_comment:601"
    )
    assert comment_id == 601
    assert in_reply_to_id is None
    assert document is not None
    assert document.parent_source_id == (
        "github:octo-org/example-repo:pull_request:42:description"
    )
    assert document.metadata["is_outdated"] is False


def test_reply_uses_parent_comment_as_parent_and_reports_outdated() -> None:
    raw = load_fixture("pull_request_review_comment_reply.json")

    _source_id, comment_id, in_reply_to_id, document = (
        parse_pull_request_review_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )
    )

    assert comment_id == 602
    assert in_reply_to_id == 601
    assert document is not None
    assert document.parent_source_id == (
        "github:octo-org/example-repo:pull_request:42:review_comment:601"
    )
    assert document.metadata["is_outdated"] is True


def test_discussion_diff_fragment_is_accepted() -> None:
    """GitHub's documented diff-view anchor (`discussion-diff-{id}`) is a
    distinct, valid family from the conversation-timeline anchor
    (`discussion_r{id}`) and need not equal the comment's own id."""
    raw = _apply(
        load_fixture("pull_request_review_comment.json"),
        {
            "html_url": "https://github.com/octo-org/example-repo/pull/42#discussion-diff-9999"
        },
    )

    _s, _c, _r, document = parse_pull_request_review_comment(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert document is not None


def test_file_level_comment_is_valid_and_is_outdated_is_none() -> None:
    """A file-level comment always has a null diff position by definition;
    that must not be reported as `outdated`."""
    raw = _apply(
        load_fixture("pull_request_review_comment.json"),
        {
            "subject_type": "file",
            "position": None,
            "original_position": None,
            "line": None,
            "original_line": None,
            "start_line": None,
            "start_side": None,
        },
    )

    _s, _c, _r, document = parse_pull_request_review_comment(
        raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
    )

    assert document is not None
    assert document.metadata["subject_type"] == "file"
    assert document.metadata["is_outdated"] is None


@pytest.mark.parametrize(
    ("overrides", "should_raise"),
    [
        # The documented legacy response shape omits `subject_type`
        # entirely; that must be accepted, not rejected.
        ({"subject_type": _MISSING}, False),
        ({"subject_type": "banana"}, True),
        ({"side": "CENTER"}, True),
        ({"start_side": "CENTER"}, True),
        ({"position": True}, True),
        ({"position": 0}, True),
        ({"position": "5"}, True),
    ],
)
def test_documented_subject_type_and_location_enum_contract(
    overrides: dict[str, object], should_raise: bool
) -> None:
    raw = _apply(load_fixture("pull_request_review_comment.json"), overrides)

    if should_raise:
        with pytest.raises(GitHubMalformedResponse):
            parse_pull_request_review_comment(
                raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
            )
    else:
        _s, _c, _r, document = parse_pull_request_review_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )
        assert document is not None
        assert document.metadata["subject_type"] is None
        assert document.metadata["is_outdated"] is None


@pytest.mark.parametrize("body", [None, "", "   "])
def test_review_comment_empty_body_is_skipped_but_identity_still_returned(
    body: object,
) -> None:
    raw = _apply(load_fixture("pull_request_review_comment.json"), {"body": body})

    source_id, comment_id, _in_reply_to_id, document = (
        parse_pull_request_review_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )
    )

    assert source_id
    assert comment_id == 601
    assert document is None


def test_self_referential_reply_is_rejected() -> None:
    raw = _apply(
        load_fixture("pull_request_review_comment.json"), {"in_reply_to_id": 601}
    )
    with pytest.raises(GitHubMalformedResponse):
        parse_pull_request_review_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "601"},
        {"id": True},
        {"in_reply_to_id": 0},
        {"in_reply_to_id": True},
        {"side": ""},
        {"created_at": "2024-01-11T11:00:00"},
        {"updated_at": "2024-01-11T10:00:00Z"},
    ],
)
def test_review_comment_rejects_malformed_fields(overrides: dict[str, object]) -> None:
    raw = _apply(load_fixture("pull_request_review_comment.json"), overrides)
    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_pull_request_review_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize("key", ["body", "user", "position"])
def test_review_comment_rejects_missing_required_nullable_key(key: str) -> None:
    raw = load_fixture("pull_request_review_comment.json")
    del raw[key]
    with pytest.raises(GitHubMalformedResponse):
        parse_pull_request_review_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )


@pytest.mark.parametrize(
    "html_url",
    [
        "https://github.com/octo-org/example-repo/pull/42#discussion_r9999",
        "https://github.com/octo-org/example-repo/pull/42",
        "https://github.com/octo-org/example-repo/pull/42#discussion-diff-0",
        "https://github.com/octo-org/example-repo/pull/42#discussion-diff-abc",
        "https://github.com/octo-org/example-repo/pull/42#something-else",
    ],
)
def test_review_comment_rejects_non_canonical_html_url(html_url: str) -> None:
    raw = _apply(
        load_fixture("pull_request_review_comment.json"), {"html_url": html_url}
    )
    with pytest.raises(GitHubMalformedResponse):
        parse_pull_request_review_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )


@pytest.mark.parametrize(
    "pull_request_url",
    [
        "https://api.github.com/repos/octo-org/other-repo/pulls/42",
        "https://api.github.com/repos/octo-org/example-repo/pulls/99",
        "http://api.github.com/repos/octo-org/example-repo/pulls/42",
    ],
)
def test_review_comment_rejects_non_canonical_pull_request_url(
    pull_request_url: str,
) -> None:
    raw = _apply(
        load_fixture("pull_request_review_comment.json"),
        {"pull_request_url": pull_request_url},
    )
    with pytest.raises(GitHubMalformedResponse):
        parse_pull_request_review_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )


def test_review_comment_rejects_missing_pull_request_url() -> None:
    raw = load_fixture("pull_request_review_comment.json")
    del raw["pull_request_url"]
    with pytest.raises(GitHubMalformedResponse):
        parse_pull_request_review_comment(
            raw, identity=_IDENTITY, pull_request_number=_PR_NUMBER
        )
