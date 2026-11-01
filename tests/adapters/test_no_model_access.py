# tests/adapters/test_no_model_access.py
"""The layer rule, as a test: adapters and services never read the models.

Two rules, both previously enforced only by review:

* an adapter talks to a controller; touching ``models`` from a protocol adapter
  is how a domain query ends up written three times, once per protocol;
* a service is a capability (the GitHub API), not a flow: a service importing
  ``models`` at runtime means a flow has leaked into the capability layer.

A ``TYPE_CHECKING`` import is allowed: naming a model in an annotation costs no
access. What is forbidden is an import that *executes*, at module level or inside
a function.

Controllers are the exception by design -- they are where the model operations a
flow is built from live -- so nothing here restricts them.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "rhosocial" / "github_ci_tracker"
PACKAGE = "rhosocial.github_ci_tracker"
MODELS = f"{PACKAGE}.models"


def _absolute(node: ast.ImportFrom, package: str) -> str:
    """Resolve a ``from ...x import y`` to its absolute module name."""
    if node.level == 0:
        return node.module or ""
    kept = package.split(".")[: len(package.split(".")) - (node.level - 1)]
    if node.module:
        kept.append(node.module)
    return ".".join(kept)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


class _ModelImports(ast.NodeVisitor):
    """Collect runtime imports of the models package, skipping type-only blocks."""

    def __init__(self, package: str) -> None:
        self.package = package
        self.found: list[str] = []

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            # The body never runs in production; the else branch does.
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _absolute(node, self.package)
        if module == MODELS or module.startswith(f"{MODELS}."):
            self.found.append(f"line {node.lineno}: {module}")


def _model_imports_in(source: str, package: str) -> list[str]:
    collector = _ModelImports(package)
    collector.visit(ast.parse(source))
    return collector.found


def _model_imports_in_tree(root: Path) -> dict[str, list[str]]:
    offenders: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        package = ".".join((PACKAGE, *path.relative_to(SRC).with_suffix("").parts[:-1]))
        found = _model_imports_in(path.read_text(encoding="utf-8"), package)
        if found:
            offenders[str(path.relative_to(SRC))] = found
    return offenders


def test_no_adapter_imports_the_models() -> None:
    """A protocol adapter reaches data through a controller, never a model."""
    offenders = _model_imports_in_tree(SRC / "adapters")

    assert not offenders, "adapters reach the models directly:\n" + "\n".join(
        f"  {path}: {', '.join(found)}" for path, found in offenders.items()
    )


def test_no_service_imports_the_models_at_runtime() -> None:
    """A service is a capability; a flow that reads rows lives in a controller."""
    offenders = _model_imports_in_tree(SRC / "services")

    assert not offenders, "services reach the models directly:\n" + "\n".join(
        f"  {path}: {', '.join(found)}" for path, found in offenders.items()
    )


def test_the_scanner_sees_relative_and_lazy_imports() -> None:
    """Guard the guard: the check must catch what it exists to catch."""
    source = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from ....models import AsyncWorkflowRun\n"
        "def load():\n"
        "    from ....models import AsyncJob\n"
        "    return AsyncJob\n"
    )

    found = _model_imports_in(source, "rhosocial.github_ci_tracker.adapters.mcp.tools")

    assert found == [
        "line 6: rhosocial.github_ci_tracker.models"
    ], "the type-only import must be allowed, the lazy one must not"


def test_the_scanner_accepts_an_indirect_import() -> None:
    source = "from ....controllers import QueryController\n"

    found = _model_imports_in(source, "rhosocial.github_ci_tracker.adapters.mcp.tools")

    assert found == []
