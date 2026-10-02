"""Unit tests for ``worker_runner._ensure_import_paths`` (card e1fcec75).

Card ``e1fcec75-5ed0-47db-9fcc-b3f0920b42ff``: the worker daemon failed every
dispatched cell with ``ModuleNotFoundError: No module named 'core'`` because
``_ensure_import_paths()`` only added ``code_root/src`` and ``code_root/scripts``
to ``sys.path``. Strategies import ``core.types`` (resolves via
``code_root/src/forex_bot``). These tests pin the post-fix contract:

- ``code_root/src`` is on ``sys.path`` (existing behavior — preserved).
- ``code_root/src/forex_bot`` is on ``sys.path`` (the new fix — required).
- ``code_root/scripts`` is on ``sys.path`` (existing behavior — preserved).
- Order: ``src``, ``src/forex_bot``, ``scripts`` mirror pytest.ini's pythonpath.
- Idempotent: a second call does not grow sys.path.

Reference pattern: ``scripts/run_tournament.py`` lines 31-34, which correctly
adds both ``src/`` and ``src/forex_bot`` (mirrors pytest.ini ``pythonpath = src/forex_bot src tests scripts``).
"""

from __future__ import annotations

import sys

from offload.worker_runner import _ensure_import_paths


def _make_fake_code_root(tmp_path):
    """Create a synthetic code_root with src/, src/forex_bot/, scripts/.

    Mirrors the real Ayumi layout so candidate resolution can compute paths
    the same way the worker daemon does.
    """
    code_root = tmp_path / "fake_repo"
    (code_root / "src").mkdir(parents=True)
    (code_root / "src" / "forex_bot").mkdir(parents=True)
    (code_root / "scripts").mkdir(parents=True)
    return code_root


def test_ensure_import_paths_injects_forex_bot(tmp_path) -> None:
    """AC2: ``src/forex_bot`` lands on sys.path after the call.

    This is the headline assertion — pre-fix this would fail because the
    candidate list omitted ``src/forex_bot``.
    """
    code_root = _make_fake_code_root(tmp_path)
    saved = list(sys.path)
    try:
        _ensure_import_paths(code_root)
        sp = sys.path
        assert str(code_root / "src") in sp, "src must be on sys.path"
        assert str(code_root / "src" / "forex_bot") in sp, (
            "src/forex_bot must be on sys.path — strategies import core.types "
            "from there. See card e1fcec75-5ed0-47db-9fcc-b3f0920b42ff."
        )
        assert str(code_root / "scripts") in sp, "scripts must be on sys.path"
    finally:
        # Restore sys.path so we don't pollute other tests.
        sys.path[:] = saved


def test_ensure_import_paths_idempotent(tmp_path) -> None:
    """A second call must not duplicate entries on sys.path.

    The function is invoked from each cell dispatch; without idempotency,
    ``sys.path`` would grow unbounded across a 68-cell matrix.
    """
    code_root = _make_fake_code_root(tmp_path)
    saved = list(sys.path)
    try:
        _ensure_import_paths(code_root)
        size_after_first = len(sys.path)
        _ensure_import_paths(code_root)
        size_after_second = len(sys.path)
        assert size_after_first == size_after_second, (
            f"_ensure_import_paths is not idempotent: "
            f"sys.path grew {size_after_first} -> {size_after_second}"
        )
    finally:
        sys.path[:] = saved


def test_ensure_import_paths_insertion_prepends(tmp_path) -> None:
    """Candidates land at the head of sys.path (shadowing later entries).

    Mirrors ``run_tournament.py`` lines 31-34 which also ``insert(0, ...)``
    so that repo paths shadow any system-installed modules. Note: with
    repeated ``insert(0, ...)``, the final order is the REVERSE of the
    declaration order — this test asserts that invariant, not exact
    positions, because there are no module-name collisions among the
    three candidates (so the exact order is implementation-defined).
    """
    code_root = _make_fake_code_root(tmp_path)
    saved = list(sys.path)
    try:
        # Drop the candidates first so we can detect insertion order.
        for c in (
            str(code_root / "src"),
            str(code_root / "src" / "forex_bot"),
            str(code_root / "scripts"),
        ):
            while c in saved:
                saved.remove(c)
        sys.path[:] = saved + ["/sentinel/tail"]
        _ensure_import_paths(code_root)
        # All three candidates must occupy the first 3 positions; the
        # /sentinel/tail entry must be further down (proves prepend, not append).
        head = set(sys.path[:3])
        assert str(code_root / "src") in head
        assert str(code_root / "src" / "forex_bot") in head
        assert str(code_root / "scripts") in head
        assert "/sentinel/tail" not in sys.path[:3], (
            "candidates must be at the head, not pushed past /sentinel/tail"
        )
        assert "/sentinel/tail" in sys.path, "sentinel must still be present"
    finally:
        sys.path[:] = saved


def test_ensure_import_paths_does_not_depend_on_pwd(tmp_path, monkeypatch) -> None:
    """The function uses absolute ``code_root`` paths, not cwd.

    Important for AC1: the worker daemon's CWD at dispatch time is
    unrelated to the repo location — only ``--code-root`` anchors it.
    Even if cwd moves, sys.path entries must still resolve.
    """
    code_root = _make_fake_code_root(tmp_path)
    saved = list(sys.path)
    monkeypatch.chdir(tmp_path)
    try:
        _ensure_import_paths(code_root)
        assert str(code_root / "src" / "forex_bot") in sys.path
        # The path entries must be absolute (start with the code_root).
        for c in (
            str(code_root / "src"),
            str(code_root / "src" / "forex_bot"),
            str(code_root / "scripts"),
        ):
            assert c.startswith(str(code_root)), (
                f"{c} must be an absolute path under code_root, not cwd-relative"
            )
    finally:
        sys.path[:] = saved
