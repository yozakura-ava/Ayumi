# R&D: Late-fill → SL attachment gap & TP ratchet miss (thin-liquidity XAUUSD)

**Card:** dfb095b1-e835-4ae8-81f9-0e665c34ebc9 (Reina, 2026-09-17 UTC)
**Repo:** $AYUMI_ROOT — read-only research; no code changes.

## Evidence base and a hard limitation

The 2026-08-20 incident log (execType sequencing for signal
`2b312ced57b9496d87bec4c6933f6986`, position 284288453) is **no longer on disk**.
`logs/forward_test.log` has been rotated; `logs/archive/` retains only a
2026-09-15 archive batch (5 gz files, zero matches for `LATE_FILL` or the
signal id). `data/forward_test.log` (mtime Jun 26) predates the incident.
Therefore the execType ordering for the incident itself is answered from
**source code** (authoritative for current behavior) plus the card's quoted
log sequence, not from re-grepped logs.

Verified this pass:
- `grep -c 'LATE_FILL' logs/forward_test.log` → **0** (current log, ~Sep 13–17 window)
- `grep -nE '2b312ced|LATE_FILL' logs/forward_test.log` → no matches
- `signal_stats.jsonl` has exactly 1 row for the signal: `outcome=open`,
  `lots=0.33`, pips=None — never reconciled to closed.
- Closed-outcome rows (sl_hit/tp_hit) in Aug 13–27 window: **0 of 7 rows**.
- No closed trades and zero LATE_FILL events in the most recent 7d window
  (Sep 10–17) — the current PID is flat (B5 Health: trades=0 live_fills=0).

## AC1 — Late-fill trades with realized loss > 1.5× configured risk (last 7d)

**0 observed.** Zero late-fill events and zero closed trades in the recent
7-day window; in the Aug 13–27 incident window signal_stats recorded no
closed outcomes at all. The single documented case (position 284288453,
−$67.15 vs $25 risk = 2.7×) is the only known instance. The incident is
**not** a recurring loss pattern in the observable data — it is a
single-occurrence structural exposure.

## AC2 — Is SL attached before or after execType=3 (filled) on LATE_FILL?

**After.** Source: `src/forex_bot/adapters/ctrader/forward_test_engine.py`
~2240–2360 (late-fill callback path). The callback fires on
`rv_status == FILLED` (i.e. after the fill/execType=3 equivalent), then
calls `amend_sl_tp` with bounded retry (3 attempts, 0.2–0.6s backoff).
So on the late-fill path the position is **naked for the
fill→amend-ack window** (~1s in the incident, up to seconds under retry).
Contrast: the synchronous path attaches SL/TP **inline on the MARKET order**
(engine.py:1638–1673, verified against cTrader demo 2026-07-06), so the
normal path is atomic with broker acceptance. The gap is exclusive to
LATE_FILL.

## AC3 — TP2/TP3 storage: late-fill-only or all trades?

**Structural to the late-fill path.** `OrderManager.update_position_tp_levels`
(order_manager.py:458–530) resolves the target Position by fuzzy match:
direct key hit, else numeric positionId → scan `_orders[].position_id`
(set by the **`on_filled` callback**), else `POS_{order_id}`. On the
late-fill path the order never passes through OrderManager's `on_filled`
(the fill arrived via the spot-feed callback after TIMEOUT), so the
Position cannot be located → returns False → the recurring
"TP2/TP3 not stored on Position … TP ratcheting will not activate"
warning (engine.py:2325 F2 branch). Normal inline-fill trades resolve via
the same order bookkeeping and store TP2/TP3 successfully. Broker TP1 still
protects the position; only ratcheting is lost.

**Side-finding (confirms card context):** signal_stats.jsonl records
`lots=0.33` for the incident signal while the live log recorded 0.16 —
signal_stats appears to log the *pre-adaptation* sizer output. Layer
discrepancy in position-size recording remains unexplained and unowned.

## AC4 — Recommendations (do NOT implement here)

| # | Recommendation | Rationale |
|---|---|---|
| 1 | **Orchestrator short-circuit: reject/defer new orders while `spot_feed state=degraded`** | Highest value. Removes the late-fill entry condition entirely; risk budget stays honored at decision time rather than being repaired post-fill. |
| 2 | Fix TP2/TP3 storage on late fills: register the late-filled Position in OrderManager (or key `_positions` by broker positionId) when the callback resolves `ctrader_position_id` | Closes the F2 warning class structurally, restores ratcheting for late fills. |
| 3 | Stop-market variant on thin-liquidity sessions: **deprioritize** | Does not close the pre-SL window (SL itself rides the same late amend path); mitigation value < option 1. |
| 4 | Log retention: forward_test.log rotation loses incident evidence in <30d; extend retention or archive LATE_FILL/late-SL lines to a durable jsonl | This research was blocked from primary evidence by rotation. |
| 5 | Reconcile signal_stats close-outs (incident row still `outcome=open`) and the lots 0.33 vs 0.16 layer discrepancy | Reporting integrity; file as separate card if pursued. |

Any implementation → new `[BUILD]` card. Not done here.

## Verification commands (re-runnable)

```
grep -c 'LATE_FILL' logs/forward_test.log            # 0
grep '2b312ced' data/signal_stats.jsonl | jq -r .outcome  # open
python3 - <<'E'
import json
rows=[json.loads(l) for l in open('data/signal_stats.jsonl')]
w=[r for r in rows if r['timestamp'][:10] between aug13-27]  # 7 rows, 0 closed
E
sed -n '2240,2360p' src/forex_bot/adapters/ctrader/forward_test_engine.py  # late-fill amend-after-fill
sed -n '458,530p' src/forex_bot/adapters/ctrader/order_manager.py          # fuzzy match fails w/o on_filled
```

— Reina, sprint heartbeat 2026-09-17 04:0x UTC. DELEG-REF: dfb095b1-e835-4ae8-81f9-0e665c34ebc9
