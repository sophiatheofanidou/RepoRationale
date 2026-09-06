"""Unit tests for Markdown tree/blob validation and normalization helpers."""

import base64

import pytest

from reporationale.adapters.github.errors import GitHubMalformedResponse
from reporationale.adapters.github.markdown import (
    decode_markdown_blob,
    is_markdown_path,
    is_regular_file_blob,
    parse_commit_reference,
    parse_git_tree,
    parse_markdown_document,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_SHA = "e" * 40


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


@pytest.mark.parametrize(
    "path",
    ["README.md", "README.MD", "docs/guide.markdown", "docs/GUIDE.MARKDOWN"],
)
def test_is_markdown_path_case_insensitive(path: str) -> None:
    assert is_markdown_path(path) is True


@pytest.mark.parametrize("path", ["README.txt", "app.py", "markdown", "README.md.bak"])
def test_is_markdown_path_rejects_other_extensions(path: str) -> None:
    assert is_markdown_path(path) is False


def test_parse_commit_reference() -> None:
    raw = {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}

    commit_sha, tree_sha = parse_commit_reference(raw)

    assert commit_sha == "a" * 40
    assert tree_sha == "b" * 40


def test_parse_commit_reference_rejects_malformed_response() -> None:
    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_commit_reference(
            {"sha": "not-hex", "commit": {"tree": {"sha": "b" * 40}}}
        )

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_parse_git_tree_returns_entries_and_truncated_flag() -> None:
    raw = {
        "sha": _SHA,
        "truncated": False,
        "tree": [
            {"path": "README.md", "mode": "100644", "type": "blob", "sha": "a" * 40},
            {"path": "docs", "mode": "040000", "type": "tree", "sha": "b" * 40},
        ],
    }

    sha, entries, truncated = parse_git_tree(raw)

    assert sha == _SHA
    assert truncated is False
    assert [e.path for e in entries] == ["README.md", "docs"]
    assert is_regular_file_blob(entries[0]) is True
    assert is_regular_file_blob(entries[1]) is False


def test_parse_git_tree_reports_truncation() -> None:
    _sha, _entries, truncated = parse_git_tree(
        {"sha": _SHA, "truncated": True, "tree": []}
    )
    assert truncated is True


def test_symlink_blob_is_not_a_regular_file() -> None:
    _sha, entries, _truncated = parse_git_tree(
        {
            "sha": _SHA,
            "truncated": False,
            "tree": [
                {"path": "link.md", "mode": "120000", "type": "blob", "sha": "a" * 40}
            ],
        }
    )
    assert is_regular_file_blob(entries[0]) is False


@pytest.mark.parametrize(
    "path",
    ["/README.md", "README.md/", "docs/../README.md", "docs//README.md", "./README.md"],
)
def test_parse_git_tree_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(GitHubMalformedResponse):
        parse_git_tree(
            {
                "sha": _SHA,
                "truncated": False,
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "sha": "a" * 40}
                ],
            }
        )


def test_decode_markdown_blob_valid_base64_utf8() -> None:
    content = decode_markdown_blob(
        {"sha": _SHA, "content": _b64("# Hello\n\nWorld."), "encoding": "base64"},
        expected_sha=_SHA,
    )
    assert content == "# Hello\n\nWorld."


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"])
def test_decode_markdown_blob_tolerates_cr_lf_wrapped_base64(line_ending: str) -> None:
    """GitHub line-wraps base64 content with CR/LF; embedded `\\r`/`\\n`
    must not be treated as invalid characters, in any of the documented
    line-ending forms."""
    encoded = _b64("# A longer document with enough content to matter here.")
    wrapped = line_ending.join(encoded[i : i + 10] for i in range(0, len(encoded), 10))
    content = decode_markdown_blob(
        {"sha": _SHA, "content": wrapped, "encoding": "base64"}, expected_sha=_SHA
    )
    assert content == "# A longer document with enough content to matter here."


@pytest.mark.parametrize("whitespace", [" ", "\t"])
def test_decode_markdown_blob_rejects_other_whitespace(whitespace: str) -> None:
    """Only CR/LF line wrapping is tolerated; a space or tab is not part of
    the base64 alphabet and must not be silently stripped."""
    encoded = _b64("# A longer document with enough content to matter here.")
    malformed = encoded[:10] + whitespace + encoded[10:]
    with pytest.raises(GitHubMalformedResponse) as excinfo:
        decode_markdown_blob(
            {"sha": _SHA, "content": malformed, "encoding": "base64"},
            expected_sha=_SHA,
        )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_decode_markdown_blob_rejects_sha_mismatch() -> None:
    with pytest.raises(GitHubMalformedResponse):
        decode_markdown_blob(
            {"sha": "f" * 40, "content": _b64("text"), "encoding": "base64"},
            expected_sha=_SHA,
        )


def test_decode_markdown_blob_rejects_unsupported_encoding() -> None:
    with pytest.raises(GitHubMalformedResponse):
        decode_markdown_blob(
            {"sha": _SHA, "content": "text", "encoding": "utf-8"}, expected_sha=_SHA
        )


def test_decode_markdown_blob_rejects_malformed_base64() -> None:
    with pytest.raises(GitHubMalformedResponse) as excinfo:
        decode_markdown_blob(
            {"sha": _SHA, "content": "not-valid-base64!!!", "encoding": "base64"},
            expected_sha=_SHA,
        )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_decode_markdown_blob_rejects_invalid_utf8() -> None:
    invalid_utf8_bytes = b"\xff\xfe\x00\x01"
    encoded = base64.b64encode(invalid_utf8_bytes).decode("ascii")
    with pytest.raises(GitHubMalformedResponse):
        decode_markdown_blob(
            {"sha": _SHA, "content": encoded, "encoding": "base64"}, expected_sha=_SHA
        )


def test_parse_markdown_document_percent_encodes_whitespace_and_unicode_paths() -> None:
    source_id, document = parse_markdown_document(
        identity=_IDENTITY,
        commit_sha="c" * 40,
        blob_sha=_SHA,
        path="docs/my notes.md",
        content="Some notes.",
    )
    assert source_id == "github:octo-org/example-repo:markdown:docs/my%20notes.md"
    assert document is not None
    assert str(document.source_url) == (
        f"https://github.com/octo-org/example-repo/blob/{'c' * 40}/docs/my%20notes.md"
    )
    assert document.metadata["path"] == "docs/my notes.md"
    assert document.metadata["blob_sha"] == _SHA


@pytest.mark.parametrize("content", ["", "   ", "\n\n\t"])
def test_parse_markdown_document_skips_empty_content(content: str) -> None:
    source_id, document = parse_markdown_document(
        identity=_IDENTITY,
        commit_sha="c" * 40,
        blob_sha=_SHA,
        path="EMPTY.md",
        content=content,
    )
    assert source_id
    assert document is None
