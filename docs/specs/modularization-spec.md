# Modularization Spec — Archive Extraction & Launcher Split

**Author:** Ava Daigo  
**Date:** 2026-06-24  
**Status:** Draft  
**Phase:** 1B (code block lifted)

---

## Executive Summary

The forward test system has a **fake archive**: `archive/legacy_ctrader/_pkg/` was "archived" on 2026-06-16 (BQ-1043 Phase 5) but runtime code still imports from it via a shim. The main launcher is also a 688-line monolith. This spec covers extracting the archive code into proper modules and splitting the launcher.

**Scope:** 2 work items, 7 sub-tasks, 6 test file updates. Estimated 2 sprint items.

---

## Part A: Archive Extraction

### Current State

The shim at `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` (52 lines) re-exports everything from `archive/legacy_ctrader/_pkg/open_api_spot_feed.py` (1132 lines). This is the **only runtime import path** into the archive — but it's a big one (the entire `OpenApiSpotFeed` class).

**Runtime imports from archive (src/ + scripts/):**

| File | Import | What it uses |
|------|--------|--------------|
| `src/.../open_api_spot_feed.py` (shim) | `from archive.legacy_ctrader._pkg.open_api_spot_feed import *` | Re-exports everything |
| `src/.../open_api_spot_feed.py` (shim) | `from archive.legacy_ctrader._pkg import open_api_spot_feed as _archive_mod` | Underscore-prefixed names |
| `scripts/verify_live_state.py` | `from archive.legacy_ctrader._pkg.open_api_spot_feed import OpenApiSpotFeed` | Direct import for verification script |

**Test imports from archive:**

| File | Import | What it uses |
|------|--------|--------------|
| `tests/test_open_api_spot_feed.py` | `import archive.legacy_ctrader._pkg.open_api_spot_feed as mod` | Module-level introspection (2 sites) |
| `tests/test_open_api_spot_feed.py` | `from archive.legacy_ctrader._pkg.connection_state import ConnectionState` | Cross-module enum identity test |
| `tests/test_connection_self_healing.py` | `from archive.legacy_ctrader._pkg.connection_state import ConnectionState as ArchiveConnectionState` | Cross-module enum identity test |
| `tests/adapters/ctrader/test_open_api_volume_decoder.py` | `from archive.legacy_ctrader._pkg.open_api_spot_feed import (...)` | Decoder function tests |

### Files Already Duplicated in src/

Most archive `_pkg/` files **already exist** in `src/forex_bot/adapters/ctrader/`:

| Archive File | In src/? | Identical? | Notes |
|-------------|----------|------------|-------|
| `models.py` (177 lines) | ✅ | ✅ Identical | Pure move — just update imports |
| `connection_state.py` (206 lines) | ✅ | ✅ Identical | Pure move |
| `connection.py` (418 lines) | ✅ | ✅ Identical | Pure move |
| `credentials.py` (367 lines) | ✅ | ✅ Identical | Pure move |
| `error_classifier.py` (65 lines) | ✅ | ✅ Identical | Pure move |
| `market_data_feed.py` (345 lines) | ✅ | ✅ Identical | Pure move |
| `market_hours.py` (38 lines) | ✅ | ✅ Identical | Pure move |
| `protocols.py` (306 lines) | ✅ | ✅ Identical | Pure move |
| `reactor_manager.py` (69 lines) | ✅ | ✅ Identical | Pure move |
| `reconnect_strategy.py` (203 lines) | ✅ | ✅ Identical | Pure move |
| `token_manager.py` (448 lines) | ✅ | ✅ Identical | Pure move |
| `auth.py` (296 lines) | ✅ | ⚠️ Different | 1-line path fix (`parents[3]` → `parents[4]`). Src version is correct. |
| `connection_manager.py` (699 lines) | ✅ | ⚠️ Different | Src has BQ-1329 doc additions. Src version is newer. |
| `open_api_spot_feed.py` (1132 lines) | ✅ (52-line shim) | ❌ Shim only | **THE BIG ONE** — needs full extraction |
| `oauth_refresh.py` (311 lines) | ❌ Not in src/ | N/A | Only imported in tests. `token_lifecycle.py` covers similar territory. |
| `__init__.py` | ✅ | ⚠️ Different | Different content, src version is current |

**Key finding:** 12 of 16 files are already identical duplicates. The archive is 90% dead weight — only `open_api_spot_feed.py` is the real dependency.

### A1: Extract `open_api_spot_feed.py` (THE BIG ONE)

**Source:** `archive/legacy_ctrader/_pkg/open_api_spot_feed.py` (1132 lines)  
**Target:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` (replace the 52-line shim with the real code)

**What it contains:**
- `OpenApiSpotFeed` class — the main cTrader spot feed + order execution client
- `_normalize_symbol_name()` helper
- `_lots_to_units()` helper
- Constants: `_HEARTBEAT_DEGRADED_SEC`, `_HEARTBEAT_RECONNECT_SEC`, `_STALE_TICK_WARN_SEC`, `_STALE_TICK_FREEZE_SEC`, `_APP_AUTH_RES_PAYLOAD_TYPE`, `_ACCT_AUTH_RES_PAYLOAD_TYPE`

**Internal imports (relative, need updating after move):**
```python
from .market_data_feed import Tick, SymbolInfo     # → stays relative, works in new location ✅
from .connection import CTraderConnection           # ✅
from .connection_state import ConnectionState, ConnectionStateManager  # ✅
from .token_manager import TokenManager, TokenStatus  # ✅
from .auth import CTraderAuth                        # ✅ (src version has correct path)
from .models import ...                              # ✅
from .market_hours import is_forex_market_closed     # ✅
```

All relative imports will work as-is after the move since the dependencies already exist in `src/forex_bot/adapters/ctrader/`.

**Path fix needed:** The archive version has `_PROJECT_ROOT` calculation based on archive depth. After move to `src/`, the path needs `parents[4]` instead of `parents[3]`. **BUT** the src/ version of `auth.py` already fixed this, and the `OpenApiSpotFeed` class itself doesn't compute project root — it receives credentials as constructor args. So this is likely a non-issue.

**Risk:** Low. The relative imports all resolve to files that already exist in the target directory. No circular dependencies.

**Steps:**
1. Copy `archive/legacy_ctrader/_pkg/open_api_spot_feed.py` → `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` (overwrite shim)
2. Remove the shim code entirely
3. Verify the file's relative imports resolve correctly
4. Run: `python -c "import sys; sys.path.insert(0, 'src/forex_bot'); from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed; print('OK')"`
5. Run tests

**Effort:** M (single file move + verify)

### A2: Update `verify_live_state.py`

**Current:** `from archive.legacy_ctrader._pkg.open_api_spot_feed import OpenApiSpotFeed`  
**After:** `from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed`

**Effort:** S (1 line change)

### A3: Update Test Imports

**Test files needing changes:**

1. **`tests/test_open_api_spot_feed.py`** (lines 366, 378, 518):
   - `import archive.legacy_ctrader._pkg.open_api_spot_feed as mod` → `import adapters.ctrader.open_api_spot_feed as mod`
   - `from archive.legacy_ctrader._pkg.connection_state import ConnectionState` → `from adapters.ctrader.connection_state import ConnectionState`

2. **`tests/test_connection_self_healing.py`** (line 126):
   - `from archive.legacy_ctrader._pkg.connection_state import ConnectionState as ArchiveConnectionState` → `from adapters.ctrader.connection_state import ConnectionState as ArchiveConnectionState`
   - ⚠️ **Check:** This test deliberately imports from both archive and src to verify enum identity across modules. After extraction, there's only one `ConnectionState`. The test may need to be simplified or the cross-module check removed.

3. **`tests/adapters/ctrader/test_open_api_volume_decoder.py`** (line 40):
   - `from archive.legacy_ctrader._pkg.open_api_spot_feed import (...)` → `from adapters.ctrader.open_api_spot_feed import (...)`

**Effort:** S (4 import lines + 1 test logic review)

### A4: Handle `oauth_refresh.py`

**Status:** Exists only in archive (311 lines). Referenced in `tests/test_connection_manager_wiring.py` as `adapters.ctrader.oauth_refresh`.

**Finding:** Tests already import it as `adapters.ctrader.oauth_refresh` but the file isn't in `src/`. There's a `.pyc` in `__pycache__` meaning it used to exist. It was likely removed from src/ but the test still references it. `token_lifecycle.py` (443 lines, in src/) covers similar functionality.

**Action:** Copy `oauth_refresh.py` to `src/forex_bot/adapters/ctrader/oauth_refresh.py`. No import changes needed since tests already use the `adapters.ctrader.` path.

**Effort:** S (1 file copy)

---

## Part B: Launcher Split

### Current Structure

`scripts/launch_blend_forward_test.py` — 688 lines:

| Section | Lines | Size |
|---------|-------|------|
| Imports | 1–47 | 47 |
| `CorrelationGate` class | 48–77 | 30 |
| `HeartbeatTracker` class | 78–127 | 50 |
| `raw_bars_to_bar_objects()` | 128–147 | 20 |
| `trade_signal_to_blend_dict()` | 148–162 | 15 |
| `BlendForwardTestEngine` class | 163–433 | **271** |
| `build_blend_runner()` | 434–450 | 17 |
| `wire_connection_reliability()` | 451–465 | 15 |
| `main()` | 466–688 | **223** |

### B1: Extract `BlendForwardTestEngine` → `src/forex_bot/engine/blend_engine.py`

**What it contains:**
- `BlendForwardTestEngine(ForwardTestEngine)` — subclass with blend-specific signal evaluation and routing
- Methods: `_evaluate_strategies()`, `_route_signal()`, `on_position_closed_release()`
- Constructor takes `blend_runner`, correlation gate, heartbeat tracker

**Dependencies:**
- `ForwardTestEngine` (from `adapters.ctrader`)
- `BlendForwardTestRunner` (from `forward_test.blend_runner`)
- `TradeSignal`, `Bar`, `BarPeriod` types
- `CorrelationGate`, `HeartbeatTracker` (move alongside or keep in launcher)

**Target:** `src/forex_bot/engine/blend_engine.py` (~271 lines)

**Effort:** M (move class + fix imports + new `__init__.py` for `engine/` package)

### B2: Extract `CorrelationGate` + `HeartbeatTracker` → `src/forex_bot/engine/guards.py`

**What they do:**
- `CorrelationGate` — prevents correlated strategies from both taking positions on the same symbol
- `HeartbeatTracker` — monitors strategy evaluation frequency and flags stalls

**Target:** `src/forex_bot/engine/guards.py` (~80 lines)

**Effort:** S (2 class moves + minimal imports)

### B3: Extract health/heartbeat logging → `src/forex_bot/engine/health_logger.py`

**What it contains:** The health logging block inside `main()` (~80 lines of the 223):
- Periodic balance/equity logging
- Signal/fill mismatch alerts
- Bar build health checks
- Live vs paper balance reporting

**Target:** `src/forex_bot/engine/health_logger.py` (~100 lines, as a function `log_health_status(engine, mode, last_log_time, interval)`)

**Effort:** S (extract inline block into function + wire callback)

### B4: Slim `main()` in launcher

**After extraction, `main()` keeps:**
- Argument parsing
- Strategy construction
- PID guard
- Signal handler registration
- Engine construction (using extracted `BlendForwardTestEngine`)
- Main loop (simplified: call `log_health_status()` each tick)
- Shutdown

**Target size:** ~120 lines (down from 223)

**Total launcher after split:** ~150 lines (imports + helpers + `build_blend_runner()` + `wire_connection_reliability()` + slim `main()`)

---

## Execution Order

Each item is independently testable. Run `pytest tests/ -q` after each step.

| Step | Task | Depends On | Effort | Risk |
|------|------|-----------|--------|------|
| 1 | A4: Copy `oauth_refresh.py` to src/ | None | S | Low |
| 2 | A1: Replace shim with real `open_api_spot_feed.py` | None | M | Low |
| 3 | A2: Update `verify_live_state.py` import | Step 2 | S | Low |
| 4 | A3: Update 3 test files' imports | Step 2 | S | Low |
| 5 | **VERIFY:** `grep -rn "from archive" src/ scripts/` is empty | Steps 1–4 | — | — |
| 6 | **VERIFY:** `pytest tests/ -q` all pass | Steps 1–4 | — | — |
| 7 | B2: Extract `guards.py` (CorrelationGate + HeartbeatTracker) | None | S | Low |
| 8 | B1: Extract `blend_engine.py` (BlendForwardTestEngine) | Step 7 | M | Medium |
| 9 | B3: Extract `health_logger.py` | None | S | Low |
| 10 | B4: Slim `main()` | Steps 7–9 | S | Low |
| 11 | **VERIFY:** `python scripts/launch_blend_forward_test.py --help` works | Steps 7–10 | — | — |
| 12 | **VERIFY:** `pytest tests/ -q` all pass | Steps 7–10 | — | — |

**Recommended batching for sprint:**
- **Sprint item 1:** Steps 1–6 (archive extraction) — can be done as one PR
- **Sprint item 2:** Steps 7–12 (launcher split) — can be done as one PR

---

## Acceptance Criteria

### Archive Extraction (Part A)
- [ ] `grep -rn "from archive\|import archive" src/ scripts/ --include="*.py" | grep -v __pycache__` returns empty
- [ ] `grep -rn "from archive\|import archive" tests/ --include="*.py" | grep -v __pycache__` returns empty (or only historical comments)
- [ ] `python scripts/launch_blend_forward_test.py --help` runs without import errors
- [ ] `python scripts/verify_live_state.py --help` runs without import errors
- [ ] All existing tests pass (`pytest tests/ -q`)
- [ ] No circular imports introduced

### Launcher Split (Part B)
- [ ] `scripts/launch_blend_forward_test.py` is under 200 lines
- [ ] `src/forex_bot/engine/blend_engine.py` exists and contains `BlendForwardTestEngine`
- [ ] `src/forex_bot/engine/guards.py` exists and contains `CorrelationGate` + `HeartbeatTracker`
- [ ] `src/forex_bot/engine/health_logger.py` exists and contains the health logging function
- [ ] All existing tests pass
- [ ] `python scripts/launch_blend_forward_test.py --help` runs without import errors

### Overall
- [ ] No runtime code imports from `archive/`
- [ ] The `archive/` directory can remain on disk (for git history) but is dead code
- [ ] Full test suite passes: `pytest tests/ -q`

---

## Risk Assessment

**Low risk:**
- 12 of 16 archive files are identical duplicates — no real extraction needed, just import updates
- The `open_api_spot_feed.py` extraction is a single-file move with relative imports that already resolve in the target
- Launcher split is mechanical (move classes/functions to new files, update imports)

**Medium risk:**
- `test_connection_self_healing.py` deliberately tests cross-module enum identity between archive and src versions. After extraction, this test logic needs updating (both sides point to the same module). May need test rewrite.
- The `BlendForwardTestEngine` subclass is tightly coupled to `ForwardTestEngine` — the split must preserve the inheritance chain correctly.

**Things to verify after extraction:**
- `OpenApiSpotFeed._auth()` uses `Path(__file__).resolve().parents[N]` for project root — verify N is correct after move
- The `.env` loading path in the launcher still resolves correctly after imports change

---

## Estimated Effort Summary

| Part | Steps | Effort | Sprint Items |
|------|-------|--------|-------------|
| A: Archive Extraction | 6 steps | 1×M + 3×S | 1 sprint item |
| B: Launcher Split | 6 steps | 1×M + 3×S | 1 sprint item |
| **Total** | **12 steps** | **2×M + 6×S** | **2 sprint items** |
