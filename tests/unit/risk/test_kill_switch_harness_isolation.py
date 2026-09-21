"""Regression tests for KillSwitchManager harness isolation (card d85c8d89).

Root-cause finding (diagnosis card 37227dea, 2026-09-10): at 16:30:25 UTC Sep 8
a backtest-harness ``KillSwitchManager`` instance ran WITHOUT the per-run
``_state_dir`` isolation monkey-patch and wrote ``ftmo_daily_loss_limit`` to
the production ``data/kill_switches/history.jsonl``. The matching
``global.state`` is missing on disk — harness events can pollute production
kill-switch state.

Fix: ``adapters.ctrader.kill_switch`` now exports ``_is_harness_mode`` and
``_is_production_state_dir`` helpers plus a ``__init__`` guard plus
runtime guards in ``_save_state`` / ``_append_history`` /
``_save_strategy_states`` that refuse writes from harness-mode processes
whose ``state_dir`` resolves outside the OS tmp prefix.

These tests pin the boundary so any future regression that lets a harness
process write to ``data/kill_switches/`` fails this test before reaching
production. The conftest ``_guard_repo_data_writes`` autouse fixture (card
84df1bcc) is the second line of defense; this test enforces the write-side
refusal directly.
"""

import os
import tempfile
from pathlib import Path

import pytest

from adapters.ctrader.kill_switch import (
    DEFAULT_STATE_DIR,
    HISTORY_FILE,
    KillSwitchManager,
    _is_harness_mode,
    _is_production_state_dir,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def harness_env(monkeypatch):
    """Activate harness mode for the duration of one test.

    Sets ``AYUMI_HARNESS=1`` so ``_is_harness_mode()`` returns True, and
    cleans up automatically via ``monkeypatch``.
    """
    monkeypatch.setenv("AYUMI_HARNESS", "1")
    yield
    # monkeypatch tears down env vars on fixture finalization.


@pytest.fixture
def isolated_state_dir(tmp_path):
    """A temp state_dir under ``tempfile.gettempdir()`` for harness runs."""
    d = tmp_path / "kill_switches"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Helper-function tests (pure unit, no harness env needed)
# ---------------------------------------------------------------------------


class TestIsHarnessMode:
    """``_is_harness_mode`` reads env vars and returns a bool."""

    def test_default_off(self, monkeypatch):
        # Strip harness env vars so the helper sees a clean slate.
        for var in ("AYUMI_HARNESS", "AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT"):
            monkeypatch.delenv(var, raising=False)
        assert _is_harness_mode() is False

    def test_ayumi_harness_on(self, monkeypatch):
        monkeypatch.setenv("AYUMI_HARNESS", "1")
        assert _is_harness_mode() is True

    def test_ayumi_harness_true(self, monkeypatch):
        monkeypatch.setenv("AYUMI_HARNESS", "true")
        assert _is_harness_mode() is True

    def test_ayumi_harness_yes(self, monkeypatch):
        monkeypatch.setenv("AYUMI_HARNESS", "yes")
        assert _is_harness_mode() is True

    def test_ayumi_harness_off_value(self, monkeypatch):
        # Anything other than "1"/"true"/"yes" is treated as off.
        monkeypatch.setenv("AYUMI_HARNESS", "0")
        assert _is_harness_mode() is False

    def test_local_root_bypass_on(self, monkeypatch):
        monkeypatch.setenv("AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT", "1")
        assert _is_harness_mode() is True


class TestIsProductionStateDir:
    """``_is_production_state_dir`` flags non-tmp paths as production."""

    def test_tmp_under_gettempdir_is_not_production(self, tmp_path):
        # tmp_path is under tempfile.gettempdir() on every platform pytest
        # supports — this is the safe harness path.
        d = tmp_path / "kill_switches"
        d.mkdir()
        assert _is_production_state_dir(d) is False

    def test_default_data_dir_is_production(self):
        # The module default ``data/kill_switches`` resolves to CWD-relative
        # which is not under tmp — that IS the production scenario the
        # 2026-09-08 regression hit.
        assert _is_production_state_dir(Path(DEFAULT_STATE_DIR)) is True

    def test_relative_path_in_cwd_is_production(self, monkeypatch):
        # If the harness CWD is somewhere outside tmp (e.g. /home/.../Ayumi),
        # a relative "data/kill_switches" resolves to a production path.
        # Use the real repo root as the non-tmp CWD (card efec4ac4: the old
        # fixture passed the literal relative string "$AYUMI_ROOT/data/...",
        # which resolved under the tmp_path CWD and trivially passed the
        # tmp-prefix check — the env var was never expanded).
        repo_root = Path(__file__).resolve().parents[3]
        monkeypatch.chdir(repo_root)
        non_tmp = Path("data/kill_switches")
        assert _is_production_state_dir(non_tmp) is True

    def test_explicit_temp_subdir_is_not_production(self):
        with tempfile.TemporaryDirectory() as td:
            ks = Path(td) / "kill_switches"
            ks.mkdir()
            assert _is_production_state_dir(ks) is False


# ---------------------------------------------------------------------------
# Init-time guard: harness + production state_dir → RuntimeError
# ---------------------------------------------------------------------------


class TestInitGuard:
    """``__init__`` refuses harness + production state_dir at construction."""

    def test_harness_with_production_state_dir_raises(
        self, harness_env, tmp_path
    ):
        """Regression: harness run with production state_dir must fail loud.

        Simulates the 2026-09-08 16:30:25 UTC incident where the harness
        ran without the isolation patch. The constructor must refuse
        with a clear RuntimeError instead of silently writing to
        production ``data/kill_switches/``.
        """
        # Anchor tmp_path under a non-tmp prefix so _is_production_state_dir
        # returns True. Use a path outside tmp/ that still exists locally.
        production_path = Path("$AYUMI_ROOT/data/kill_switches")
        # Don't require the path to exist — the guard must catch this
        # BEFORE any filesystem operation, so a missing dir is fine.
        # If it doesn't exist on this machine, fall back to a symlink-free
        # non-tmp path we can create.
        if not production_path.exists():
            # Use /var/tmp if present, otherwise an explicit non-tmp local
            # dir under /opt or /srv. /opt exists on most test images.
            fallback = Path("/opt") / "ayumi_test_production_state"
            fallback.mkdir(parents=True, exist_ok=True)
            production_path = fallback / "kill_switches"
            production_path.mkdir(exist_ok=True)

        with pytest.raises(RuntimeError) as excinfo:
            KillSwitchManager(state_dir=str(production_path))
        msg = str(excinfo.value)
        assert "Harness-mode process" in msg
        assert "d85c8d89" in msg
        assert str(production_path) in msg

    def test_harness_with_tmp_state_dir_succeeds(
        self, harness_env, isolated_state_dir
    ):
        """Harness run with isolated tmp state_dir must work normally."""
        mgr = KillSwitchManager(state_dir=str(isolated_state_dir), disabled=False)
        # Activating kill should succeed and write to the isolated dir.
        mgr.activate_global_kill(reason="regression_test", triggered_by="test")
        history = isolated_state_dir / HISTORY_FILE
        assert history.exists(), (
            "isolated harness write should land in tmp state_dir, not production"
        )
        assert "regression_test" in history.read_text()

    def test_non_harness_with_production_state_dir_succeeds(
        self, tmp_path, monkeypatch
    ):
        """Production launcher path (no AYUMI_HARNESS) is unaffected.

        Verifies the guard is opt-in via env var so production paths
        that do not set ``AYUMI_HARNESS`` continue to behave as before.
        """
        # Ensure harness env vars are NOT set.
        for var in ("AYUMI_HARNESS", "AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT"):
            monkeypatch.delenv(var, raising=False)
        # Use a tmp path that we can write to; the guard must NOT fire.
        mgr = KillSwitchManager(state_dir=str(tmp_path / "kill_switches"))
        # No exception means the guard correctly stayed silent.
        assert mgr._state_dir == tmp_path / "kill_switches"


# ---------------------------------------------------------------------------
# Runtime guard: persistence methods refuse writes post-init
# ---------------------------------------------------------------------------


class TestRuntimeGuard:
    """Persistence methods re-check the guard on every call (belt-and-suspenders)."""

    def test_append_history_refuses_in_harness_with_production_dir(
        self, harness_env, monkeypatch, tmp_path
    ):
        """Even if the object was constructed without the guard firing
        (e.g. env var was set AFTER __init__), ``_append_history`` must
        refuse the write when the harness env is active and state_dir
        is production.
        """
        # Construct the manager WITHOUT the harness env so __init__ does
        # not raise, then enable the harness env and call _append_history.
        monkeypatch.delenv("AYUMI_HARNESS", raising=False)
        # Use a tmp state_dir so __init__ does not raise.
        ks_dir = tmp_path / "kill_switches"
        ks_dir.mkdir()
        mgr = KillSwitchManager(state_dir=str(ks_dir), disabled=False)

        # Now flip the harness env. The runtime guard should fire.
        monkeypatch.setenv("AYUMI_HARNESS", "1")

        # Move state_dir to a production path AFTER init to simulate the
        # ``_HarnessIsolatedKS`` subclass reassignment edge case.
        # We use a path under /opt or /srv that exists; fall back to /tmp.
        non_tmp = Path("/opt")
        if non_tmp.exists():
            fake_production = non_tmp / "ayumi_test_runtime_production" / "kill_switches"
            fake_production.mkdir(parents=True, exist_ok=True)
        else:
            # On unusual images without /opt, this branch documents the
            # intent but the test will be skipped — the init-time guard
            # already covers the standard regression path.
            pytest.skip("requires /opt for non-tmp production path simulation")
        mgr._state_dir = fake_production

        with pytest.raises(RuntimeError) as excinfo:
            mgr._append_history({"ts": "now", "event": "test"})
        assert "Harness-mode process" in str(excinfo.value)
        assert "_append_history" in str(excinfo.value)

    def test_save_state_refuses_in_harness_with_production_dir(
        self, harness_env, monkeypatch, tmp_path
    ):
        """``_save_state`` re-checks the guard on every call."""
        monkeypatch.delenv("AYUMI_HARNESS", raising=False)
        ks_dir = tmp_path / "kill_switches"
        ks_dir.mkdir()
        mgr = KillSwitchManager(state_dir=str(ks_dir), disabled=False)
        monkeypatch.setenv("AYUMI_HARNESS", "1")

        non_tmp = Path("/opt")
        if non_tmp.exists():
            fake_production = non_tmp / "ayumi_test_runtime_production_state" / "kill_switches"
            fake_production.mkdir(parents=True, exist_ok=True)
        else:
            pytest.skip("requires /opt for non-tmp production path simulation")
        mgr._state_dir = fake_production

        with pytest.raises(RuntimeError) as excinfo:
            mgr._save_state()
        assert "_save_state" in str(excinfo.value)

    def test_save_strategy_states_refuses_in_harness_with_production_dir(
        self, harness_env, monkeypatch, tmp_path
    ):
        """``_save_strategy_states`` re-checks the guard on every call."""
        monkeypatch.delenv("AYUMI_HARNESS", raising=False)
        ks_dir = tmp_path / "kill_switches"
        ks_dir.mkdir()
        mgr = KillSwitchManager(state_dir=str(ks_dir), disabled=False)
        monkeypatch.setenv("AYUMI_HARNESS", "1")

        non_tmp = Path("/opt")
        if non_tmp.exists():
            fake_production = (
                non_tmp / "ayumi_test_runtime_production_strategies" / "kill_switches"
            )
            fake_production.mkdir(parents=True, exist_ok=True)
        else:
            pytest.skip("requires /opt for non-tmp production path simulation")
        mgr._state_dir = fake_production

        with pytest.raises(RuntimeError) as excinfo:
            mgr._save_strategy_states()
        assert "_save_strategy_states" in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end: isolated harness run cannot reach production (conftest-paired)
# ---------------------------------------------------------------------------


class TestEndToEndIsolation:
    """Full flow: harness env + production state_dir = blocked everywhere."""

    def test_harness_activation_blocked_at_every_persistence_site(
        self, harness_env, tmp_path
    ):
        """A harness run that tries to construct with a production path
        fails at __init__ before any persistence call. This pins the
        boundary so any future entry path that forgets the redirect
        fails loudly at startup.
        """
        non_tmp = Path("/opt")
        if non_tmp.exists():
            fake_production = (
                non_tmp / "ayumi_test_e2e_production" / "kill_switches"
            )
            fake_production.mkdir(parents=True, exist_ok=True)
        else:
            pytest.skip("requires /opt for non-tmp production path simulation")

        # 1. __init__ refuses.
        with pytest.raises(RuntimeError):
            KillSwitchManager(state_dir=str(fake_production))

        # 2. The companion conftest guard (card 84df1bcc) is autouse; we
        #    only need to assert that no kill_switch artifact landed in
        #    the fake production dir from this test.
        history = fake_production / HISTORY_FILE
        assert not history.exists(), (
            "harness run leaked to production kill_switches dir"
        )
