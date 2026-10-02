"""Tests for card 758273a7-c3f1-41fa-95dc-7384c260acd7 — harness isolation.

ruff: noqa: S608 — ruff flags ``f"Found pattern at offsets: {[m.start() for m in
matches]}"`` as a SQL-injection vector. This is a false positive: ``.start()``
returns a regex match offset (``int``), not SQL. The test exercises regex
source matching, not database queries.

Card 758273a7 (URGENT, 2026-09-08): the blend harness previously wrote
to LIVE forward-test state paths, contaminating ``data/kill_switches/
global.state`` and ``data/risk_state_blend.json`` when invoked as root.
Root-run re-run #3 poisoned live engine startup state during the Phase 1
restart 16:30 UTC incident.

These tests guard against regression of:

  * Default isolated per-run state dir (never LIVE paths).
  * Startup guard refuses on live PID file presence (kill -0 probe).
  * Startup guard refuses on foreign-ownership of live state files.
  * ``--allow-foreign-ownership`` bypass (operator-only flag).
  * ``--keep-state-dir`` preserves the isolated dir for post-mortem.
  * Isolated ``KillSwitchManager`` writes ONLY to its own state_dir, never
    to ``data/kill_switches/`` even when activations fire.

The tests monkeypatch ``LIVE_*`` paths inside the harness module so the
suite runs without touching real production state. The integration
check (mtime + sha256 before/after) is performed at proof time, not in
unit tests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# Ensure src/forex_bot and scripts/ are importable for
# adapters.ctrader.kill_switch and backtest_blend_harness.
_REPO_ROOT = Path(__file__).resolve().parents[3]
for sub in ("src/forex_bot", "scripts"):
    p = str(_REPO_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

import scripts.backtest_blend_harness as harness  # noqa: E402

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_fake_live_files(monkeypatch, tmp_path: Path):
    """Create fake LIVE state files under tmp_path and monkeypatch the
    harness's ``LIVE_*`` constants to point at them. Returns the live root.
    """
    live_root = tmp_path / "fake_live"
    live_root.mkdir()
    kill_switch_dir = live_root / "data" / "kill_switches"
    kill_switch_dir.mkdir(parents=True)
    live_state = kill_switch_dir / "global.state"
    live_state.write_text(json.dumps({"active": False, "version": 1}) + "\n")
    live_risk = live_root / "data" / "risk_state_blend.json"
    live_risk.write_text(json.dumps({"starting_balance": 10000.0}) + "\n")
    pid_file = live_root / "data" / "forward_test.pid"

    monkeypatch.setattr(harness, "LIVE_FORWARD_TEST_PID", pid_file)
    monkeypatch.setattr(harness, "LIVE_KILL_SWITCH_GLOBAL_STATE", live_state)
    monkeypatch.setattr(harness, "LIVE_RISK_STATE_BLEND", live_risk)
    return live_root


def _harness_source_path() -> Path:
    return _REPO_ROOT / "scripts" / "backtest_blend_harness.py"


# ── AC1: default state dir is isolated (tempfile, never LIVE) ──────────────


def test_default_state_dir_is_isolated_temp_dir():
    """``_resolve_state_dir`` returns a tempdir under tempfile.gettempdir()
    by default — never ``data/kill_switches`` or ``data/risk_state_blend.json``.
    Card 758273a7: harness must not touch LIVE paths by default.
    """
    args = argparse.Namespace(state_dir=None, allow_foreign_ownership=False, keep_state_dir=False)
    resolved = harness._resolve_state_dir(args)
    try:
        assert resolved.exists(), f"Isolated state dir must exist: {resolved}"
        assert str(resolved).startswith(tempfile.gettempdir()), (
            f"Default state dir must be under tempfile.gettempdir() for auto-cleanup; got {resolved}"
        )
        assert "data/kill_switches" not in str(resolved)
        assert "data/risk_state_blend" not in str(resolved)
    finally:
        import shutil

        shutil.rmtree(resolved, ignore_errors=True)


def test_explicit_state_dir_respected(tmp_path):
    """``--state-dir PATH`` overrides the default tempdir."""
    explicit = tmp_path / "my_harness_state"
    args = argparse.Namespace(state_dir=str(explicit), allow_foreign_ownership=False, keep_state_dir=False)
    resolved = harness._resolve_state_dir(args)
    assert resolved == explicit.resolve()
    assert resolved.exists()


# ── AC2: startup guard refuses when PID file present and live ──────────────


def test_guard_refuses_when_pid_file_present(monkeypatch, tmp_path):
    """Live forward_test.pid with live PID → harness refuses."""
    _make_fake_live_files(monkeypatch, tmp_path)
    pid_file = harness.LIVE_FORWARD_TEST_PID
    pid_file.write_text(str(os.getpid()))

    args = argparse.Namespace(allow_foreign_ownership=False, keep_state_dir=False)
    state_dir = tmp_path / "harness_state"
    state_dir.mkdir()
    with pytest.raises(harness.RefuseToRun) as excinfo:
        harness._verify_isolation_or_refuse(args, state_dir)
    assert "LIVE forward_test engine is running" in str(excinfo.value)
    assert str(os.getpid()) in str(excinfo.value)


def test_guard_allows_stale_pid_file(monkeypatch, tmp_path, caplog):
    """Stale PID file (PID no longer alive) is logged and ignored."""
    import logging

    _make_fake_live_files(monkeypatch, tmp_path)
    harness.LIVE_FORWARD_TEST_PID.write_text("99999999")

    args = argparse.Namespace(allow_foreign_ownership=False, keep_state_dir=False)
    state_dir = tmp_path / "harness_state"
    state_dir.mkdir()
    with caplog.at_level(logging.WARNING, logger="ayumi.backtest_harness"):
        # Must not raise
        harness._verify_isolation_or_refuse(args, state_dir)
    assert any("Stale PID file" in rec.message for rec in caplog.records), (
        f"Expected a 'Stale PID file' warning; got: {[r.message for r in caplog.records]}"
    )


# ── AC3: startup guard refuses on foreign-ownership ─────────────────────────


def test_guard_refuses_on_foreign_ownership(monkeypatch, tmp_path):
    """Live state file owned by a different uid → harness refuses."""
    _make_fake_live_files(monkeypatch, tmp_path)
    real_getuid = os.getuid

    def fake_getuid():
        return 0 if real_getuid() != 0 else 1

    monkeypatch.setattr(os, "getuid", fake_getuid)
    # And ensure pwd.getpwuid works for our fake uid 0
    import pwd

    def fake_getpwuid(uid):
        if uid == 0:
            return pwd.struct_passwd(("root", "", 0, 0, "", "/root", "/bin/bash"))
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", fake_getpwuid)

    args = argparse.Namespace(allow_foreign_ownership=False, keep_state_dir=False)
    state_dir = tmp_path / "harness_state"
    state_dir.mkdir()
    with pytest.raises(harness.RefuseToRun) as excinfo:
        harness._verify_isolation_or_refuse(args, state_dir)
    assert "LIVE state file" in str(excinfo.value)
    assert "758273a7" in str(excinfo.value)


def test_guard_allows_with_allow_foreign_ownership(monkeypatch, tmp_path):
    """``--allow-foreign-ownership`` bypasses the ownership check."""
    _make_fake_live_files(monkeypatch, tmp_path)
    real_getuid = os.getuid

    def fake_getuid():
        return 0 if real_getuid() != 0 else 1

    monkeypatch.setattr(os, "getuid", fake_getuid)

    args = argparse.Namespace(allow_foreign_ownership=True, keep_state_dir=False)
    state_dir = tmp_path / "harness_state"
    state_dir.mkdir()
    # Must not raise
    harness._verify_isolation_or_refuse(args, state_dir)


def test_guard_passes_clean_state(monkeypatch, tmp_path):
    """No PID file, no live state files → guard passes silently."""
    live_root = tmp_path / "fake_live"
    live_root.mkdir()
    (live_root / "data").mkdir()
    monkeypatch.setattr(harness, "LIVE_FORWARD_TEST_PID", live_root / "data" / "forward_test.pid")
    monkeypatch.setattr(
        harness,
        "LIVE_KILL_SWITCH_GLOBAL_STATE",
        live_root / "data" / "kill_switches" / "global.state",
    )
    monkeypatch.setattr(
        harness,
        "LIVE_RISK_STATE_BLEND",
        live_root / "data" / "risk_state_blend.json",
    )

    args = argparse.Namespace(allow_foreign_ownership=False, keep_state_dir=False)
    state_dir = tmp_path / "harness_state"
    state_dir.mkdir()
    # Must not raise
    harness._verify_isolation_or_refuse(args, state_dir)


# ── AC4: isolated KillSwitchManager writes only to its own state_dir ───────


def test_isolated_killswitch_writes_only_to_isolated_dir(tmp_path):
    """Activating kill on an isolated KillSwitchManager writes ONLY to its
    state_dir — never to ``LIVE_KILL_SWITCH_GLOBAL_STATE`` (which the test
    points at a sentinel under tmp_path).
    """
    from adapters.ctrader.kill_switch import KillSwitchManager

    # Sentinel "LIVE" state file
    sentinel_live = tmp_path / "live_kill_switches" / "global.state"
    sentinel_live.parent.mkdir(parents=True)
    sentinel_content_before = json.dumps({"active": False, "version": 1}) + "\n"
    sentinel_live.write_text(sentinel_content_before)
    sentinel_mtime_before = sentinel_live.stat().st_mtime

    # Isolated KillSwitchManager bound to a different state_dir
    isolated_dir = tmp_path / "isolated_state" / "kill_switches"
    isolated_ks = KillSwitchManager(state_dir=str(isolated_dir), disabled=False)
    isolated_ks.activate_global_kill(reason="test", triggered_by="unit_test")

    # 1. Isolated dir got the kill state file
    isolated_state_file = isolated_dir / "global.state"
    assert isolated_state_file.exists(), (
        f"Isolated KillSwitchManager must write its state file to {isolated_state_file}"
    )
    state = json.loads(isolated_state_file.read_text())
    assert state["active"] is True
    assert state["reason"] == "test"

    # 2. Sentinel LIVE state file was NOT modified
    assert sentinel_live.read_text() == sentinel_content_before, (
        "Sentinel LIVE kill-switch state must be unchanged after isolated KS activation"
    )
    assert sentinel_live.stat().st_mtime == sentinel_mtime_before, (
        "Sentinel LIVE kill-switch state mtime must be unchanged after isolated KS activation"
    )


# ── AC5: source-level invariants ───────────────────────────────────────────


def test_harness_exposes_state_dir_arg():
    """``--state-dir`` must be a documented CLI option (card 758273a7)."""
    src = _harness_source_path().read_text(encoding="utf-8")
    assert '"--state-dir"' in src, "harness must expose --state-dir CLI arg"


def test_harness_exposes_allow_foreign_ownership_arg():
    """``--allow-foreign-ownership`` must be a documented CLI option (card 758273a7)."""
    src = _harness_source_path().read_text(encoding="utf-8")
    assert '"--allow-foreign-ownership"' in src


def test_harness_exposes_keep_state_dir_arg():
    """``--keep-state-dir`` must be a documented CLI option (card 758273a7)."""
    src = _harness_source_path().read_text(encoding="utf-8")
    assert '"--keep-state-dir"' in src


def test_harness_does_not_unlink_live_risk_state():
    """The harness must NOT contain an unconditional unlink() of the live
    risk state path. Card 758273a7: pre-fix harness line 313-316 poisoned
    ``data/risk_state_blend.json`` on every run.

    The LIVE path IS referenced (as the ``LIVE_RISK_STATE_BLEND`` constant
    that the ownership guard checks); what we forbid is the combination
    of that live path with ``.unlink()`` (the poisoning action).
    """
    src = _harness_source_path().read_text(encoding="utf-8")
    # Look for `...risk_state_blend.json").unlink()` (the poisoning pattern)
    forbidden_pattern = re.compile(
        r"risk_state_blend\.json[^)]*\)\s*\.\s*unlink\s*\(",
        re.MULTILINE,
    )
    # The LIVE constant declaration (LIVE_RISK_STATE_BLEND = ...) is fine
    # and must NOT match — only the live risk state being unlinked is bad.
    matches = list(forbidden_pattern.finditer(src))
    assert not matches, (
        f"Harness must NOT unlink the live risk_state_blend.json; "
        f"delete from the isolated state_dir instead. "
        f"Found pattern at offsets: {[m.start() for m in matches]}"
    )


def test_harness_rewires_engine_kill_switch_before_build_components():
    """The harness must replace ``engine._kill_switch`` with the isolated
    instance BEFORE ``engine._build_components()`` so every consumer sees
    the isolated state_dir. Card 758273a7.
    """
    src = _harness_source_path().read_text(encoding="utf-8")
    # Find a code-level (indented) call, not a comment/docstring occurrence.
    code_call_re = re.compile(r"^\s+engine\._build_components\(\)", re.MULTILINE)
    code_assign_re = re.compile(r"^\s+engine\._kill_switch = isolated_ks", re.MULTILINE)
    build_match = code_call_re.search(src)
    assign_match = code_assign_re.search(src)
    assert build_match, "Harness must call engine._build_components()"
    assert assign_match, "Harness must assign engine._kill_switch = isolated_ks (indented code)"
    assert assign_match.start() < build_match.start(), (
        f"Harness must assign engine._kill_switch = isolated_ks BEFORE "
        f"engine._build_components() — every consumer (PositionMonitor, "
        f"PaperTrader._risk_guard, ExecutionPermissionPolicy, market_feed) "
        f"is wired via self._kill_switch in _build_components(). "
        f"got assign@{assign_match.start()} build@{build_match.start()}"
    )
    iso_decl = src.find("isolated_ks = _build_isolated_kill_switch(")
    assert iso_decl > 0, "Harness must construct isolated_ks via _build_isolated_kill_switch(state_dir)"


def test_harness_calls_verify_isolation_at_run_backtest_top():
    """``run_backtest`` must call ``_verify_isolation_or_refuse`` near its top.
    Card 758273a7: guard must run before any state-writing work.
    """
    src = _harness_source_path().read_text(encoding="utf-8")
    fn_start = src.index("def run_backtest(")
    fn_end = src.index("\ndef ", fn_start + 1)
    fn_body = src[fn_start:fn_end]
    guard_call_pos = fn_body.index("_verify_isolation_or_refuse(args, state_dir)")
    assert guard_call_pos < 4000, "Guard must run at the top of run_backtest, before any state-writing work"


def test_harness_uses_isolated_risk_state_path_in_run_backtest():
    """``run_backtest`` must delete/use the isolated risk state path, not LIVE.
    Card 758273a7.
    """
    src = _harness_source_path().read_text(encoding="utf-8")
    fn_start = src.index("def run_backtest(")
    fn_end = src.index("\ndef ", fn_start + 1)
    fn_body = src[fn_start:fn_end]
    assert 'state_path = state_dir / "risk_state_blend.json"' in fn_body, (
        "Harness must derive risk_state path from state_dir (isolated), "
        "not from PROJECT_ROOT / 'data' / 'risk_state_blend.json'"
    )


def test_harness_redirects_blend_runner_persistence_path():
    """The harness must redirect ``blend_runner._persistence._path`` to the
    isolated state dir. Card 758273a7: ``build_blend_runner()`` (defined in
    launch_blend_forward_test.py:1285+) defaults to ``data/risk_state_blend.json``,
    and the runner's ``stop()`` writes there — which would be the LIVE path
    unless redirected.
    """
    src = _harness_source_path().read_text(encoding="utf-8")
    fn_start = src.index("def run_backtest(")
    fn_end = src.index("\ndef ", fn_start + 1)
    fn_body = src[fn_start:fn_end]
    assert "blend_runner._persistence._path =" in fn_body, (
        "Harness must redirect blend_runner._persistence._path to the "
        "isolated state dir; otherwise blend_runner.stop() writes to the "
        "LIVE data/risk_state_blend.json (card 758273a7 second poisoning "
        "channel)."
    )
    # Must come AFTER blend_runner = build_blend_runner()
    build_pos = fn_body.index("blend_runner = build_blend_runner()")
    redirect_pos = fn_body.index("blend_runner._persistence._path =")
    assert build_pos < redirect_pos, (
        "Persistence redirect must come AFTER blend_runner construction"
    )


def test_harness_patches_engine_kill_switch_manager_reference():
    """The harness must patch ``forward_test_engine.KillSwitchManager``
    before constructing the engine so the engine's __init__-time
    ``self._kill_switch = KillSwitchManager()`` (line 535) uses the
    isolated state dir, not ``data/kill_switches/``.

    Card 758273a7 second poisoning channel: the engine's __init__ creates
    a default KillSwitchManager pointing at the LIVE path BEFORE the
    harness can replace ``engine._kill_switch``. That default instance's
    ``_load_state()`` fails on a corrupt sentinel and triggers the
    fail-safe ``_save_state()`` (active=True, reason='corrupt_state_file'),
    corrupting the live ``data/kill_switches/global.state``. The harness
    must swap the engine's class reference so the __init__-time instance
    is itself isolated.
    """
    src = _harness_source_path().read_text(encoding="utf-8")
    # Check that the harness patches the kill_switch module's
    # KillSwitchManager reference (broader patch than just forward_test_engine).
    assert "_ks_module.KillSwitchManager =" in src, (
        "Harness must swap adapters.ctrader.kill_switch.KillSwitchManager "
        "to a subclass that defaults state_dir to the isolated dir (card "
        "758273a7 — every KillSwitchManager() call in the engine pipeline "
        "must target the isolated dir)."
    )
    # And restores the kill_switch module's reference
    assert "_ks_module.KillSwitchManager = _saved_ks_cls" in src, (
        "Harness must restore the kill_switch module's original "
        "KillSwitchManager reference after engine construction "
        "(card 758273a7)"
    )
    # And also patches the forward_test_engine / paper_trader references
    assert "_fte_module.KillSwitchManager =" in src, (
        "Harness must also patch forward_test_engine.KillSwitchManager "
        "(defensive: catches pre-existing bindings from other importers)"
    )


# ── AC6: keep-state-dir flag preserves dir for post-mortem ────────────────


def test_keep_state_dir_preserves_directory(tmp_path):
    """``--keep-state-dir`` must prevent auto-cleanup of the isolated dir."""
    state_dir = tmp_path / "harness_state"
    state_dir.mkdir()
    (state_dir / "kill_switches").mkdir()

    harness._cleanup_state_dir(state_dir, keep=True)
    assert state_dir.exists(), "--keep-state-dir must preserve the state dir"


# ── AC7 (Rin rework 4f384bfc MEDIUM): failure-path tests ────────────────


def test_isolation_patch_restores_module_bindings_on_exception(tmp_path, monkeypatch):
    """``_isolation_patch`` must restore ALL module-level KillSwitchManager
    bindings on the exception path. Card 758273a7 REWORK (Rin verdict
    4f384bfc HIGH #1) — pre-rework restoration was success-only, leaking
    _HarnessIsolatedKS into subsequent in-process constructions.
    """
    import adapters.ctrader.forward_test_engine as fte_mod
    import adapters.ctrader.kill_switch as ks_mod
    from adapters.ctrader import paper_trader as pt_mod

    # Capture originals for the assertion at the end.
    orig_ks = ks_mod.KillSwitchManager
    orig_fte = fte_mod.KillSwitchManager
    orig_pt = getattr(pt_mod, "KillSwitchManager", None)

    # Build an isolated_ks to feed into the context manager.
    isolated_ks_dir = tmp_path / "isolated_ks"
    isolated_ks_dir.mkdir()
    isolated_ks = harness._build_isolated_kill_switch(isolated_ks_dir)

    # Inject an exception inside the with-block.
    sentinel = RuntimeError("simulated mid-run failure")
    with pytest.raises(RuntimeError) as excinfo:
        with harness._isolation_patch(isolated_ks):
            # Verify patches are applied inside the with-block.
            assert ks_mod.KillSwitchManager is not orig_ks, (
                "ks_module.KillSwitchManager must be patched inside the with-block"
            )
            assert fte_mod.KillSwitchManager is not orig_fte
            raise sentinel
    assert excinfo.value is sentinel

    # After the with-block exits (via exception), originals MUST be restored.
    assert ks_mod.KillSwitchManager is orig_ks, (
        "ks_module.KillSwitchManager must be restored after exception"
    )
    assert fte_mod.KillSwitchManager is orig_fte, (
        "fte_module.KillSwitchManager must be restored after exception"
    )
    if orig_pt is not None:
        assert getattr(pt_mod, "KillSwitchManager", None) is orig_pt, (
            "pt_module.KillSwitchManager must be restored after exception"
        )


def test_isolation_patch_restores_disabled_flag_on_exception(tmp_path):
    """``_isolation_patch`` must restore ``KillSwitchManager._disabled``
    on the exception path. Card 758273a7 REWORK (Rin verdict 4f384bfc
    HIGH #2) — pre-rework flipped ``_disabled = False`` and never
    restored, leaking enabled state into subsequent in-process runs.
    """
    from adapters.ctrader.kill_switch import KillSwitchManager

    # Capture the original class-level value.
    orig_disabled = KillSwitchManager._disabled

    # Pre-condition: verify we are starting from a known state. If the
    # harness left _disabled=False from a previous failed test, this
    # assertion catches it.
    isolated_ks_dir = tmp_path / "isolated_ks"
    isolated_ks_dir.mkdir()
    isolated_ks = harness._build_isolated_kill_switch(isolated_ks_dir)

    with pytest.raises(RuntimeError, match="simulated"):
        with harness._isolation_patch(isolated_ks):
            # Inside the with-block, _disabled must be False (patched).
            assert KillSwitchManager._disabled is False, (
                "_disabled must be False inside the with-block (harness-enabled)"
            )
            raise RuntimeError("simulated")

    # After the with-block, _disabled must be restored to the captured value.
    assert KillSwitchManager._disabled is orig_disabled, (
        f"_disabled must be restored to original ({orig_disabled}) after "
        f"exception; got {KillSwitchManager._disabled}"
    )


def test_isolation_patch_restores_disabled_flag_true_after_mid_run_exception(
    tmp_path, monkeypatch
):
    """Rin verdict 4f384bfc REWORK-2, 2026-09-08: assert the production
    default ``_disabled = True`` (per Craig directive Jun 27) is the value
    restored on the exception path — not the harness-flipped ``False``.

    Pre-REWORK-2 bug: ``run_backtest`` flipped ``KillSwitchManager._disabled
    = False`` BEFORE entering ``_isolation_patch``, so the context manager
    captured the already-flipped False and its finally restored False
    instead of the production default True. The harness leaked the
    enabled state into subsequent in-process runs.

    The pre-existing ``test_isolation_patch_restores_disabled_flag_on_exception``
    did NOT catch this bug because it captured the current value of
    ``_disabled`` first — if a prior test had left it at False, the test
    captured False as "original" and verified False was restored.
    This test forces ``_disabled = True`` (production default) as the
    precondition so the bug is observable.
    """
    from adapters.ctrader.kill_switch import KillSwitchManager

    # Force production default as precondition.
    monkeypatch.setattr(KillSwitchManager, "_disabled", True)

    isolated_ks_dir = tmp_path / "isolated_ks"
    isolated_ks_dir.mkdir()
    isolated_ks = harness._build_isolated_kill_switch(isolated_ks_dir)

    # Sanity: pre-condition holds.
    assert KillSwitchManager._disabled is True, (
        "precondition: KillSwitchManager._disabled must be True (production default)"
    )

    # Simulate a mid-run exception. Use a try/except so the test itself
    # doesn't fail — we just need to verify post-exception restoration.
    try:
        with harness._isolation_patch(isolated_ks):
            assert KillSwitchManager._disabled is False, (
                "_disabled must be False inside the with-block (harness-enabled)"
            )
            raise RuntimeError("simulated mid-run failure")
    except RuntimeError:
        pass

    # Post-condition: must be back to production default (True), not
    # the harness-flipped False. This is the exact bug Rin flagged.
    assert KillSwitchManager._disabled is True, (
        f"_disabled must be restored to production default True (Craig directive "
        f"Jun 27) after a mid-run exception; got {KillSwitchManager._disabled}. "
        f"REGRESSION: _isolation_patch captured an already-flipped value instead "
        f"of the production default (Rin verdict 4f384bfc REWORK-2 HIGH #2)."
    )


def test_isolation_patch_restores_on_normal_return(tmp_path):
    """``_isolation_patch`` must restore on the normal-return path too
    (Rin HIGH #1 + #2 — both paths must restore). Two sequential in-process
    runs must not leak state into each other.
    """
    import adapters.ctrader.kill_switch as ks_mod
    from adapters.ctrader.kill_switch import KillSwitchManager

    orig_ks = ks_mod.KillSwitchManager
    orig_disabled = KillSwitchManager._disabled

    isolated_ks_dir = tmp_path / "isolated_ks"
    isolated_ks_dir.mkdir()
    isolated_ks = harness._build_isolated_kill_switch(isolated_ks_dir)

    # First run — normal completion.
    with harness._isolation_patch(isolated_ks):
        assert KillSwitchManager._disabled is False
        assert ks_mod.KillSwitchManager is not orig_ks

    # After first run: originals restored.
    assert KillSwitchManager._disabled is orig_disabled
    assert ks_mod.KillSwitchManager is orig_ks

    # Second run — must also restore cleanly (proving no leakage).
    with harness._isolation_patch(isolated_ks):
        assert KillSwitchManager._disabled is False
        assert ks_mod.KillSwitchManager is not orig_ks

    assert KillSwitchManager._disabled is orig_disabled, (
        "_disabled must be restored after second in-process run too"
    )
    assert ks_mod.KillSwitchManager is orig_ks


def test_guard_refuses_on_unreadable_pid_file_via_permission_error(
    monkeypatch, tmp_path, caplog
):
    """PID file that raises PermissionError on read_text() must REFUSE,
    not proceed. Card 758273a7 REWORK (Rin verdict 4f384bfc HIGH #3).
    Pre-rework guard treated PermissionError as STALE (fail-open), which
    let an unreadable / foreign-owned PID file slip past the guard.
    """
    import logging

    _make_fake_live_files(monkeypatch, tmp_path)
    pid_file = harness.LIVE_FORWARD_TEST_PID
    pid_file.write_text("99999")  # content exists, but read will be denied

    # Monkeypatch read_text to raise PermissionError (simulates a file
    # locked or owned by a foreign user).
    def fake_read_text(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", str(pid_file))

    monkeypatch.setattr(type(pid_file), "read_text", fake_read_text)

    args = argparse.Namespace(allow_foreign_ownership=False, keep_state_dir=False)
    state_dir = tmp_path / "harness_state"
    state_dir.mkdir()

    with caplog.at_level(logging.WARNING, logger="ayumi.backtest_harness"):
        with pytest.raises(harness.RefuseToRun) as excinfo:
            harness._verify_isolation_or_refuse(args, state_dir)

    # The error message must mention fail-closed / unreadable.
    err = str(excinfo.value)
    assert "unreadable" in err.lower(), (
        f"RefuseToRun message must explain unreadable PID file; got: {err}"
    )
    assert "failing closed" in err.lower() or "FAIL CLOSED" in err or "refuses" in err.lower(), (
        f"RefuseToRun message must indicate fail-closed semantics; got: {err}"
    )
    assert "4f384bfc" in err, (
        f"RefuseToRun message must cite Rin verdict 4f384bfc; got: {err}"
    )


# ── AC8 (card e1e32b07) — guard LIVE paths anchored to MAIN worktree root ──
#
# Card e1e32b07 (2026-09-09): when the harness is loaded from a non-primary
# git worktree, the pre-fix guard's LIVE_* paths resolved against
# ``PROJECT_ROOT`` (the per-worktree root), so the guard's
# ``data/forward_test.pid`` probe was scoped to the WORKTREE's data/
# directory — which typically does NOT contain the live PID file. The live
# engine running in the MAIN tree consequently slipped past the
# refuse-when-live check by construction (the bug pattern was that the
# guard was effectively a no-op from any worktree).
#
# The fix re-resolves the guard's LIVE_* paths against the MAIN worktree's
# root via ``_main_worktree_root()`` (``git worktree list --porcelain``,
# first entry). These tests verify the fix WITHOUT touching the real repo
# or the live engine: they monkeypatch the main-root resolver to a tmp
# skeleton so the harness's LIVE paths resolve against an isolated
# fake main tree.


# ── AC8.1: regression — guard refuses when MAIN root has a live PID ───────


def test_guard_uses_main_root_for_live_pid_check_when_running_from_worktree(
    monkeypatch, tmp_path, caplog
):
    """Regression for card e1e32b07: when the harness is running from a
    non-primary worktree and the MAIN tree's data/forward_test.pid points
    at a live PID, the guard MUST refuse.

    Pre-fix bug: ``LIVE_FORWARD_TEST_PID`` resolved against the worktree's
    PROJECT_ROOT, so the guard never saw the MAIN tree's PID file and the
    harness silently proceeded (the refuse-when-live gate was bypassed by
    construction).

    Test setup: monkeypatch ``_main_worktree_root`` to return a tmp skeleton
    containing ``data/forward_test.pid`` containing ``os.getpid()`` (which
    is alive for the test process). Also monkeypatch the existing LIVE_*
    constants to point at the SAME tmp skeleton so the ownership check
    has the matching LIVE_KILL_SWITCH_GLOBAL_STATE / LIVE_RISK_STATE_BLEND
    paths. Force ``_GUARD_LIVE_ROOT`` to match.
    """
    import logging

    fake_main = tmp_path / "fake_main"
    fake_main.mkdir()
    live_data = fake_main / "data"
    live_data.mkdir()
    pid_file = live_data / "forward_test.pid"
    pid_file.write_text(str(os.getpid()))  # live PID (this test process)

    # Sentinel kill switch + risk state owned by us so the ownership check
    # does not also fire (we are testing the PID refusal path specifically).
    ks_dir = live_data / "kill_switches"
    ks_dir.mkdir()
    live_ks = ks_dir / "global.state"
    live_ks.write_text(json.dumps({"active": False, "version": 1}) + "\n")
    live_risk = live_data / "risk_state_blend.json"
    live_risk.write_text(json.dumps({"starting_balance": 10000.0}) + "\n")

    # Point the guard's LIVE_* constants at the fake main skeleton.
    monkeypatch.setattr(harness, "_GUARD_LIVE_ROOT", fake_main)
    monkeypatch.setattr(harness, "LIVE_FORWARD_TEST_PID", pid_file)
    monkeypatch.setattr(harness, "LIVE_KILL_SWITCH_GLOBAL_STATE", live_ks)
    monkeypatch.setattr(harness, "LIVE_RISK_STATE_BLEND", live_risk)

    # Make _main_worktree_root() also return the fake main so any code path
    # that re-resolves the root mid-flight stays consistent. (Defensive —
    # the LIVE_* constants above are the ones _verify_isolation_or_refuse
    # actually reads.)
    monkeypatch.setattr(harness, "_main_worktree_root", lambda: fake_main)

    args = argparse.Namespace(allow_foreign_ownership=False, keep_state_dir=False)
    state_dir = tmp_path / "harness_state"
    state_dir.mkdir()

    with caplog.at_level(logging.INFO, logger="ayumi.backtest_harness"):
        with pytest.raises(harness.RefuseToRun) as excinfo:
            harness._verify_isolation_or_refuse(args, state_dir)

    err = str(excinfo.value)
    assert "LIVE forward_test engine is running" in err, (
        f"RefuseToRun message must indicate live engine detected; got: {err}"
    )
    assert str(os.getpid()) in err, (
        f"RefuseToRun message must include the live PID ({os.getpid()}); got: {err}"
    )
    # The refused path must be the fake main tree's PID file, NOT a path
    # under the current worktree. This is the structural assertion that
    # proves the guard is reading MAIN-root LIVE paths.
    assert str(pid_file) in err, (
        f"RefuseToRun message must reference the MAIN-root LIVE_FORWARD_TEST_PID "
        f"({pid_file}); got: {err}. "
        f"REGRESSION: guard is still resolving LIVE paths against PROJECT_ROOT "
        f"instead of the main worktree root (card e1e32b07)."
    )
    assert "data/forward_test.pid" in err


def test_guard_main_root_resolver_runs_git_worktree_list(monkeypatch, tmp_path):
    """``_main_worktree_root`` must run ``git worktree list --porcelain``
    and return the FIRST ``worktree`` entry as a ``Path``.

    We intercept ``subprocess.run`` to verify the call args without
    actually invoking git (which depends on the host environment).
    """
    import scripts.backtest_blend_harness as harness_mod

    # Use a real directory so _main_worktree_root's `Path.is_dir()` check
    # passes (it must return None for non-existent paths — see
    # test_guard_main_root_resolver_nonexistent_path_returns_none).
    fake_main = tmp_path / "fake_main"
    fake_main.mkdir()
    fake_feature = tmp_path / "fake_feature"
    fake_feature.mkdir()

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = (
            f"worktree {fake_main}\n"
            "HEAD abcdef0123456789\n"
            "branch refs/heads/main\n"
            "\n"
            f"worktree {fake_feature}\n"
            "HEAD 1234567890abcdef\n"
            "branch refs/heads/feature/x\n"
        )
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted()

    monkeypatch.setattr(harness_mod.subprocess, "run", fake_run)

    result = harness_mod._main_worktree_root()
    assert result == fake_main, (
        f"Expected FIRST worktree entry {fake_main} (the primary); got {result}"
    )
    # Verify the cmd, timeout, cwd, and capture-output contract.
    assert captured["cmd"] == ["git", "worktree", "list", "--porcelain"], (
        f"_main_worktree_root must invoke 'git worktree list --porcelain'; got {captured['cmd']}"
    )
    assert captured["kwargs"].get("timeout") is not None, (
        "_main_worktree_root must pass a timeout to subprocess.run (HR5 bounded exec)"
    )
    assert captured["kwargs"].get("cwd"), (
        "_main_worktree_root must set cwd (run from PROJECT_ROOT)"
    )


def test_guard_main_root_resolver_timeout_returns_none(monkeypatch):
    """``_main_worktree_root`` must return ``None`` (not raise) when git
    times out, so the caller can fall back to ``PROJECT_ROOT``.
    """
    import subprocess as sp

    import scripts.backtest_blend_harness as harness_mod

    def fake_run(*_args, **_kwargs):
        raise sp.TimeoutExpired(cmd=["git"], timeout=5)

    monkeypatch.setattr(harness_mod.subprocess, "run", fake_run)

    assert harness_mod._main_worktree_root() is None, (
        "_main_worktree_root must return None on TimeoutExpired, not raise"
    )


def test_guard_main_root_resolver_nonzero_exit_returns_none(monkeypatch):
    """``_main_worktree_root`` must return ``None`` when git exits non-zero
    (e.g. not in a git repo, corrupt .git, etc.)."""
    import scripts.backtest_blend_harness as harness_mod

    class _FakeCompleted:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository"

    monkeypatch.setattr(
        harness_mod.subprocess, "run", lambda *a, **kw: _FakeCompleted()
    )
    assert harness_mod._main_worktree_root() is None


def test_guard_main_root_resolver_missing_first_entry_returns_none(monkeypatch):
    """``_main_worktree_root`` must return ``None`` when ``git worktree
    list --porcelain`` returns no ``worktree`` line at all (corrupt output,
    exotic git version)."""
    import scripts.backtest_blend_harness as harness_mod

    class _FakeCompleted:
        returncode = 0
        stdout = "HEAD abcdef0123456789\nbranch refs/heads/main\n"
        stderr = ""

    monkeypatch.setattr(
        harness_mod.subprocess, "run", lambda *a, **kw: _FakeCompleted()
    )
    assert harness_mod._main_worktree_root() is None


def test_guard_main_root_resolver_nonexistent_path_returns_none(monkeypatch, tmp_path):
    """``_main_worktree_root`` must return ``None`` when the first parsed
    path does not exist on disk (e.g. stale worktree after ``git worktree
    remove``)."""
    import scripts.backtest_blend_harness as harness_mod

    ghost = "/nonexistent/path/that/does/not/exist"

    class _FakeCompleted:
        returncode = 0
        stdout = f"worktree {ghost}\nHEAD abcdef\nbranch refs/heads/main\n"
        stderr = ""

    monkeypatch.setattr(
        harness_mod.subprocess, "run", lambda *a, **kw: _FakeCompleted()
    )
    assert harness_mod._main_worktree_root() is None, (
        "_main_worktree_root must return None when the parsed path is not a "
        "directory; otherwise we'd resolve LIVE paths to a stale/removed "
        "worktree and miss the actual main tree's data/."
    )


def test_guard_resolve_guard_live_root_falls_back_to_project_root_on_failure(
    monkeypatch,
):
    """``_resolve_guard_live_root`` must return ``PROJECT_ROOT`` (current
    tree) when ``_main_worktree_root`` returns ``None``. This preserves
    pre-fix behavior on every failure path — fail-closed guard semantics
    are still the safety net."""
    import scripts.backtest_blend_harness as harness_mod

    monkeypatch.setattr(harness_mod, "_main_worktree_root", lambda: None)
    # Defensive: ensure no env-bypass is set for this test.
    monkeypatch.delenv(harness_mod._GUARD_LIVE_ENV_BYPASS, raising=False)

    assert harness_mod._resolve_guard_live_root() == harness_mod.PROJECT_ROOT, (
        "_resolve_guard_live_root must fall back to PROJECT_ROOT when "
        "_main_worktree_root returns None — preserving pre-fix behavior on "
        "git failures."
    )


def test_guard_resolve_guard_live_root_env_bypass_logs_loudly(
    monkeypatch, caplog
):
    """When ``AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT=1`` is set,
    ``_resolve_guard_live_root`` must:
      (1) return ``PROJECT_ROOT`` (NOT the main root), AND
      (2) emit a WARNING-level log so any unexpected use surfaces loudly.

    Card e1e32b07: the bypass exists for operator debugging on the main
    tree itself (where PROJECT_ROOT IS the main root) and for tests that
    drive the guard via monkeypatched LIVE_* constants. It must NOT be
    silent — an unexpected use in production must show up in operator
    logs.
    """
    import logging

    import scripts.backtest_blend_harness as harness_mod

    fake_main = Path("/some/main/root/that/should/not/be/used")
    monkeypatch.setattr(harness_mod, "_main_worktree_root", lambda: fake_main)
    monkeypatch.setenv(harness_mod._GUARD_LIVE_ENV_BYPASS, "1")

    with caplog.at_level(logging.WARNING, logger="ayumi.backtest_harness"):
        result = harness_mod._resolve_guard_live_root()

    assert result == harness_mod.PROJECT_ROOT, (
        f"Bypass env var must force _resolve_guard_live_root to PROJECT_ROOT; "
        f"got {result}"
    )
    bypass_warnings = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING and "AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT" in rec.message
    ]
    assert bypass_warnings, (
        f"Bypass env var must emit a WARNING log so unexpected use surfaces "
        f"loudly. Got caplog records: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )


def test_guard_resolve_guard_live_root_uses_main_root_by_default(monkeypatch):
    """Default (no env-bypass, git succeeds): ``_resolve_guard_live_root``
    returns the main root (not PROJECT_ROOT). This is the structural fix
    for card e1e32b07."""
    import scripts.backtest_blend_harness as harness_mod

    fake_main = Path("/home/test/fake_main_root")
    monkeypatch.setattr(harness_mod, "_main_worktree_root", lambda: fake_main)
    monkeypatch.delenv(harness_mod._GUARD_LIVE_ENV_BYPASS, raising=False)

    result = harness_mod._resolve_guard_live_root()
    assert result == fake_main, (
        f"_resolve_guard_live_root must return the main root by default; "
        f"got {result}. Pre-fix bug was that LIVE paths were anchored to "
        f"PROJECT_ROOT (worktree-local), bypassing the refuse-when-live gate."
    )


# ── AC8.2: source-level — guard does NOT silence the structural fix ────────


def test_harness_source_uses_git_worktree_list_for_main_root():
    """Source-level guard: ``_main_worktree_root`` must invoke ``git
    worktree list --porcelain`` so the structural fix for card e1e32b07
    is observable in the source. Detects accidental regressions where the
    function is simplified away or the subprocess invocation is removed.
    """
    src = _harness_source_path().read_text(encoding="utf-8")
    assert '"git"' in src and '"worktree"' in src and '"list"' in src and '"--porcelain"' in src, (
        "Harness source must contain the git worktree list --porcelain "
        "invocation — the structural fix for card e1e32b07 depends on it. "
        "If this fails, the main-root resolver has been removed and the "
        "guard's refuse-when-live check is again bypassed from worktrees."
    )
    assert "_main_worktree_root" in src, (
        "Harness source must define _main_worktree_root()"
    )
    assert "_resolve_guard_live_root" in src, (
        "Harness source must define _resolve_guard_live_root() that wraps "
        "_main_worktree_root() with fallback + env-bypass"
    )
    # LIVE_* constants must be anchored to _GUARD_LIVE_ROOT, not PROJECT_ROOT.
    assert "LIVE_FORWARD_TEST_PID = _GUARD_LIVE_ROOT" in src, (
        "LIVE_FORWARD_TEST_PID must be anchored to _GUARD_LIVE_ROOT, not "
        "PROJECT_ROOT — card e1e32b07"
    )
    assert "LIVE_KILL_SWITCH_GLOBAL_STATE = _GUARD_LIVE_ROOT" in src, (
        "LIVE_KILL_SWITCH_GLOBAL_STATE must be anchored to _GUARD_LIVE_ROOT"
    )
    assert "LIVE_RISK_STATE_BLEND = _GUARD_LIVE_ROOT" in src, (
        "LIVE_RISK_STATE_BLEND must be anchored to _GUARD_LIVE_ROOT"
    )
    # And the env-bypass must exist and log loudly.
    assert "AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT" in src, (
        "Harness must expose AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT env-bypass "
        "with loud WARNING logging for operator/main-tree debugging."
    )
    # PROJECT_ROOT itself must NOT have changed (preserved for non-guard uses
    # like duckdb path / launcher import).
    assert "PROJECT_ROOT = Path(__file__).resolve().parent.parent" in src, (
        "PROJECT_ROOT must remain anchored to the current worktree "
        "(unchanged) so non-guard uses (duckdb path, launcher import) "
        "stay on the current tree per card e1e32b07 spec."
    )


# ── AC8.3 (card e1e32b07 REWORK, Rin HIGH finding) ─────────────────────────
#
# Card e1e32b07 REWORK (2026-09-09): the pre-rework resolver scanned
# `git worktree list --porcelain` LINE BY LINE and returned the FIRST
# ``worktree <path>`` line regardless of any flags carried by that
# block. In a bare-repo layout the bare repo's path appears first and
# would be selected, anchoring LIVE paths to a directory with no
# ``data/`` subdirectory — silently bypassing the refuse-when-live
# guard. The pre-rework tests above did NOT cover this because the
# fake ``_FakeCompleted.stdout`` strings in the existing tests always
# started with a non-bare block.
#
# The fix: parse output as BLOCKS separated by blank lines; skip blocks
# that carry ``bare`` or ``detached`` flags; anchor only to the first
# non-bare, non-detached block. The tests below exercise the bare and
# detached paths.


def test_guard_main_root_resolver_skips_bare_first_block(monkeypatch, tmp_path):
    """Card e1e32b07 REWORK (Rin HIGH finding): when the FIRST block in
    ``git worktree list --porcelain`` output carries the ``bare`` flag,
    ``_main_worktree_root`` must SKIP it and return the path of the next
    non-bare, non-detached block.

    Pre-rework bug: the resolver scanned line-by-line and returned the
    FIRST ``worktree <path>`` line regardless of the ``bare`` flag. In
    a bare-repo layout, that path pointed at the bare repo (which has
    no ``data/``), so LIVE paths resolved to a directory with no
    ``forward_test.pid`` and the refuse-when-live guard silently
    skipped its check.

    Test setup: a fake ``git worktree list --porcelain`` output whose
    FIRST block is a bare repo (with the ``bare`` flag) and whose
    SECOND block is a normal (non-bare, non-detached) checkout. The
    resolver must skip the bare block and return the SECOND block's
    path.
    """
    import scripts.backtest_blend_harness as harness_mod

    bare_path = tmp_path / "bare_repo"
    bare_path.mkdir()
    main_path = tmp_path / "main_checkout"
    main_path.mkdir()

    class _FakeCompleted:
        returncode = 0
        # IMPORTANT: FIRST block is the bare repo; SECOND block is the
        # real primary checkout. Pre-rework would return bare_path.
        stdout = (
            f"worktree {bare_path}\n"
            "bare\n"
            "\n"
            f"worktree {main_path}\n"
            "HEAD abcdef0123456789\n"
            "branch refs/heads/main\n"
        )
        stderr = ""

    monkeypatch.setattr(
        harness_mod.subprocess, "run", lambda *a, **kw: _FakeCompleted()
    )

    result = harness_mod._main_worktree_root()
    assert result == main_path, (
        f"_main_worktree_root must skip the bare entry and return the "
        f"non-bare primary worktree ({main_path}); got {result}. "
        f"REGRESSION: pre-rework resolver scanned line-by-line and "
        f"would return {bare_path} (bare repo path with no data/), "
        f"anchoring LIVE paths away from the real checkout."
    )
    assert result != bare_path, (
        f"_main_worktree_root must NEVER return a bare repo path; got {result}. "
        f"REGRESSION (card e1e32b07 REWORK Rin HIGH): bare-repo entries must be "
        f"skipped so LIVE paths land on the real primary checkout."
    )


def test_guard_main_root_resolver_returns_none_when_only_bare(monkeypatch, tmp_path):
    """Card e1e32b07 REWORK (Rin HIGH finding): when ``git worktree list
    --porcelain`` returns ONLY bare entries (e.g. a bare-only repo with
    no checkouts), ``_main_worktree_root`` must return ``None`` so the
    caller falls back to ``PROJECT_ROOT``.

    Pre-rework bug: the resolver would return the bare repo's path,
    anchoring LIVE paths to a directory with no ``data/`` and
    silently bypassing the refuse-when-live guard.
    """
    import scripts.backtest_blend_harness as harness_mod

    bare_path = tmp_path / "only_bare"
    bare_path.mkdir()

    class _FakeCompleted:
        returncode = 0
        stdout = f"worktree {bare_path}\nbare\n"
        stderr = ""

    monkeypatch.setattr(
        harness_mod.subprocess, "run", lambda *a, **kw: _FakeCompleted()
    )

    result = harness_mod._main_worktree_root()
    assert result is None, (
        f"_main_worktree_root must return None when the only entry is "
        f"bare; got {result}. REGRESSION (card e1e32b07 REWORK Rin "
        f"HIGH): the resolver would otherwise return the bare path "
        f"and the refuse-when-live guard would silently skip its check."
    )


def test_guard_main_root_resolver_skips_bare_middle_block(monkeypatch, tmp_path):
    """Card e1e32b07 REWORK (Rin HIGH finding): when a ``bare`` block
    appears BETWEEN non-bare blocks, the resolver must still return the
    FIRST non-bare, non-detached block (not the bare one, not any
    block that follows the bare one).

    This guards against a regression where a future refactor might
    accidentally pick a worktree that appears AFTER a bare block in
    the list — e.g. a feature worktree registered alongside a bare
    sibling.
    """
    import scripts.backtest_blend_harness as harness_mod

    bare_path = tmp_path / "bare_sibling"
    bare_path.mkdir()
    primary_path = tmp_path / "primary"
    primary_path.mkdir()
    feature_path = tmp_path / "feature"
    feature_path.mkdir()

    class _FakeCompleted:
        returncode = 0
        # Block order: primary → bare → feature. The resolver must
        # return ``primary`` (FIRST non-bare, non-detached), not
        # ``bare_path`` and not ``feature_path``.
        stdout = (
            f"worktree {primary_path}\n"
            "HEAD abcdef0123456789\n"
            "branch refs/heads/main\n"
            "\n"
            f"worktree {bare_path}\n"
            "bare\n"
            "\n"
            f"worktree {feature_path}\n"
            "HEAD 1234567890abcdef\n"
            "branch refs/heads/feature/x\n"
        )
        stderr = ""

    monkeypatch.setattr(
        harness_mod.subprocess, "run", lambda *a, **kw: _FakeCompleted()
    )

    result = harness_mod._main_worktree_root()
    assert result == primary_path, (
        f"_main_worktree_root must return the FIRST non-bare, non-detached "
        f"block ({primary_path}); got {result}. A bare block between the "
        f"primary and a feature worktree must NOT shift the anchor."
    )
    assert result != bare_path, (
        f"_main_worktree_root must NEVER return a bare repo path; got {result}"
    )
    assert result != feature_path, (
        f"_main_worktree_root must return the PRIMARY (first non-bare) "
        f"block, not a later feature worktree; got {result}"
    )


def test_guard_main_root_resolver_skips_detached_first_block(monkeypatch, tmp_path):
    """Card e1e32b07 REWORK (Rin HIGH finding): when the FIRST block
    carries the ``detached`` flag, the resolver must skip it and return
    the path of the next non-bare, non-detached block.

    The spec anchors LIVE paths to the PRIMARY (non-detached) worktree
    only — a detached worktree (e.g. mid-bisect) is not the primary
    checkout and must not anchor LIVE paths.
    """
    import scripts.backtest_blend_harness as harness_mod

    detached_path = tmp_path / "detached_wt"
    detached_path.mkdir()
    primary_path = tmp_path / "primary"
    primary_path.mkdir()

    class _FakeCompleted:
        returncode = 0
        stdout = (
            f"worktree {detached_path}\n"
            "HEAD abcdef0123456789\n"
            "detached\n"
            "\n"
            f"worktree {primary_path}\n"
            "HEAD 1234567890abcdef\n"
            "branch refs/heads/main\n"
        )
        stderr = ""

    monkeypatch.setattr(
        harness_mod.subprocess, "run", lambda *a, **kw: _FakeCompleted()
    )

    result = harness_mod._main_worktree_root()
    assert result == primary_path, (
        f"_main_worktree_root must skip the detached entry and return "
        f"the primary ({primary_path}); got {result}. REGRESSION: "
        f"pre-rework resolver returned the FIRST worktree line and "
        f"would have anchored LIVE paths to the detached worktree."
    )
    assert result != detached_path, (
        f"_main_worktree_root must NEVER return a detached worktree path "
        f"as the primary; got {result}"
    )
