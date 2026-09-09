"""Smoke tests for the project package."""

from importlib.resources import files

import reporationale


def test_package_is_importable() -> None:
    """The installed project exposes its top-level package."""
    assert reporationale.__name__ == "reporationale"


def test_streamlit_theme_is_packaged_with_the_application() -> None:
    """The installed UI can load its required stylesheet from package data."""
    theme = files("reporationale").joinpath("static", "theme.css")

    assert theme.is_file()
    assert "--rr-bg:" in theme.read_text(encoding="utf-8")
