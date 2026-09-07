"""Small dependency-direction checks for the modular-monolith boundaries."""

import ast
from pathlib import Path


def test_adapters_do_not_import_application_modules() -> None:
    adapters_dir = Path("src/reporationale/adapters")
    violations: list[str] = []

    for path in sorted(adapters_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "reporationale.application"
            ):
                violations.append(f"{path}:{node.lineno}: {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("reporationale.application"):
                        violations.append(f"{path}:{node.lineno}: {alias.name}")

    assert violations == []
