"""Smoke tests for the project package."""

import reporationale


def test_package_is_importable() -> None:
    """The installed project exposes its top-level package."""
    assert reporationale.__name__ == "reporationale"
