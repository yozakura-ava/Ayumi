#!/usr/bin/env python3
"""cTrader adapter callback linter.

Scans src/forex_bot/adapters/ctrader/ for cTrader callback registrations such
as setConnectCallback / setDisconnectCallback / setMessageReceivedCallback
and flags:
  - common typos (e.g. setConnectedCallback vs setConnectCallback)
  - callback method names that are not defined on the enclosing class
  - registration calls that occur outside an adapter module

Can run standalone or as a pre-commit hook:
  python3 scripts/ctrader_callback_linter.py
  python3 scripts/ctrader_callback_linter.py src/forex_bot/adapters/ctrader/

Exit codes:
  0 = no issues
  1 = lint issues found
  2 = internal error
"""

from __future__ import annotations

import ast
import logging
import sys
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger("ctrader_callback_linter")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Canonical cTrader Client callback setters. Any deviation is likely a typo.
CANONICAL_CALLBACKS = {
    "setConnectedCallback",
    "setDisconnectedCallback",
    "setMessageReceivedCallback",
}

# Known typo patterns and their likely corrections.
COMMON_TYPOS = {
    "setConnectCallback": "setConnectedCallback",
    "setDisconnectCallback": "setDisconnectedCallback",
    "setMessageRecievedCallback": "setMessageReceivedCallback",
    "setMessageReceivedCallBack": "setMessageReceivedCallback",
    "setConnectedCallBack": "setConnectedCallback",
    "setDisconnectedCallBack": "setDisconnectedCallback",
    "onConnectedCallback": "setConnectedCallback",
    "onDisconnectedCallback": "setDisconnectedCallback",
}


def _callback_methods_for_class(tree: ast.AST, class_name: str) -> set[str]:
    """Collect all method names defined on a class."""
    methods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(item.name)
    return methods


def _enclosing_class(node: ast.AST, tree: ast.AST) -> str | None:
    """Find the nearest enclosing class for an AST node."""
    # Build parent map
    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent

    current: ast.AST | None = node
    while current is not None:
        parent = parent_map.get(current)
        if isinstance(parent, ast.ClassDef):
            return parent.name
        current = parent
    return None


def _is_self_attribute(node: ast.expr) -> str | None:
    """Return method name if node is self.<method>."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


def _collect_issues(path: Path, source: str) -> list[dict]:
    """Parse a Python file and collect callback-related issues."""
    issues: list[dict] = []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [
            {
                "path": str(path),
                "line": exc.lineno or 1,
                "type": "syntax_error",
                "message": str(exc),
            }
        ]

    # Pre-compute class method definitions
    class_methods: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_methods[node.name] = _callback_methods_for_class(tree, node.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if not isinstance(func, ast.Attribute):
            continue

        method_name = func.attr
        if method_name not in CANONICAL_CALLBACKS and method_name not in COMMON_TYPOS:
            continue

        line = getattr(node, "lineno", 1)

        # Typo detection
        if method_name in COMMON_TYPOS:
            expected = COMMON_TYPOS[method_name]
            issues.append(
                {
                    "path": str(path),
                    "line": line,
                    "type": "typo",
                    "message": f"likely typo '{method_name}' should be '{expected}'",
                }
            )
            continue

        # Registration outside a class is suspicious
        enclosing_class = _enclosing_class(node, tree)
        if enclosing_class is None:
            issues.append(
                {
                    "path": str(path),
                    "line": line,
                    "type": "registration_outside_class",
                    "message": f"{method_name} called outside a class",
                }
            )
            continue

        # Check that the callback argument is a method defined on the class
        callback_target: str | None = None
        if node.args:
            callback_target = _is_self_attribute(node.args[0])

        if callback_target is None and node.args:
            # Could be a local variable or a module-level function - flag it
            arg = node.args[0]
            issues.append(
                {
                    "path": str(path),
                    "line": line,
                    "type": "non_method_callback",
                    "message": f"{method_name} registered with non-self argument ({type(arg).__name__})",
                }
            )
            continue

        if callback_target and callback_target not in class_methods.get(enclosing_class, set()):
            issues.append(
                {
                    "path": str(path),
                    "line": line,
                    "type": "undefined_callback",
                    "message": (
                        f"{method_name} references self.{callback_target}, "
                        f"but '{callback_target}' is not defined in class '{enclosing_class}'"
                    ),
                }
            )

    return issues


def _scan_directory(root: Path) -> Iterable[Path]:
    """Yield Python files under root."""
    if root.is_file():
        yield root
        return
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        yield path


def main() -> int:
    argv = sys.argv[1:]
    if argv:
        targets = [Path(p).resolve() for p in argv]
    else:
        project_root = Path(__file__).resolve().parents[1]
        targets = [project_root / "src" / "forex_bot" / "adapters" / "ctrader"]

    all_issues: list[dict] = []
    files_scanned = 0
    for target in targets:
        if not target.exists():
            logger.error("Target not found: %s", target)
            return 2
        for path in _scan_directory(target):
            try:
                source = path.read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.error("Cannot read %s: %s", path, exc)
                return 2
            files_scanned += 1
            issues = _collect_issues(path, source)
            all_issues.extend(issues)

    if not all_issues:
        print(f"No callback issues found in {files_scanned} file(s).")
        return 0

    print(f"Found {len(all_issues)} issue(s) across {files_scanned} file(s):")
    for issue in all_issues:
        print(f"  {issue['path']}:{issue['line']} [{issue['type']}] {issue['message']}")

    return 1


if __name__ == "__main__":
    sys.exit(main())
