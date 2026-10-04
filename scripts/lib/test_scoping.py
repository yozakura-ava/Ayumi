#!/usr/bin/env python3
"""
test_scoping.py — Shared test-file discovery for builder tooling.

Provides:
- scope_tests(): Map source module paths to their corresponding test files
  under ``tests/``. Extracted from builder_quality_gate.py:check_targeted_tests
  so that the BQG, run_test_scope.sh, and the pre-commit hook all share one
  implementation.

Usage:
    from scripts.lib.test_scoping import scope_tests

    test_files = scope_tests([Path("scripts/foo.py")], workspace=Path("/root/.openclaw/workspace"))
"""

from __future__ import annotations

from pathlib import Path

# Coupled BQES tests (card 7754c16e — L2-A adoption follow-up).
#
# ADR-013 §1 says the on-disk manifest at ``data/bqes/manifest.v*.yaml``
# is a pure function of live ``bqes_check.py`` constants. The drift test
# at ``tests/lobster/test_bqes_manifest.py`` catches the failure mode
# where the constants moved but the manifest didn't. Without coupling,
# a change to ``bqes_check.py`` only ever pulls in ``test_bqes_check_gates.py``
# (stem ``bqes_check``) and the drift test stays dormant until someone
# remembers to run it explicitly.
#
# Pattern matched: any Python file under ``scripts/lobster/`` whose stem
# is either ``manifest`` or starts with ``bqes_``. ``manifest.py`` is
# included so that schema/parsing changes re-run the drift test too;
# in practice the stem match for ``manifest`` already covers that case,
# but coupling here keeps the rule explicit and self-documenting.
_BQES_COUPLED_TESTS: tuple[Path, ...] = (
    Path("tests/lobster/test_bqes_manifest.py"),
)


def _is_bqes_source(path: Path, workspace: Path) -> bool:
    """Return True iff ``path`` is a BQES subsystem file.

    BQES subsystem = ``<workspace>/scripts/lobster/bqes_*.py`` OR
    ``<workspace>/scripts/lobster/manifest.py``. Resolves ``path`` and
    checks the workspace-relative path; returns False for anything
    outside the workspace or in a different subdirectory so unrelated
    ``scripts/lobster/*.py`` files (e.g. ``workboard_collect.py``,
    ``combine_review_verdicts.py``) are not over-coupled.
    """
    if path.suffix != ".py":
        return False
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except (ValueError, OSError):
        return False
    parts = rel.parts
    if len(parts) < 3 or parts[0] != "scripts" or parts[1] != "lobster":
        return False
    stem = path.stem
    return stem == "manifest" or stem.startswith("bqes_")


def scope_tests(changed_files: list[Path], workspace: Path) -> list[Path]:
    """Map source module paths to corresponding test files under ``tests/``.

    For each Python source file, extract its stem (module name) and search
    ``<workspace>/tests/`` for files matching ``*<stem>*.py`` (excluding
    ``.pyc`` and ``__pycache__``). Returns a deduplicated list of test file
    paths, preserving insertion order.

    In addition to stem-based matching, the function pulls in coupled
    BQES subsystem tests (``_BQES_COUPLED_TESTS``) whenever a BQES
    source file (``scripts/lobster/bqes_*.py`` or ``manifest.py``)
    appears in ``changed_files`` — see card 7754c16e for the rationale.

    Args:
        changed_files: List of source file paths (absolute or relative).
        workspace: Workspace root containing the ``tests/`` directory.

    Returns:
        Deduplicated list of test file ``Path`` objects. Empty if no matches.
    """
    tests_dir = workspace / "tests"
    if not tests_dir.is_dir():
        return []

    seen: set[Path] = set()
    result: list[Path] = []

    for f in changed_files:
        module_name = f.stem

        # conftest.py files are pytest fixtures, not test files. Running
        # ``pytest tests/conftest.py`` exits 5 ("no tests ran"), which
        # surfaced as a false-positive failure in check_targeted_tests()
        # for every sibling conftest under tests/ (card b30fc832, Aug 2026
        # cron-test-greenup). Map conftest changes to the enclosing
        # tests/ directory's test files so the fixtures' dependents are
        # actually re-run instead.
        if module_name == "conftest":
            target_dir = _conftest_target_dir(f, tests_dir)
            for candidate in _collect_test_py(target_dir):
                resolved = candidate.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    result.append(resolved)
            continue

        if not module_name or module_name == "__init__":
            continue

        # Scope the stem-match to the changed file's project directory
        # instead of the entire tests/ tree (card 7b3dada4 — pre-commit
        # BQG scoping explosion, 2026-10-02). Without this scope, a
        # ``src/<project>/loader.py`` change (stem ``loader``) matches
        # ``tests/**/*loader*.py`` ANYWHERE in the workspace — turning a
        # single src/<project>/ commit into ~700 pytest targets and a
        # 180s timeout. _test_roots_for() returns the project-local
        # ``src/<project>/tests/`` and the top-level ``tests/<project>/``
        # (with dash/underscore variants) so the workspace's split layout
        # (some projects keep tests inside ``src/<project>/tests/``,
        # others use the top-level ``tests/<project>/``) keeps working.
        # ``scripts/`` and other paths fall through to the existing
        # whole-tests-dir behavior so non-src commits are unaffected.
        for test_root in _test_roots_for(f, workspace, tests_dir):
            for candidate in test_root.rglob(f"*{module_name}*"):
                # Only match .py files — rglob also catches .pyc in __pycache__/
                # which causes pytest to exit code 4 ("no tests ran") as a
                # false failure.
                if candidate.suffix != ".py":
                    continue
                # Defensive: never return a conftest.py from a wildcard match,
                # even if a module name happens to contain "conftest". Same
                # exit-5 root cause as above.
                if candidate.stem == "conftest":
                    continue
                resolved = candidate.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    result.append(resolved)

    # Coupled-test pull-in (card 7754c16e). Walk changed_files a second
    # time — cheap, and keeps the stem-match loop above untouched so the
    # existing tests/lib/test_test_scoping.py parametrized suite keeps
    # its documented contract. A single matching BQES source is enough
    # to add each coupled test exactly once; ``seen`` enforces dedupe
    # across repeated matches and across the stem loop above.
    for f in changed_files:
        if _is_bqes_source(f, workspace):
            for coupled_rel in _BQES_COUPLED_TESTS:
                coupled = (workspace / coupled_rel).resolve()
                if coupled.exists() and coupled not in seen:
                    seen.add(coupled)
                    result.append(coupled)
            break

    return result


def _test_roots_for(source_file: Path, workspace: Path, tests_dir: Path) -> list[Path]:
    """Return the test directories to search for ``source_file``'s tests.

    Scope map (card 7b3dada4 — pre-commit BQG scoping explosion):

    - ``src/<project>/<file>.py`` → ``src/<project>/tests/`` (project-local)
      AND ``tests/<project>/`` (top-level). The local variant exists for
      projects that ship tests alongside code (``src/ava_gate/tests/``,
      ``src/crypto_monitor/tests/``); the top-level variant exists for
      projects whose tests live at the workspace top level
      (``tests/entity-intelligence/``, ``tests/portfolio-intelligence/``,
      ``tests/lovense_mcp/``).
    - ``scripts/<file>.py`` → ``tests/`` (top-level, existing behavior).
    - Other paths (top-level files, ``tests/`` itself) → ``tests/`` (top-level,
      existing behavior).

    The workspace uses a mixed naming convention: some projects use
    underscores in ``src/`` and dashes in ``tests/`` (e.g. tracked
    ``src/entity_intelligence/`` with top-level ``tests/entity-intelligence/``).
    To handle both forms without coupling this helper to a project
    registry, we check the project's exact name plus dash/underscore
    variants for both ``src/<name>/tests/`` and ``tests/<name>/``. Returns
    an empty list when no scoped directory matches — the src/ commit
    should NOT pull in the whole test suite as a fallback, since that's
    exactly the regression we're fixing (card 7b3dada4).
    """
    try:
        rel = source_file.resolve().relative_to(workspace.resolve())
    except (ValueError, OSError):
        return [tests_dir]
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "src":
        project = parts[1]
        # Generate candidate project names: exact + dash + underscore forms.
        candidates: set[str] = {project}
        if "_" in project:
            candidates.add(project.replace("_", "-"))
        if "-" in project:
            candidates.add(project.replace("-", "_"))
        roots: list[Path] = []
        for cand in candidates:
            local = workspace / "src" / cand / "tests"
            if local.is_dir() and local not in roots:
                roots.append(local)
            top = tests_dir / cand
            if top.is_dir() and top not in roots:
                roots.append(top)
        # Empty list when no test dir exists for the project — INTENTIONAL.
        # A src/<project>/ commit in a project without tests must not pull
        # in the whole tests/ tree (that would re-introduce the scoping
        # explosion). If the project needs test coverage, that's a
        # separate issue; the gate surfaces the empty selection as
        # ``test_files = []`` and skips the pytest invocation.
        return roots
    # scripts/ and other top-level paths → tests/ (existing behavior).
    return [tests_dir]


def _conftest_target_dir(conftest_path: Path, tests_dir: Path) -> Path:
    """Pick the tests/ subdirectory whose tests depend on ``conftest_path``.

    Rules:
    - If ``conftest_path`` lives somewhere under ``tests/`` (e.g.
      ``tests/quarantine/conftest.py``), use its parent directory so only
      dependents of that conftest get re-run.
    - Otherwise (e.g. a conftest in a source-tree folder, or outside the
      repo), fall back to the workspace ``tests/`` root as the safest
      blanket coverage.
    """
    try:
        rel = conftest_path.resolve().relative_to(tests_dir.resolve())
    except ValueError:
        return tests_dir
    parent = (tests_dir / rel).parent
    return parent if parent.is_dir() else tests_dir


def _collect_test_py(directory: Path) -> list[Path]:
    """Return sorted .py test files under ``directory``.

    Filters conftest.py and __init__.py — both exit pytest with code 5
    ("no tests ran") and would surface as false-positive gate failures
    (card b30fc832, Aug 2026 cron-test-greenup). Sorts for deterministic
    per-file reporting across runs.
    """
    if not directory.is_dir():
        return []
    return sorted(
        candidate
        for candidate in directory.rglob("*.py")
        if candidate.suffix == ".py"
        and candidate.stem != "conftest"
        and candidate.stem != "__init__"
        and "__pycache__" not in candidate.parts
    )
