"""Shared, deterministic GitHub REST test fixtures and mock-transport helpers.

Not a test module itself (no `test_` prefix); imported by the GitHub adapter
test modules that live alongside it in this directory.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "github"


def load_fixture(name: str) -> Any:
    """Load a deterministic GitHub JSON fixture by file name."""
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def json_response(
    status_code: int, payload: Any, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    """Build a deterministic mocked GitHub JSON response."""
    return httpx.Response(status_code, json=payload, headers=headers or {})


def raw_response(
    status_code: int, content: bytes, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    """Build a deterministic mocked GitHub response with raw (non-JSON) content."""
    return httpx.Response(status_code, content=content, headers=headers or {})


RequestHandler = Callable[[httpx.Request], httpx.Response]


def mock_transport(handler: RequestHandler) -> httpx.MockTransport:
    """Wrap a request handler as an `httpx` transport for a `GitHubClient`."""
    return httpx.MockTransport(handler)
