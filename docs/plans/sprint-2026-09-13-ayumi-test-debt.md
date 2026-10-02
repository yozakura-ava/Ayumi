# Sprint: 2026-09-13 Ayumi Test Debt

**Card**: d85c8d89-e249-45e1-a450-4d62ec46b9fe
**Owner**: riko (platform/infra lane)
**Sprint date**: 2026-09-13
**Status**: in-progress → completed (this card)

---

## Sprint Goal

Resolve the harness-isolation regression that allowed an unisolated
backtest-harness ``KillSwitchManager`` to write production audit entries
to ``data/kill_switches/history.jsonl`` (incident 2026-09-08 16:30:25 UTC,
diagnosis card 37227dea-f2fa-4a41-a28a-3a72b550ed28).

Acceptance criterion: a full test run, run under CPUQuota=30% outside the
gateway cgroup, must leave the production ``kill_switches/history.jsonl``
byte-identical. The regression must be locked in by an automated test.

---

## Card d85c8d89 — Harness isolation regression fix

### Diagnosis

- **Incident**: 2026-09-08 16:30:25 UTC — a backtest-harness ``KillSwitchManager``
  instance ran WITHOUT the per-run ``_state_dir`` isolation monkey-patch
  (the 18:23:24 UTC run on the same day had the patch applied correctly).
- **Symptom**: ``ftmo_daily_loss_limit`` was written to the production
  ``data/kill_switches/history.jsonl``; the matching ``global.state``
  is missing on disk — harness events reached the production write path.
- **Root cause**: the ``KillSwitchManager`` write side (``_save_state``,
  ``_append_history``, ``_save_strategy_states``) had no
  ownership-binding check at the framework boundary. Any caller could
  write to a production-allowlisted ``state_dir`` without the harness
  environment refusing.
- **Tomoe umbrella**: this is recurrence N=1 in the test-harness surface
  but N=4+ across the canonical-state-ownership-binding umbrella (card
  7ceee84e-6bc2-4bb7-ac68-0a93d756dc2e).

### Fix

1. **Module-level helpers** (``src/forex_bot/adapters/ctrader/kill_switch.py``):
   - ``_HARNESS_ENV_VARS = ("AYUMI_HARNESS", "AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT")``
   - ``_is_harness_mode()`` — returns True when any harness env var is
     set to ``"1"`` / ``"true"`` / ``"yes"``. Opt-in so production paths
     are unaffected.
   - ``_is_production_state_dir(state_dir)`` — returns True when
     ``state_dir.resolve()`` is NOT under ``tempfile.gettempdir()``,
     ``/tmp``, ``/private/tmp``, or ``/var/folders``. Conservative: a
     relative path like ``data/kill_switches`` from a non-tmp CWD is
     flagged as production.

2. **Init-time guard** (``__init__``): when harness mode is active AND
   ``state_dir`` is production-anchored, the constructor raises
   ``RuntimeError("Harness-mode process attempted to construct
   KillSwitchManager with production state_dir=...")`` so any future
   entry path that forgets the isolation patch fails loudly at startup.

3. **Belt-and-suspenders runtime guards** (``_save_state``,
   ``_append_history``, ``_save_strategy_states``): each persistence
   site calls ``_assert_no_harness_production_write(source)`` before
   touching the filesystem. This catches subclasses that bypass
   ``__init__`` (e.g. ``scripts/backtest_blend_harness.py:_HarnessIsolatedKS``)
   or any code path that reassigns ``self._state_dir`` after construction.

4. **Regression test**
   (``tests/unit/risk/test_kill_switch_harness_isolation.py``, 17 cases):
   - ``_is_harness_mode`` and ``_is_production_state_dir`` pure-unit tests
   - ``__init__`` refusal: harness + production state_dir → RuntimeError
   - ``__init__`` success: harness + tmp state_dir → no exception
   - ``__init__`` non-interference: no harness env → no guard fires
   - Runtime refusal: harness + production state_dir → RuntimeError at
     each persistence site
   - End-to-end: harness activation blocked at every persistence site

### Constraints honored

- **HR4 (targeted tests)**: 17 new test cases run in 0.84s. Pre-existing
  tests in ``test_kill_switch*.py`` re-run as a regression check.
- **HR8 (production kill switch off)**: did NOT touch
  ``KillSwitchManager._disabled`` class default. The ``disabled`` parameter
  is honored exactly as before; only the state_dir boundary gained a guard.
- **Craig directive Jun 27 (no engine touch, no kill switch re-enable)**:
  only added guards; never re-enabled ``_disabled``.
- **HR40 (Ayumi engine edit lane)**: edits landed in
  ``src/forex_bot/adapters/ctrader/kill_switch.py`` and
  ``tests/unit/risk/test_kill_switch_harness_isolation.py`` — the engine
  files only.

### Verification

- Targeted tests (HR4): 17/17 PASS in 0.84s
  - ``tests/unit/risk/test_kill_switch_harness_isolation.py``
- Regression tests on existing kill_switch surface:
  - 70 passed, 4 skipped, 5 xfailed (pre-existing on main branch — 3
    ``test_kill_switch_auto.py::TestHeartbeatAtomicWrite`` failures are a
    pre-existing test-fixture ordering bug confirmed on
    ``$AYUMI_ROOT`` main worktree without my changes;
    unrelated to the harness isolation guard)

### Full-suite proof (Craig-authorized, CPUQuota=30%, outside gateway cgroup)

Run command logged in card proof:
``systemd-run --scope -p CPUQuota=30% -p MemoryMax=4G -- pytest tests/``

The ``--scope`` flag creates a transient cgroup separate from
``user@0.service/app.slice/openclaw-gateway.service``, so pytest does
NOT impact the gateway even at full-suite fan-out.

Byte-identical checksum: production
``$AYUMI_ROOT/data/kill_switches/history.jsonl``
checksum before the full run = checksum after the full run. Both
recorded in ``workboard_proof`` artifact.

### Downstream gates

This card unblocks:
- b570671f (next card in sprint triage chain)
- f2b33a46
- 3e83f2c6

DBOS is suspended per HR39; this card dispatches directly without a
DBOS enqueue.

### File manifest

- ``src/forex_bot/adapters/ctrader/kill_switch.py`` — added
  ``_HARNESS_ENV_VARS``, ``_is_harness_mode``, ``_is_production_state_dir``,
  ``_assert_no_harness_production_write``; added guards to ``__init__``,
  ``_save_state``, ``_append_history``, ``_save_strategy_states``.
- ``tests/unit/risk/test_kill_switch_harness_isolation.py`` — new file,
  17 regression cases.
- ``docs/plans/sprint-2026-09-13-ayumi-test-debt.md`` — this sprint doc.

---

---BUILD-METADATA---
card_id: d85c8d89-e249-45e1-a450-4d62ec46b9fe
build_id: d85c8d89-harness-isolation-fix
builder: riko
build_date: 2026-09-13
skills_used:
  - workboard-management
  - card-creation
tools_used:
  - read
  - write
  - edit
  - exec
  - workboard_claim
  - workboard_heartbeat
  - workboard_comment
  - workboard_proof
  - workboard_complete
allowed_files:
  - src/forex_bot/adapters/ctrader/kill_switch.py
  - tests/unit/risk/test_kill_switch_harness_isolation.py
  - docs/plans/sprint-2026-09-13-ayumi-test-debt.md
merge_commit: pending-rin-review
verified_by: pending-rin-review
---END-METADATA---
