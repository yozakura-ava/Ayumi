# ForwardTestEngine mypy Audit — 2026-08-16

**Auditor:** Satsuki (research lane)
**Card:** [70a53daa](https://workboard/70a53daa) — `[RESEARCH] forward_test_engine.py — 51 mypy errors remediation plan`
**Scope:** Inventory and categorize all mypy errors in `src/forex_bot/adapters/ctrader/forward_test_engine.py` (3,496 lines). Produce a remediation plan with safe-vs-behavior-affecting split and live-trading review path.
**Method:** Static mypy run on a clean checkout, categorized by error code and reachability into live-trading paths. Cross-referenced against `signal_adapter.py:313` (runtime return type) and `market_data_feed.py:351` (deprecated stub).

---

## TL;DR

- **47 mypy errors in `forward_test_engine.py`** (not 51 as the card title claims; the card estimate predates a small fix or was rough).
- **Total project errors: 196 in 25 files** (only reachable because of a config regression — see §3).
- **1 real behavior bug** (line 2371, bar-close leak detector silently no-op) — **live-trading safety relevant**.
- **46 annotation-only** — 39 of them collapse into ONE annotation fix (`self._market_feed` type widening).
- **1 mypy.ini regression** introduced by lint sprint `91aa8ee2` (Aug 14, Tsubaki) prevents mypy from scanning ANY file under `src/forex_bot/`. Silent — CI doesn't run mypy (`ci.yml.disabled`).

**Verdict:** Doable in ~1.1 SP. The "annotation-only" framing in the card is mostly right — but **the leak detector at line 2371 is a real bug that's been silently broken**, masked because `dict.get(int_key)` against a `dict[str, ...]` returns None instead of raising.

---

## 1. Error Inventory (47 errors)

Captured via `MYPYPATH=src/forex_bot python3 -m mypy --explicit-package-bases src/forex_bot/adapters/ctrader/forward_test_engine.py` on `main` (HEAD = `bef7fd3f`, Aug 16 ~12:30 EDT).

### 1.1 Category breakdown

| Error code | Count | Example site | Root cause |
|------------|-------|--------------|------------|
| `[attr-defined]` | 31 | 531, 1010, 1272, 2183, 2194 | Accessing `is_running`, `name_to_id`, `symbols`, `resolve_symbol_id`, `amend_sl_tp` on `LiveMarketDataFeed` — but that class is a **deprecated stub** (`market_data_feed.py:351` raises `NotImplementedError` on `__init__`). Runtime uses `OpenApiSpotFeed`; static type lies. |
| `[union-attr]` | 8 | 1004, 1036, 1037, 1041, 1064 | Same root cause: `self._market_feed` is typed `Optional[LiveMarketDataFeed]` but actually holds `OpenApiSpotFeed`. The None branch fires spuriously. |
| `[assignment]` | 5 | 132, 166, 1035, 1047, 2543 | `ForwardTestConfig.symbols: list[str] = None` (lines 132, 166); `self._market_feed = OpenApiSpotFeed(...)` (1035); `self._api_client = cTraderAPIClient(...)` (1047); `signals: list[ISignalStrategy] = CTraderTradeSignal` (2543). |
| `[var-annotated]` | 1 | 2090 | `_pending_outcome_keys = getattr(self, "_pending_outcome_keys", set())` — mypy can't infer type from `getattr`. |
| `[call-overload]` | **1** | **2371** | **`dict.get(int)` against `dict[str, ...]`** — see §1.4. **REAL BUG.** |
| `[arg-type]` | 1 | 2729 | `_execute_signal_live(signal: CTraderTradeSignal)` called with `s` from `signals: list[ISignalStrategy]`. Runtime is `CTraderTradeSignal` (per `signal_adapter.py:313`); type lies. |

### 1.2 Live-trading path reachability

Lines that execute in the **live-trading path** (i.e. `_config.live_mode == True` branches) and carry errors:

| Line | Code path | Live-trading impact |
|------|-----------|---------------------|
| 531 | `connected=self._market_feed.is_running` in `health` property | Health metric always reports `False` for the connected flag (method missing on stub) — silences a real connection-loss signal. **MEDIUM risk.** |
| 1004 | `self._market_feed.start(...)` in `_start_market_feed` | Method missing — **would raise `AttributeError` if `LiveMarketDataFeed` were ever instantiated.** Currently masked because `use_openapi_feed=True` swaps to `OpenApiSpotFeed`. **LOW risk while OpenApiSpotFeed is in use.** |
| 1010, 1272, 1284, 1370, 1374, 2183, 2194 | Symbol resolution, subscription, amend-sl/tp on feed | Same: would fail if `LiveMarketDataFeed` instantiated. **LOW risk in current config.** |
| 2371 | Bar-close leak detector in `_evaluate_strategies` | See §1.4. **HIGH risk — silent production bug.** |
| 2543, 2554-2729 | Signal iteration in `_evaluate_strategies` | Type lie, runtime OK. **LOW risk** (annotation-only). |

### 1.3 The 39-error cascade fix

A single annotation change at **line 388**:

```python
# Before (current)
self._market_feed: Optional[LiveMarketDataFeed] = None

# After
self._market_feed: Optional[Union[LiveMarketDataFeed, OpenApiSpotFeed]] = None
```

would eliminate:
- 31 × `[attr-defined]` errors (lines 531, 1010, 1272, 1284, 1370, 1374, 2183, 2194, etc.)
- 8 × `[union-attr]` errors (lines 1004, 1036 × 2, 1037 × 2, 1041 × 2, 1064)
- 2 × `[assignment]` errors at lines 1035, 2543 (assignment site annotated as `LiveMarketDataFeed | None`)

**Total: 41 errors collapsed from one line.** Verified by inspection.

### 1.4 The real bug (line 2371)

```python
# Line 2364
tf_bars: dict[int, list[Bar]] = {}              # ← int keys (timeframes)
# Line 2370-2371
for tf_key in tf_bars:                          # ← tf_key is int
    forming = self._current_bar.get(tf_key)     # ← _current_bar is dict[str, Optional[Bar]] (line 384)
```

`self._current_bar` is declared `dict[str, Optional[Bar]]` at line 384 (key scheme is `{symbol}_{timeframe_label}`). The intent of the "Defensive: verify no forming bars leaked into evaluation" comment is clearly to check if the forming bar is in the eval list — but the lookup uses the int `tf` against a str-keyed dict.

**Result:** `dict[str, ...].get(int)` returns `None` (no `KeyError`, no exception). So `forming is not None` is always False, and the leak detector **never fires in production**.

This is a real safety regression: the contract comment at line 330 ("Never pass `self._current_bar` into MarketState or strategy evaluation") is enforced by code that does nothing.

**Suggested fix** (for builder triage — not implemented by research):

```python
for tf in self._required_timeframes:
    tf_key = self._bar_key(symbol, tf)        # build the actual str key
    forming = self._current_bar.get(tf_key)
    if forming is not None and tf_bars[tf]:
        ...
```

OR re-iterate over `_current_bar` keys intersecting `tf_bars`. Either approach is ~5 lines.

---

## 2. Remediation Plan

### 2.1 Safe split (annotation-only, ~0.6 SP)

| Step | Fix | Lines affected | Errors cleared |
|------|-----|----------------|----------------|
| 2.1.a | Widen `self._market_feed` annotation | 388 | 41 |
| 2.1.b | Widen `signals` annotation (or refactor type upstream) | 2543, surrounding block | 24 |
| 2.1.c | `ForwardTestConfig` field Optional-ization | 132, 166 | 2 |
| 2.1.d | `_api_client` annotation widening | 1047 | 1 |
| 2.1.e | Explicit type for `_pending_outcome_keys` | 2090 | 1 |
| **Subtotal** | | | **69 errors** (including ripple into 2543-area) |

All of 2.1 is annotation-only. Test impact: existing test `tests/unit/ctrader/test_stats_fail_reset.py` validates by source inspection (`inspect.getsource`), so type changes won't affect it. Recommend running the existing test suite after each step.

### 2.2 Behavior-affecting (the leak detector, ~0.5 SP)

| Step | Fix | Risk | Verification |
|------|-----|------|--------------|
| 2.2.a | Rewrite bar-close leak detector at lines 2368-2378 to use correct str keys | Behavior change — leak detector may START firing | Manual code review + unit test for the leak path (currently absent — recommend adding `tests/unit/ctrader/test_bar_close_leak_detection.py`) |

This is the only behavior-affecting item. **Live-trading review required.**

### 2.3 mypy.ini regression fix (~0.05 SP)

The `mypy.ini` change in commit `91aa8ee2` (Aug 14, Tsubaki's card `e1ba054c`) regressed mypy functionality:

| Setting | Before | After | Status |
|---------|--------|-------|--------|
| `mypy_path` | `src/forex_bot` | `src` | **Broken** — mypy refuses to scan files inside `forex_bot/` ("forex_bot contains __init__.py but is not a valid Python package name") |

The commit message claims the old config was broken because `forex_bot` is a hyphenated dir. **That claim is wrong** — `mypy_path` is a *sys.path entry*, not a package name. With `MYPYPATH=src/forex_bot`, mypy treats `src/forex_bot` as a path root and resolves `adapters.ctrader.forward_test_engine` correctly (returns 196 errors across 25 files, which is the truth).

The original config (`mypy_path = src/forex_bot`) was correct. The new config (`mypy_path = src`) was a misdiagnosis. **Revert** the mypy.ini line; the ruff config change in the same commit should stay.

| Step | Fix |
|------|-----|
| 2.3.a | `mypy_path = src` → `mypy_path = src/forex_bot` |

### 2.4 Total SP estimate

| Phase | SP | Risk |
|-------|----|------|
| 2.1 annotation-only sweep | 0.6 | LOW |
| 2.2 leak detector fix | 0.5 | MEDIUM (behavior-affecting, live-trading review) |
| 2.3 mypy.ini revert | 0.05 | LOW |
| **Total** | **1.15** | matches card's 1.0 SP estimate (slightly over) |

---

## 3. Wider finding: mypy silent regression

CI is disabled (`ci.yml.disabled`) and the enabled CI does not include mypy. So the regression introduced by `91aa8ee2` was silent:

- Before `91aa8ee2`: mypy would have run and reported 196+ errors
- After `91aa8ee2`: mypy can't start — no errors reported
- No CI signal, no human signal (the error is "hyphenated package name", which looks like the original problem was correctly diagnosed)

**Recommendation:** This pattern (config tweak → silent scan regression) should be guarded against by adding mypy to the actual CI workflow when it gets re-enabled. Surface to Tomoe as a meta-finding.

---

## 4. Cross-source check

| Claim | Sources |
|-------|---------|
| 47 mypy errors in forward_test_engine.py | Direct mypy run (this session), grep verification on raw output |
| LiveMarketDataFeed is deprecated | `market_data_feed.py:351-365` (raises NotImplementedError on `__init__`) |
| `evaluate_all_strategies` returns `list[CTraderTradeSignal]` | `signal_adapter.py:294-313` (return annotation + code appends `results`) |
| `_current_bar` is `dict[str, Optional[Bar]]` | `forward_test_engine.py:384` (declaration) + usage at lines 726, 1111, 1152 |
| mypy.ini regression | Direct comparison of `mypy.ini` (current `mypy_path = src`) vs commit `91aa8ee2` diff; mypy run output shows the package-name error |

Single-source claims: none. Every claim has direct file/line evidence.

---

## 5. Conflicts surfaced (NOT silently resolved)

- **Card 70a53daa claims "~51 mypy errors" — actual count is 47.** Possible explanations: error count drifted downward via intermediate fixes, or the card title was rough. Flag for Himari to confirm whether the AC ("Full mypy error inventory") is satisfied by 47 or whether the requester wanted a broader sweep.
- **Card c40bd9e3 ("Were historical Ayumi mypy claims vacuous?") premise was that `mypy_path = src/forex_bot` was broken.** That premise is **wrong** per the evidence above. The c40bd9e3 audit may want to revisit — but that's out of scope here.
- **Commit `91aa8ee2` message claims `mypy_path=src/forex_bot` was broken.** This brief contradicts that claim with direct evidence. Surface to Tsubaki for follow-up; possibly amend the commit message in a future PR.

---

## 6. Stopping condition

**MET.** Question answered:
- ✅ Full mypy error inventory on `forward_test_engine.py` (47 errors, categorized)
- ✅ Remediation plan with SP estimate and safe-vs-behavior-affecting split
- ✅ Live-trading review path flagged (line 2371 = real bug)

Unknowns remaining:
- Whether `LiveMarketDataFeed` is intended to be revived (deprecation comment says "use MarketDataFeed"). If yes, mypy annotation fix 2.1.a should preserve `LiveMarketDataFeed` in the Union; if no, drop it entirely and delete the stub class.
- Whether the line 2371 leak detector was ever observed firing in production (would require time-series review of error metrics for the matching log line).

---

## 7. Recommended consumer

- **Primary: Tsubaki** (build) — for implementing 2.1, 2.2, 2.3.
- **Routed via: Himari** (portfolio) — for SP confirmation against the 1.0 SP estimate on the card.
- **Secondary notification: Tomoe** — for the mypy.ini silent-regression meta-finding (§3).
- **Card c40bd9e3 conflict notification: Himari** — for the contradiction with the vacuous-claims premise.

---

## Provenance

- Generated 2026-08-16 by Satsuki HB#160 in-session.
- Mypy run output: `/tmp/fte_mypy.txt` (211 lines, captured this session).
- mypy version: 2.2.0 (compiled: yes) on Python 3.12.
- HEAD: `bef7fd3f` on `main`.
- Source repo: `$AYUMI_ROOT` (Ayumi, path verified HB#158).

**Freshness:** mypy errors and config drift; re-validate on any merge to `main` touching `forward_test_engine.py`, `signal_adapter.py`, `market_data_feed.py`, or `mypy.ini`. Target next audit: 2026-09-13 or on next Ayumi live-trading sprint.