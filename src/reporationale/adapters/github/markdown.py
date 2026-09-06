"""Normalization of Markdown files from a resolved default-branch revision.

Provides the strict response models and per-item validation used by
`GitHubClient.list_markdown_documents`, which uses the Git Trees and Git
Blobs REST endpoints to build a deterministic inventory of `.md`/
`.markdown` files at one exact commit and normalizes each non-empty file
as one `SourceDocument`. Does not chunk file content; the whole file is
the source document (chunking belongs to the retrieval-index layer).
"""

import base64
import binascii
import re
from dataclasses import dataclass
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from reporationale.adapters.github._shared import require_github_platform
from reporationale.adapters.github.errors import GitHubMalformedResponse
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

# A Git object ID is lowercase hex; 40 characters (SHA-1) is standard today,
# but this deliberately does not hard-code exactly 40 (a future SHA-256
# repository would use 64).
_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40,64}")
_MARKDOWN_SUFFIXES = (".md", ".markdown")
_SUPPORTED_BLOB_ENCODING = "base64"
_SYMLINK_MODE = "120000"


def _validate_object_id(value: str) -> str:
    if not _OBJECT_ID_PATTERN.fullmatch(value):
        raise ValueError("must be a valid lowercase-hex Git object identifier")
    return value


class _GitCommitReferenceTreeResponse(BaseModel):
    """The nested `commit.tree` object of a "Get a commit" REST response."""

    model_config = ConfigDict(extra="ignore", strict=True)

    sha: str = Field(min_length=1)

    @field_validator("sha")
    @classmethod
    def sha_valid(cls, value: str) -> str:
        return _validate_object_id(value)


class _GitCommitReferenceDetailResponse(BaseModel):
    """The nested `commit` object of a "Get a commit" REST response."""

    model_config = ConfigDict(extra="ignore", strict=True)

    tree: _GitCommitReferenceTreeResponse


class _GitCommitReferenceResponse(BaseModel):
    """The subset of the GitHub "Get a commit" REST response that is used
    to resolve a branch name to an exact commit and root-tree identity."""

    model_config = ConfigDict(extra="ignore", strict=True)

    sha: str = Field(min_length=1)
    commit: _GitCommitReferenceDetailResponse

    @field_validator("sha")
    @classmethod
    def sha_valid(cls, value: str) -> str:
        return _validate_object_id(value)


def parse_commit_reference(raw: object) -> tuple[str, str]:
    """Validate a "Get a commit" response and return `(commit_sha, tree_sha)`.

    Raises `GitHubMalformedResponse` only after the originating `except`
    block has fully exited, so the Pydantic `ValidationError` is not
    reachable from `__cause__`/`__context__`.
    """
    malformed = False
    parsed: _GitCommitReferenceResponse | None = None
    try:
        parsed = _GitCommitReferenceResponse.model_validate(raw)
    except ValidationError:
        malformed = True

    if malformed or parsed is None:
        raise GitHubMalformedResponse(
            "The GitHub API commit-reference response failed validation."
        )
    return parsed.sha, parsed.commit.tree.sha


class _GitTreeEntryResponse(BaseModel):
    """One entry of a GitHub "Get a tree" REST response."""

    model_config = ConfigDict(extra="ignore", strict=True)

    path: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    type: str = Field(min_length=1)
    sha: str = Field(min_length=1)

    @field_validator("sha")
    @classmethod
    def sha_valid(cls, value: str) -> str:
        return _validate_object_id(value)

    @field_validator("path")
    @classmethod
    def path_must_be_safe(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be blank")
        if value.startswith("/") or value.endswith("/"):
            raise ValueError("path must not be absolute or end with '/'")
        segments = value.split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise ValueError("path must not contain an unsafe segment")
        return value


class _GitTreeResponse(BaseModel):
    """The subset of the GitHub "Get a tree" REST response that is used."""

    model_config = ConfigDict(extra="ignore", strict=True)

    sha: str = Field(min_length=1)
    truncated: bool
    tree: list[_GitTreeEntryResponse]

    @field_validator("sha")
    @classmethod
    def sha_valid(cls, value: str) -> str:
        return _validate_object_id(value)


@dataclass(frozen=True)
class GitTreeEntry:
    """One validated tree entry: a file, directory, or submodule reference."""

    path: str
    mode: str
    type: str
    sha: str


def parse_git_tree(raw: object) -> tuple[str, list[GitTreeEntry], bool]:
    """Validate a "Get a tree" response and return `(sha, entries, truncated)`.

    `sha` is the tree object's own identity as reported by the response;
    the caller must check it against the tree sha it actually requested,
    since this function has no way to know what was requested. `truncated
    =True` means the response is incomplete; the caller must not treat the
    returned entries as the complete tree in that case.

    Raises `GitHubMalformedResponse` only after the originating `except`
    block has fully exited, so the Pydantic `ValidationError` is not
    reachable from `__cause__`/`__context__`.
    """
    malformed = False
    parsed: _GitTreeResponse | None = None
    try:
        parsed = _GitTreeResponse.model_validate(raw)
    except ValidationError:
        malformed = True

    if malformed or parsed is None:
        raise GitHubMalformedResponse("The GitHub API tree response failed validation.")
    entries = [
        GitTreeEntry(path=entry.path, mode=entry.mode, type=entry.type, sha=entry.sha)
        for entry in parsed.tree
    ]
    return parsed.sha, entries, parsed.truncated


def is_markdown_path(path: str) -> bool:
    """A `.md`/`.markdown` file, matched case-insensitively."""
    lowered = path.lower()
    return lowered.endswith(_MARKDOWN_SUFFIXES)


def is_regular_file_blob(entry: GitTreeEntry) -> bool:
    """A plain file blob: not a directory, submodule, or symlink."""
    return entry.type == "blob" and entry.mode != _SYMLINK_MODE


class _GitBlobResponse(BaseModel):
    """The subset of the GitHub "Get a blob" REST response that is used."""

    model_config = ConfigDict(extra="ignore", strict=True)

    sha: str = Field(min_length=1)
    content: str
    encoding: str = Field(min_length=1)

    @field_validator("sha")
    @classmethod
    def sha_valid(cls, value: str) -> str:
        return _validate_object_id(value)

    @field_validator("encoding")
    @classmethod
    def encoding_must_be_supported(cls, value: str) -> str:
        if value != _SUPPORTED_BLOB_ENCODING:
            raise ValueError(f"unsupported blob encoding: {value!r}")
        return value


def decode_markdown_blob(raw: object, *, expected_sha: str) -> str:
    """Validate a "Get a blob" response and return its decoded UTF-8 text.

    Rejects a blob whose `sha` does not match the tree entry that was
    requested, malformed base64 (GitHub line-wraps its base64 content with
    CR/LF only; those two characters are stripped before decoding, but any
    other character outside the base64 alphabet, including a plain space
    or tab, is not tolerated), and content that is not valid UTF-8.
    """
    malformed_reason: str | None = None
    parsed: _GitBlobResponse | None = None

    try:
        parsed = _GitBlobResponse.model_validate(raw)
    except ValidationError:
        malformed_reason = "The GitHub API blob response failed validation."

    text: str | None = None
    if malformed_reason is None and parsed is not None:
        if parsed.sha != expected_sha:
            malformed_reason = (
                "The GitHub API blob sha does not match the requested tree entry."
            )
        else:
            unwrapped_content = parsed.content.replace("\r", "").replace("\n", "")
            try:
                raw_bytes = base64.b64decode(unwrapped_content, validate=True)
            except (binascii.Error, ValueError):
                malformed_reason = "The GitHub API blob content is not valid base64."
            else:
                try:
                    text = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    malformed_reason = "The GitHub API blob content is not valid UTF-8."

    if malformed_reason is not None:
        raise GitHubMalformedResponse(malformed_reason)
    if text is None:
        raise AssertionError("unreachable: text or malformed_reason must be set")
    return text


def parse_markdown_document(
    *,
    identity: RepositoryIdentity,
    commit_sha: str,
    blob_sha: str,
    path: str,
    content: str,
) -> tuple[str, SourceDocument | None]:
    """Normalize one validated, decoded Markdown file, or skip it (return a
    `None` document) if it is empty or whitespace-only.

    `path` must already be a validated, safe, repository-relative path
    (see `_GitTreeEntryResponse.path_must_be_safe`). The source ID and
    canonical blob URL percent-encode it so a path containing whitespace
    or non-ASCII characters cannot violate the `SourceDocument` identity
    contract or produce an unsafe URL.

    Always returns the deterministic `source_id`, even when skipped for
    empty content, so the caller can still detect a duplicate path/identity
    across the collected tree regardless of whether this call produced a
    document.
    """
    require_github_platform(identity)

    encoded_path = quote(path, safe="/")
    source_id = f"github:{identity.owner}/{identity.name}:markdown:{encoded_path}"

    if not content.strip():
        return source_id, None

    source_url = (
        f"https://github.com/{identity.owner}/{identity.name}/blob/"
        f"{commit_sha}/{encoded_path}"
    )

    document = SourceDocument(
        source_id=source_id,
        platform="github",
        repository=identity.repository,
        source_type="markdown",
        text=content,
        source_url=source_url,
        created_at=None,
        updated_at=None,
        parent_source_id=None,
        item_number=None,
        title=None,
        metadata={
            "path": path,
            "blob_sha": blob_sha,
            "commit_sha": commit_sha,
        },
    )
    return source_id, document
