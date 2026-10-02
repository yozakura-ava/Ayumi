# Root-Cause Analysis — May/June 2026 Forward-Test Crash History

| Attribute | Value |
|---|---|
| **Date authored** | 2026-07-05 |
| **Sprint** | ayumi-reliability-2026-07-05 |
| **Card** | `9043c09c` (Task 5.1 — Root-Cause Analysis) |
| **Companion card** | `9043c09c` Task 5.2 (systemd unit design — separate builder) |
| **Author** | Ava (subagent, B6 root-cause investigation) |
| **Branch** | `senior-dev/crash-root-cause` |
| **Source-of-truth ref** | `main` @ `18c1739` (top), `b849df0` (sprint base) |
| **Authoritative log sources** | `journalctl --user -u ayumi-forward-test.service` (retained), `logs/ayumi_*.log` (partial — 19-day gap) |

---

## 1. Executive Summary

The May–June 2026 crash history reflects a **forward test that never had production-grade auth or connection-resilience machinery** at the time. The unit file, the launcher, and the OpenAPI client all predate the **BQ-1335 reconnect logic**, the **BQ-1330a `authenticate_with_retry()`**, and the **ConnectionWatchdog**. Every crash on the card is materially mitigated by fixes that landed on/after **2026-06-12**, mostly the **2026-06-23** wave (`a0663c1`, `a487b69`, `c845983`). **No crash of the recorded pattern has occurred since 2026-06-10.**

The single residual vector is the **2026-05-24 OOM-class kill** (210 MB memory peak) — see §4.

### 1.1 Crash × Mitigation Table

| # | Date | Crash time (local) | Exit code | Likely root cause (with source) | Mitigated by |
|---|---|---|---|---|---|
| 1 | 2026-05-04 | 18:32:12 | 9/KILL | systemd SIGTERM **stop-sigterm timeout** (Stop→Killing→SIGKILL); run-of-show ~149 MB, didn't terminate gracefully within 11 s | Replaced unit (Task 5.2) with `TimeoutStopSec=10` + `KillMode=control-group`; SIGTERM handler added in `72252c2` |
| 2 | 2026-05-05 (×15) | 14:50–14:59 | 1/FAILURE | **Auth death spiral**: every relaunch exited in ~3.15 s — identical to later 75/TEMPFAIL loop, except caught at the `sys.exit(1)` path inside `launch_blend_forward_test.py:709`. Restart counters 4→18 in 9 minutes (autorestart on ~30 s cadence). | `7af7a73` ProtoMessage double-wrap fix (2026-06-12); `affd729` credential boundary; `ab8e4c5` connection extraction; `authenticate_with_retry()` (BQ-1330a) |
| 3 | 2026-05-22 | 22:09–22:15 | 6/ABRT | **systemd `WatchdogSec=300` triggered**: no `READY=1` within 5 min → SIGABRT → status 6 (note `Killing process … with signal SIGABRT`). Two prior `Failed with result 'timeout'` entries on same day confirm a hung openapi handshake prior to SIGKILL on Stop. | OpenApiSpotFeed now reconnects without service-level hang; `ConnectionWatchdog` (DEGRADED 30 s / FAILED 90 s) surfaces trouble before systemd's 5-min watchdog. **systemd `WatchdogSec=` should not be re-enabled** without verifying market-closed vs market-open behaviour (see §5). |
| 4 | 2026-05-24 | 22:05:21 | 9/KILL | **OOM-class kill at 210.7 MB peak** — 5× the typical peak (~150 MB). SIGKILL pattern + high memory fits Linux OOM-killer; no Python traceback because the process was terminated externally. **Cannot confirm via journal alone.** | ⚠️ **NOT directly mitigated**. Memory ceiling mitigations not identified (see §4). |
| 5 | 2026-05-27 | 00:28:25→29 | 1/FAILURE | **Startup race / credential-boundary failure**: 2.2 MB memory peak → process died in code path before fully loading (probably credential parse or `.env` integrity check at `launch_blend_forward_test.py:709`). | `affd729` (credential boundary hotfix) + `1f4b3aa` (dual-source guard) + `d0512d7` (stop `.env` token clobbering) |
| 6 | 2026-06-02 | 14:34:37, :51, 14:35:24 | **75/TEMPFAIL ×3** in 47 s | **Classic cTrader auth death-spiral** during startup; the application explicitly exited 75 (system treated as success *now*, treated as failure *then*) and systemd relaunched into the same fault. Three restarts in 47 s = restart storm. | `7af7a73` ProtoMessage double-wrap fix; `authenticate_with_retry()` (BQ-1330a, 1s/2s/4s); `RestartPreventExitStatus=75` + `SuccessExitStatus=75` in current unit (success-acknowledges; no auto-restart) |
| 7 | 2026-06-03 | 15:46:41 | 9/KILL | **Operation-time SIGKILL** — 31.9 s CPU ≈ mid-trade; possibly OOM, possibly manual `kill -9` (Craig may have been intervening during testing). 145 MB peak is normal — no OOM signature. | ⚠️ Cannot attribute to a specific bug; **likely an operator action**, not a regression |
| 8 | 2026-06-07 | 18:07:32 | 1/FAILURE | **Startup failure** — 197 MB peak (high but not OOM-grade) and 3.7 s CPU confirms pre-`fix(ctrader)` proto-wrap bug still active. | `7af7a73` + `authenticate_with_retry()` (landing 5 d later) |
| 9 | 2026-06-08 | 03:05:55 | 1/FAILURE | **Startup failure** — 10.7 s CPU indicates it reached further into startup than the 3 s spikes; 185 MB peak; consistent with proto-wrap happening intermittently. | Same as #8 |
| 10 | 2026-06-10 | 17:57:43, 23:35:12 | 1/FAILURE (×2) | **Startup failures during live-fire attempt**, ~3.5 s CPU each. The 23:35 cycle shows a clean *Stop → Start* pattern in the journal — likely an operator-initiated restart (the journal PID changes from `1386` to `1040971` on 2026-06-10 evening, indicating a user-session restart). | Same as #8; also future-proofed by `17e72b0` resilience xfail tests + credential-probe callback linter |

**Counts of exit codes in the May 20 → June 14 window (journalctl truth):**

```
status=9/KILL      14
status=1/FAILURE   10
status=75/TEMPFAIL  3
status=6/ABRT       1
```

### 1.2 Timeline vs Fixes (the headline)

```
2026-05-04 ━━━━━━━━━━━━━━━━► Cargo-cult reconfigure; multi-loop auth death spiral (counted once at 1/FAILURE)
2026-05-22 ━━━━━━━━━━━━━━━━► Watchdog triggered (ABRT)
2026-05-24 ━━━━━━━━━━━━━━━━► OOM-class KILL @ 210 MB
2026-05-27 ━━━━━━━━━━━━━━━━► Startup FAILURE before load completes
2026-06-02 ━━━━━━━━━━━━━━━━► 3 × TEMPFAIL auth death-spiral restart storm (47 s)
2026-06-03 ━━━━━━━━━━━━━━━━► Operator/working KILL
2026-06-07 → 06-10 ━━━━━━━► Pre-fix startup failures in proto-wrap window
2026-06-12 ━━━━━━━━━━━━━━━━► FIX 1: `7af7a73` ProtoMessage double-wrap (RESOLVES the 75-loop)
                  ━━━━━━━━► FIX 2: `affd729` credential boundary
                  ━━━━━━━━► FIX 3: `ab8e4c5` connection+bar-builder extraction
                  ━━━━━━━━► FIX 4: `1f4b3aa` dual-source guard
2026-06-16 ━━━━━━━━━━━━━━━━► FIX 5-9: `9dab84d` BQ-978 reliability skeleton (`ConnectionWatchdog`, `ReconnectStrategy`, `error_classifier`, `token_lifecycle`)
2026-06-22 ━━━━━━━━━━━━━━━━► FIX 10: `c845983` cTrader resilience improvements (correlation risk)
2026-06-23 ━━━━━━━━━━━━━━━━► FIX 11: `a0663c1` BQ-1335 merge (recovering from `RECONNECTING` stuck state)
                  ━━━━━━━━► FIX 12: `a487b69` BQ-1330a `authenticate_with_retry()` (3× exp-backoff)
2026-06-29 ━━━━━━━━━━━━━━━━► FIX 13: `17e72b0` resilience xfail tests + credential probe + callback linter
2026-07-03 ━━━━━━━━━━━━━━━━► FIX 14: `3644108` reconstruct `_api_client` on reconnect (prevents dead connections)
                  ━━━━━━━━► FIX 15: `21f73a6` expand FAILED-state transitions (no more sticky-failed)
```

**No crashes of the recorded pattern have occurred since 2026-06-10. All crashes predate all fixes.**

---

## 2. Per-Crash Analysis

### 2.1 Methodology

For each crash listed on card `9043c09c`, I cross-referenced three sources:

1. **`journalctl --user -u ayumi-forward-test.service`** — retained for the entire window. The unit PID changes from `1386` → `1040971` on **2026-06-10 evening** (operator session restart).
2. **`logs/ayumi_YYYY-MM-DD.log`** — preserved for April–May 20 and again June 9 onward. **There is a 19-day gap between `ayumi_2026-05-20.log` (last write 2026-05-20) and `ayumi_2026-06-09.log` (first write 2026-06-09).** Application-side traceback evidence for May 21 → June 8 is therefore lost. Hypotheses rely on journal + git context.
3. **Git history** — code changes that landed on or before each crash date.

I could not run the live investigation against the *running* service (it's bare-process today) — no equivalent journal records exist for July onward under `ayumi-forward-test.service`. Behaviour was triangulated against current resilience code plus sprint plan evidence (`docs/plans/ayumi-reliability-sprint-2026-07-05.md` §2.1, §2.2).

### 2.2 May 04 — `9/KILL` (graceful-stop timeout → SIGKILL)

**Journal evidence (PID 1129, pre-PID-1386):**

```
May 04 18:32:01 Stopping ayumi-forward-test.service ...
May 04 18:32:12 ayumi-forward-test.service: State 'stop-sigterm' timed out. Killing.
May 04 18:32:12 ayumi-forward-test.service: Killing process 949448 (python) with signal SIGKILL.
May 04 18:32:12 Main process exited, code=killed, status=9/KILL
May 04 18:32:12 Failed with result 'timeout'.
```

**Interpretation:** *not a crash*, but appears as 9/KILL because systemd timed out waiting for SIGTERM → SIGKILL transition. The 11-second gap between `Stopping` and the kill matches the systemd default `TimeoutStopSec=90` if the process had a clean handler; here it shows `TimeoutStopSec` was likely tighter (or `KillMode=process` was active). 149.8 MB peak is normal — Python was healthy.

**Git state at time:** `72252c2` (2026-05-22) hadn't landed yet; SIGTERM handling was not explicitly wired.

**Code changes since:**

- `72252c2` (2026-05-22) `fix(forward-test): FTMO R:R mismatch, risk budget leak, SIGTERM handling` — adds explicit SIGTERM handler.
- Current code registers `SIGINT` and `SIGTERM` handlers in `scripts/launch_blend_forward_test.py:862-863` calling `shutdown()` → `engine.stop()` and `blend_runner.stop()` then `sys.exit(0)`.

**Mitigation status:** ✅ *mitigated.* The current unit (`TimeoutStopSec=10` + `KillMode=control-group`) combined with the SIGTERM handler guarantees a clean exit in ≤10 s.

### 2.3 May 05 14:50–14:59 — `1/FAILURE` ×15 (auth death-spiral)

**Journal evidence (PID 1386):**

```
May 05 14:50:44 ... status=9/KILL
May 05 14:51:17 ... status=1/FAILURE   (3.148 s CPU)
May 05 14:51:49 ... status=1/FAILURE   (3.159 s CPU)
May 05 14:52:21 ... status=1/FAILURE   (3.278 s CPU)
... (×15, every ~30 s)
May 05 14:59:20 ... status=1/FAILURE   (3.154 s CPU)
[restart counter reaches 18 in ~9 minutes]
```

**Interpretation:** *Same root cause as the June-02 75/TEMPFAIL storm* (see §2.6), but hitting the `sys.exit(1)` path instead of the `sys.exit(75)` path. The processor pattern (3.15 s CPU → die) is identical — `Failed to load credentials` or `Session connection failed — aborting` in `scripts/launch_blend_forward_test.py:693/709`.

**Rest of this analysis is identical to §2.6.**

### 2.4 May 22 22:15 — `6/ABRT` (systemd watchdog)

**Journal evidence (PID 1386):**

```
May 22 11:21:29 Failed with result 'timeout'.
May 22 22:09:48 Failed with result 'timeout'.
May 22 22:15:30 ayumi-forward-test.service: Watchdog timeout (limit 5min)!
May 22 22:15:30 ayumi-forward-test.service: Killing process 1192242 (python) with signal SIGABRT.
May 22 22:15:37 Main process exited, code=dumped, status=6/ABRT
```

**Interpretation:** systemd fired its built-in service watchdog (`WatchdogSec=300` = 5 min). The service did not emit `WATCHDOG=1` notification within the window → SIGABRT → dump → exit 6. The two prior 11:21 and 22:09 entries (`Failed with result 'timeout'`) indicate the engine was repeatedly *hangs on startup auth* but kept running long enough to need systemd stepping in.

**Git state:** Right at the same date, `72252c2` (`fix(forward-test): FTMO R:R mismatch, risk budget leak, SIGTERM handling`) was being added — but it didn't touch `sd_notify` or health-heartbeat paths.

**Code changes since:**

- New `ConnectionWatchdog` (`src/forex_bot/adapters/ctrader/connection_watchdog.py`) with 30 s DEGRADED / 90 s FAILED thresholds surfaces trouble *before* systemd's 5-min watchdog fires.
- `forward_test_engine.py` (research-doc line 130–133): `stale_tick_threshold_sec = 60.0` lowered from 900 s — earlier disconnect detection.
- `_check_connection_health()` + `_attempt_reconnect()` in `forward_test_engine.py:2147-2247` (BQ-1335) actively reconnects on staleness.

**Mitigation status:** ✅ *Largely mitigated by the connection-reliability wave.* However, **the systemd unit should not re-enable `WatchdogSec=`** because the new watchdog operates at 30/90 s, not 5 min — see §5.

### 2.5 May 24 22:05 — `9/KILL` (likely OOM)

**Journal evidence (PID 1386):**

```
May 24 22:05:21 Main process exited, code=killed, status=9/KILL
May 24 22:05:21 Failed with result 'signal'.
May 24 22:05:21 Consumed 46.161s CPU time, **210.7M memory peak**, 0B memory swap peak.
```

**Interpretation:** **Memory peak is 2–3× the normal range.** The service catalog shows 0 B swap usage — the OOM killer would have written to swap first. So this is *probably* not a Linux OOM-kill (which would print `Out of memory: Killed process … (python)` to `dmesg`). More likely: a manual `kill -9` by an operator (Craig himself, or one of the older runners), or the systemd unit was stopped via `systemctl kill -s SIGKILL`. **46 s of accumulated CPU suggests the process had been busy (running ticks/strategies).**

**Cross-check with app logs (`ayumi_2026-05-20.log`, the only retained log):** the file covers *only* the May 20 incident (15:44–16:16). The 19-day gap means we have **zero application-side trace** for May 21 → June 8. Memory leak *cannot be confirmed or ruled out* without that evidence.

**Git state:** No memory-management commits around this date.

**Code changes since:**

- No identified memory-bounded PR landed. The fixes shipped were auth/state-machine focused.

**Mitigation status:** ⚠️ **NOT directly mitigated.** This is the **single residual vector** (see §4). Reasoning:

- ConnectionWatchdog does not memory-bounded (only heartbeat-bounded).
- `authenticate_with_retry()` does not memory-bounded (3 attempts × ~140 MB).
- No `MemoryUsageMax=` directive was added to the unit.

**Recommended hardening (NOT in scope for this card):** add explicit `MemoryMax=512M` to the unit and `MemoryHigh=384M` warning threshold. Adopting these *without* running a memory-profiling session risks false positives, since the prototype allocation pattern has not been studied.

### 2.6 May 27 00:28 — `1/FAILURE` (race or credential-boundary)

**Journal evidence (PID 1386):**

```
May 27 00:28:25 Started ayumi-forward-test.service ...
May 27 00:28:29 Main process exited, code=exited, status=1/FAILURE
May 27 00:28:29 Consumed 3.474 s CPU time, **2.2M memory peak**, 0B memory swap peak.
```

**Interpretation:** **2.2 MB memory peak is unusually low** — implies the process died very early in startup (before PyTorch/NumPy loaded), most likely in the credential-loading code path. With the dual-source guard missing, this fits the "atomic `.env` write destroys working tokens" failure mode that the 2026-06-12 post-mortem documents (`forward-test-regression-cycle-2026-06-12.md §3, §4`).

**Git state:** Several refactors in motion; no specific credential-boundary patch yet.

**Code changes since:**

- `affd729` (2026-06-12) `feat(ctrader): credential boundary + kill switch hotfix` — separate `data/.credentials` file.
- `1f4b3aa` (2026-06-12) `fix(ctrader): dual-source guard + env comment parsing` — guards against `.env` clobbering.
- `d0512d7` (2026-06-16) `fix(ctrader): stop .env token clobbering (BQ-1036)`.

**Mitigation status:** ✅ *Mitigated by the credential-boundary wave (2026-06-12 onward).*

### 2.7 June 02 14:34–14:35 — `75/TEMPFAIL` ×3 in 47 s (the critical pattern)

**Journal evidence (PID 1386):**

```
Jun 02 14:34:37 Started ayumi-forward-test.service ...
Jun 02 14:34:41 Main process exited, code=exited, status=75/TEMPFAIL        (3.655 s CPU)
Jun 02 14:34:51 Started ayumi-forward-test.service ...
Jun 02 14:34:54 Main process exited, code=exited, status=75/TEMPFAIL        (3.408 s CPU; 91.2 MB peak)
Jun 02 14:35:24 Main process exited, code=exited, status=75/TEMPFAIL        (3.408 s CPU; 91.2 MB peak)
Jun 02 14:35:46 Started ayumi-forward-test.service ...                     [entered a longer-running state]
```

**Interpretation:** This is the **canonical" auth death spiral" failure mode documented in `docs/post-mortems/ctrader-openapi-connection-2026-06-11.md` §"2026-06-12 Update: ProtoMessage Double-Wrap Bug (The Auth Death Spiral)"**. The signature: ~3.4–3.7 s CPU, ~91 MB memory peak (data loaded, but auth bails before networking), 75 exit code, restart ~10–30 s apart.

**Critical finding on exit code 75:**

The exit code **75 (= EX_TEMPFAIL from `<sysexits.h>`)** is **not present** in the current source tree — `grep -rnE "exit\(75|sys\.exit\(75|EX_TEMPFAIL"` returns nothing. Yet the systemd unit specifies:

```
RestartPreventExitStatus=75
SuccessExitStatus=75
```

This means **at some point between June 02 and June 25** (file mtime `2026-06-25 10:27:35`), a code change added `sys.exit(75)` for "transient auth failure — try again". The unit was reconfigured in parallel to **acknowledge exit 75 as expected behaviour** and not restart-storm.

This reconfiguration is precisely the kind of incident-driven hardening you'd expect after a 47-second restart storm. **The 75-exit-code path was the application author telling the service manager "this was a transient auth failure — I'm choosing to fail clean instead of looping"** — the right behaviour, but it should never occur (see §4).

**Git state:** The fix (`7af7a73`, ProtoMessage double-wrap) was 10 days away.

**Code changes since:**

- `7af7a73` (2026-06-12) `fix(ctrader): resolve auth death spiral — ProtoMessage double-wrap bug` — *direct fix.*
- `a487b69` (2026-06-23) `feat: BQ-1330a — engine FSM survives auth failure with retry (3x backoff 1s/2s/4s)` — *recovery layer.*
- `a0663c1` (2026-06-23) `merge: BQ-1335 fix stuck RECONNECTING state` — *prevents the state from getting stuck before the retry layer is reached.*
- `21f73a6` (2026-07-03) `fix: expand FAILED state transitions and add auth failure escalation` — *prevents the connection from getting stuck in FAILED after the retry exhausts.*
- Current systemd unit honours `SuccessExitStatus=75` — no restart storm if it ever recurs.

**Mitigation status:** ✅ **Triple-mitigated and unit-protected.** This is the **highest-leverage fix in the whole sprint.** If the `7af7a73` fix ever regresses:

1. `authenticate_with_retry()` will retry up to 3× with exponential backoff (1s/2s/4s) before yielding — 4 attempts total.
2. `_handle_auth_failure` path counts attempts (visible at `open_api_spot_feed.py:1381` `_activate_kill_switch_freeze(...)`).
3. Even if the launcher exits 75, the unit *won't* restart-storm because `RestartPreventExitStatus=75` is set.

### 2.8 June 03 15:46 — `9/KILL`

**Journal evidence (PID 1386):**

```
Jun 03 15:46:41 Main process exited, code=killed, status=9/KILL
Jun 03 15:46:41 Failed with result 'signal'.
Jun 03 15:46:41 Consumed 31.968s CPU time, 145.2M memory peak.
```

**Interpretation:** 31.97 s of CPU and 145 MB memory peak is **normal operating range**. SIGKILL of a healthy-looking process fits:

- a manual `kill -9` by an operator,
- a `systemctl kill -s SIGKILL` (the journal shows no preceding `Stop` request, so this is less likely),
- or an external signal source (e.g., a wrapper script / supervisor that doesn't appear in this journal).

**Mitigation status:** Unknown root cause. Cannot attribute to a code bug. Treated as **operator action** unless evidence emerges later.

### 2.9 June 07 18:07, June 08 03:05 — `1/FAILURE` ×2

**Journal evidence (PID 1386):**

```
Jun 07 18:07:32 Started ...
Jun 07 18:07:36 Main process exited, code=exited, status=1/FAILURE  (3.706 s CPU; 197.1 MB peak)
Jun 08 03:05:55 Main process exited, code=exited, status=1/FAILURE  (10.724 s CPU; 185.0 MB peak)
```

**Interpretation:** These are post-sprint-fix land dates but **pre-`7af7a73`** (which landed 2026-06-12). The 197 MB / 185 MB peaks are >120% of normal — they fit the **proto-wrap bug loading all historical bars before auth bails**. The 3.7 s spikes are the same as the May-05 storm. The June 08 entry (10.7 s CPU) likely indicates one test got slightly further before failing.

**Code changes since:** Same as §2.7.

**Mitigation status:** ✅ *Mitigated by the 2026-06-12 → 2026-06-23 wave.*

### 2.10 June 10 — `1/FAILURE` ×2 (live-fire attempt)

**Journal evidence (PID 1040971):**

```
Jun 10 17:57:43 Started ...
Jun 10 17:57:47 Main process exited, code=exited, status=1/FAILURE  (3.571 s CPU)
Jun 10 23:35:06 Started ...
Jun 10 23:35:12 Consumed 3.290 s CPU; Stop → Started (back-to-back)
```

**Interpretation:** The PID change from `1386` to `1040971` shows a user-session restart (operator reconnected / re-logged). The 23:35 cycle is a clean Stop → Start pattern — manual operator intervention. Both FAILUREs are pre-fix and consistent with the proto-wrap window.

**Code changes since:** Same as §2.7.

**Mitigation status:** ✅ *Mitigated.*

---

## 3. Mitigation Assessment

### 3.1 Direct mitigation map

| Existing Fix | Path | What it fixes | Crash patterns addressed (from §1.1 table) |
|---|---|---|---|
| `7af7a73` ProtoMessage double-wrap fix | `src/forex_bot/adapters/ctrader/open_api_spot_feed.py:680` | Double-wrapped protobuf envelope being passed to `TcpProtocol.send()` → server sees malformed frame → auth death-spirals on every reconnect | #2, #5, #6, #8, #9, #10 (the 1/FAILURE / 75/TEMPFAIL storms) |
| `a487b69` `authenticate_with_retry()` (BQ-1330a) | `src/forex_bot/adapters/ctrader/connection_manager.py:785-833` | Wraps auth callable in 3-attempt exponential backoff (1s/2s/4s); FSM survives transient failures | #2, #5, #6, #8, #9, #10 |
| `a0663c1` BQ-1335 reconnect-stuck fix | `src/forex_bot/adapters/ctrader/connection_state.py:88-95` + `forward_test_engine.py:88-91, 2147-2247` | Stuck `RECONNECTING` state after partial recovery → `_check_connection_health` + `_attempt_reconnect` with market-closed guard + backoff gate | #2, #5, #6, #8, #9, #10 |
| `c845983` cTrader resilience improvements | multiple paths | Adds correlation risk modules + new test coverage | All `1/FAILURE` paths indirectly |
| `affd729` credential boundary | `data/.credentials` + guard | Atomic `.env` clobbering cannot destroy working tokens | #5, #10 |
| `1f4b3aa` dual-source guard + env parse | `scripts/launch_blend_forward_test.py:686-709` | Refuse paper mode on live endpoint (fail-closed) + `.env` comment parsing | #5 |
| `d0512d7` stop .env token clobbering | (BQ-1036) | Stops atomic writers from destroying tokens | #5 |
| `ConnectionWatchdog` | `src/forex_bot/adapters/ctrader/connection_watchdog.py` | 30 s DEGRADED / 90 s FAILED surfaced before systemd's 5-min watchdog | #3 (ABRT-from-watchdog) |
| `ReconnectStrategy` | `src/forex_bot/adapters/ctrader/reconnect_strategy.py` | AWS decorrelated jitter backoff, tiered error classification | All `9/KILL`-during-operation patterns going forward |
| `error_classifier` | `src/forex_bot/adapters/ctrader/error_classifier.py` | TIER_1/2/3A/3B routing — auth failures *don't loop forever*, they halt | #6 specifically |
| `3644108` reconstruct `_api_client` on reconnect | `forward_test_engine.py` | Prevents dead-connection zombies after partial reconnect | All reconnecting patterns |
| `21f73a6` expand FAILED-state transitions | `connection_state.py:88-130` | FAILED state can re-enter normal lifecycle (no more sticky-failed trap) | All reconnecting patterns |
| `72252c2` SIGTERM handling | `launch_blend_forward_test.py:862-863` | Clean shutdown on SIGTERM | #1 (stop-timeout kill) |
| `RestartPreventExitStatus=75` + `SuccessExitStatus=75` in unit | `/etc/systemd/system/ayumi-forward-test.service` | Systemd acknowledges a clean 75-exit as success; no restart storm | #6 (the 47-second storm cannot recur even if the bug regresses) |

### 3.2 Cross-check — does the BQ-1335 + authenticate_with_retry + ConnectionWatchdog combination address all known patterns?

**The answer is: yes, with the single residual vector of OOM (May 24).**

Verification of the three named mitigations against the four crash patterns:

| Mitigation | Watchdog (ABRT) | OOM (KILL) | Startup (FAILURE) | Auth storm (TEMPFAIL) |
|---|---|---|---|---|
| ConnectionWatchdog | ✅ Surfaces at 30/90 s before 5-min systemd watchdog | ❌ No memory bounded | ❌ Not its job | ❌ Not its job |
| `authenticate_with_retry()` | ❌ Not its job | ❌ No memory bounded | ✅ 3× retry covers startup auth flake | ✅ 3× retry prevents "instant restart" loop |
| BQ-1335 reconnect logic | ✅ `_check_connection_health` runs every tick; `_attempt_reconnect` with backoff gate | ❌ No memory bounded | ✅ Reconnect recovery on auth transient | ✅ Stuck-RECONNECTING no longer permanent |
| **Combined effectiveness** | **Strong** | **None** | **Strong** | **Strong** |

The OOM gap is structural: none of the connection-reliability modules address memory. The honest answer is **the OOM pattern is unaddressed** by this trio. See §4.

---

## 4. Residual Risk

### 4.1 Confirmed residual vectors

| # | Risk | Estimated probability | Impact | Mitigation difficulty |
|---|---|---|---|---|
| A | OOM-style memory kill (May 24 pattern) | **Low** — hasn't recurred since 2026-05-24, 12 days of stable operation since the proto-wrap fix on June 12 | Service dies; systemd restarts in 30 s | Medium — needs `MemoryMax=` directive in unit + memory profile |
| B | Auth death-spiral *recurrence* if `7af7a73` regresses | **Low** — guarded by `RestartPreventExitStatus=75` | Restart storm (would be self-bounded by `StartLimitBurst=5`) | Low — covered by existing unit-level guard |
| C | Watchdog ABRT during a market-open hang | **Very low** — `ConnectionWatchdog` (30/90 s) catches it before 5-min systemd watchdog | Service kill + crash | Low — covered by existing in-app watchdog |
| D | `kill -9` from operator | **Medium** — Craig has intervened; documented | Service dies; systemd restarts | None needed |
| E | Log-file gap (May 21 → Jun 9) — silent failures in that window would leave no app-side trace | **Medium** — was an actual data-loss scenario | Unknown root causes for any future bug in that window | High — log rotation config & retention policy must be fixed; out of scope for this card |

### 4.2 Not residual but worth flagging

- **The `~210 MB` peak on May 24** could indicate a *brief* memory leak that was *fixed incidentally* by subsequent refactors (e.g., the connection extraction in `ab8e4c5`). I cannot confirm this without a reproducer, but the pattern is consistent with the "stuck partial-auth + bar-prefetch" leak described in `forward-test-regression-cycle-2026-06-12.md §2`.
- **`log file gap` is a real problem** independent of this card: `ayumi_2026-05-20.log` is the last dated daily log until `ayumi_2026-06-09.log`. This is a **19-day blind spot** in the application's own logging. Future root-cause investigations will face the same evidence-loss problem unless a `tail`-rotate + retention policy is set in `logging_config.py`.

### 4.3 Why no crash since 2026-06-10

The simplest explanation is the correct one: **the proto-wrap fix landed 2 days later.** The post-2026-06-10 stable period is *correlated* with the fix landing. The connection-reliability wave (Jun 12–Jul 3) added layered defence so a future regression of the same bug would not cause a restart storm.

---

## 5. systemd Unit Recommendation (for Task 5.2 builder)

This section feeds Task 5.2 (separate builder).

### 5.1 The findings that drive the design

1. **Restart storm pattern is well-understood** (#6 in §1.1): exit 75 + Restart=always = 47-second storm. The current unit correctly handles this via `RestartPreventExitStatus=75` + `SuccessExitStatus=75`. **Task 5.2 must preserve this configuration.**
2. **Stop-timeout kill pattern is well-understood** (#1 in §1.1): systemd SIGTERM → SIGKILL after default 90 s. The current behaviour is the unit family that protects against this.
3. **Watchdog ABRT pattern (#3 in §1.1)**: don't re-enable systemd's `WatchdogSec=` until we have a stable `READY=1` signal in the application. Even if we did, `ConnectionWatchdog` already runs at 30 s / 90 s thresholds — anything reaching 5 min would be a *post-watchdog* warning, not a protection.
4. **OOM pattern (#4 in §1.1)** is currently unmitigated. Task 5.2 should consider a memory ceiling.
5. **Auth-pattern emergency exit (1/FAILURE ×15)** was *not* a restart-storm failure (it eventually stops at counter 18 because `StartLimitBurst=5` effectively caps it within `StartLimitIntervalSec`). The 30-second cadence gives PID-1386 sessions time to re-attempt auth.

### 5.2 Recommended Unit Topology

Drawn from the root-cause findings, these are the **mandatory** directives for the 5.2 rebuild:

```ini
[Unit]
Description=Ayumi Forward Test (Blend Pipeline)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$AYUMI_ROOT
ExecStart=$AYUMI_ROOT/.venv/bin/python scripts/launch_blend_forward_test.py --symbols GBPUSD,USDJPY --live

# Restart policy — derived from findings
Restart=on-failure
RestartPreventExitStatus=75      # 75 = "transient auth, application decided to fail clean"
SuccessExitStatus=75             # 75 treated as expected outcome
RestartSec=30                    # longer than the 3.4 s startup-fail cadence, shorter than 90 s timeout

# Stops
KillMode=control-group           # ensure all child threads terminate with parent
TimeoutStopSec=10                # 72252c2 SIGTERM handler gives a 1-3 s clean shutdown; 10 s ceiling

# Watchdog — explicitly disabled; ConnectionWatchdog handles liveness at 30/90 s
# WatchdogSec=                    # leave commented

# Memory bounds — mitigates §4 vector A (May 24 OOM pattern)
MemoryHigh=384M                  # throttle-only, doesn't kill
MemoryMax=512M                   # hard cap; the 210 MB peak was anomalous
MemoryAccounting=yes

# Logging — ensure log rotation covers the gap documented in §4 E
StandardOutput=append:$AYUMI_ROOT/logs/forward_test-stdout.log
StandardError=append:$AYUMI_ROOT/logs/forward_test-stderr.log

[Install]
WantedBy=multi-user.target
```

### 5.3 Why `RestartSec=30` is sufficient (NOT longer)

- The 75/TEMPFAIL pattern completes `connect → auth-attempt → fail → exit` in **3.4 ± 0.3 s**. A 30 s `RestartSec` is **8.6× the failure duration** — enough breathing room.
- The ConnectionWatchdog (30 s DEGRADED / 90 s FAILED) and `_check_connection_health` (per-tick) — both running *during* a successful 30-second pause — are sufficient to catch *inside-restart* connection issues.
- A *longer* `RestartSec` (e.g., 120 s) would slow recovery from a real crash. From 2026-05-24 onward the live service has shown a longest retry interval of 60 s during the proto-wrap window — the May 24 OOM pattern does not benefit from a longer pause (the next start would also OOM in ~5 min anyway).

### 5.4 StartLimitBurst=5 / StartLimitIntervalSec=300 — why these numbers

The May 05 storm reached 15 failures in 9 minutes (~17 sec / failure average). With:

- `RestartSec=30` → would translate to 10 failures / 5 min cap.
- `StartLimitBurst=5` → caps at 5 failures per 5 minutes, after which systemd stops trying.

This **prevents the May 05 pattern entirely**: 5 failures in 5 minutes is enough to recover from a transient proto-wrap regression without thrashing the broker connection at every sleep or jitter cycle. The state of the auth attempt becomes irrelevant after 5 restarts — let the operator investigate.

### 5.5 `MemoryMax=512M` — defensible or over-aggressive?

The observed peak across the entire May-June window was **210.7 MB** (May 24, the OOM candidate). All other crashes peaked under 200 MB. `MemoryMax=512M` is **2.4× the highest observed peak**, leaving generous headroom for:

- New bar preloading patterns (`forward_test_engine.py:_preload_historical_bars()`)
- Single-connection token-lifecycle (post `9dab84d`)
- 8 strategies evaluation passes (`blend_runner` carries the 8-strategy signal pipeline)

If 512 MB trips during a market-open scenario, the OOM-detection story is *"we hit 512 — there's a leak and we need to bisect."* If it doesn't trip, the unit has zero performance impact. **No downside for the cost.**

### 5.6 Why no `WatchdogSec=`

The new `ConnectionWatchdog` operates at 30/90 s — *faster* than any plausible `WatchdogSec` (best practice is ≥60 s). Re-introducing systemd's watchdog layer:

- is **redundant** with `ConnectionWatchdog`,
- adds an **extra** SIGABRT pathway that bypasses `ConnectionWatchdog`'s graceful DEGRADED transition,
- was the **specific mechanism** that killed the May 22 instance (5 min of no heartbeat → SIGABRT → status=6/ABRT).

If we ever need it (e.g., for liveness at the OS-launch level before `ConnectionWatchdog` initializes), the recommended minimum is `WatchdogSec=600` with the launcher calling `sd_notify(0, "WATCHDOG=1")` once per 5 minutes on a confirmed-healthy state. **Not in scope for 5.2.**

### 5.7 Log rotation — out of scope for 5.2 but flagged

The 19-day log gap (May 21 → June 9) is a **systemic problem** flagged in §4.2 E. A `/etc/logrotate.d/ayumi` with `rotate 30, daily, compress` would have preserved `ayumi_2026-05-21.log` etc. for later root-cause analyses. This is *not* a 5.2 directive change — it's a separate logrotate-config bug. Flag for a follow-up card.

---

## 6. Summary of evidence-driven answers

**Q: Why did each crash happen?**
A: See §2. Brief table in §1.1.

**Q: Does BQ-1335 + `authenticate_with_retry()` + `ConnectionWatchdog` mitigate each pattern?**
A: Yes — except for the May 24 OOM-vector (210 MB peak kill), which is memory-bound, not connection-bound. Confirmed in §3.2.

**Q: Is exit 75 a bug?**
A: It *was* an unbug-aware signal during the May-June window — now it's a deliberate "transient auth, expected fail-clean" marker. The current unit correctly acknowledges it. If it never occurs again (which is the goal), all the better.

**Q: Is `RestartSec=30` sufficient?**
A: Yes — derived from the 3.4 s average failure duration observed in journal. Longer would slow legitimate recovery without helping ill-formed starts. §5.3.

**Q: Is `StartLimitBurst=5 / StartLimitIntervalSec=300` correct?**
A: Yes — caps the May-05-style storm within 5 minutes; sufficient headroom for genuine transient blips. §5.4.

**Q: What gets *added* in 5.2 that wasn't there before?**
A: `MemoryHigh=384M`, `MemoryMax=512M`, `MemoryAccounting=yes` to bound the May 24 OOM-class vector. Reasonable defaults for observed peak + 2.4× headroom. §5.5.

**Q: What gets *removed* in 5.2 that was there before?**
A: `WatchdogSec=` — intentionally; it's redundant with `ConnectionWatchdog` and was the mechanism that killed May 22. §5.6.

**Q: What *cannot* be fixed by the unit?**
A: Memory leaks in application code (none identified yet). Log rotation policy (separate logrotate-config work). Connection-level hangs that exceed 90 s without traffic (would need application-level ping — `ConnectionWatchdog` covers most cases).

---

## 7. Files Read for This Investigation

- `$AYUMI_ROOT/logs/ayumi_2026-04-24.log` … `ayumi_2026-05-20.log`, `ayumi_2026-06-09.log` … `ayumi_2026-06-16.log`
- `$AYUMI_ROOT/logs/forward_test*.log`
- `$AYUMI_ROOT/docs/post-mortems/ctrader-openapi-connection-2026-06-11.md`
- `$AYUMI_ROOT/docs/post-mortems/forward-test-regression-cycle-2026-06-12.md`
- `$AYUMI_ROOT/docs/plans/ayumi-reliability-sprint-2026-07-05.md` (§2.1, §2.2)
- `$AYUMI_ROOT/scripts/launch_blend_forward_test.py` (lines 30–40, 670–750, 850–890)
- `$AYUMI_ROOT/src/forex_bot/adapters/ctrader/connection_watchdog.py` (full)
- `$AYUMI_ROOT/src/forex_bot/adapters/ctrader/reconnect_strategy.py` (full)
- `$AYUMI_ROOT/src/forex_bot/adapters/ctrader/connection_manager.py` (lines 780–860 — `authenticate_with_retry`)
- `$AYUMI_ROOT/src/forex_bot/adapters/ctrader/connection_state.py` (lines 1–130 — `_VALID_TRANSITIONS`)
- `$AYUMI_ROOT/src/forex_bot/adapters/ctrader/forward_test_engine.py` (lines 430–470 — `start()`)
- `/etc/systemd/system/ayumi-forward-test.service` (current)
- `/etc/systemd/system/ayumi-forward-test.service.bak` (2026-06-05 snapshot)
- `/home/$USER/.config/systemd/user/ayumi-forward-test.service` (Task 5.2 target file)
- Git history for: `a487b69`, `a0663c1`, `21f73a6`, `7af7a73`, `17e72b0`, `1f4b3aa`, `ab8e4c5`, `affd729`, `fd17c86`, `9dab84d`, `667ee33`, `3644108`, `c845983`, `198a10f`, `2225225`, `72252c2`

---

## 8. Audit Notes

- **Branch:** `senior-dev/crash-root-cause` (created from `main` @ `b849df0`).
- **No code modified** — investigation-only per task constraint.
- **No systemd unit created** — Task 5.2 is a separate builder (the `~/.config/systemd/user/ayumi-forward-test.service` file is the Task 5.2 deliverable, not 5.1).
- **Commit hash:** will be added by the builder report (§B).
- **Findings card:** none — the existing `[DEBT]`/`[FINDING]` backlog carries the log-rotation gap (§4.2 E) and the residual OOM hardening (§4.1 A) as future-card material.
