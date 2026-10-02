# Counterfactual Regime-Gate Observability Log (card d2be30f4)

## What

Per-bar JSONL observability for the live launcher's regime gate.
Enabled by setting `AYUMI_COUNTERFACTUAL_LOG=1` in the launcher
environment; default off. The flag is **additive observability only** —
the regime gate's behavior is unchanged whether the flag is on or off.

Path: `data/counterfactual_gate_log.jsonl` (project root).

## Why

`hb177` showed 80/81 strategy evaluations = no_signal vs 1/81 =
regime-gate rejection. Without separation between "strategy never fires"
and "gate blocks fires", Phase 1.3's gate-loosening experiment cannot
attribute changes in emit frequency to the right cause.

This log lets operators count both sides:

- `strategy_emitted=false` rows = the strategy itself didn't fire.
- `strategy_emitted=true && current_gate="reject"` rows = the gate
  blocked a real signal.
- `strategy_emitted=true && current_gate="pass"` rows = the signal
  reached downstream.

## Schema

One JSON object per line. Fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `ts` | string (ISO-8601 UTC) | Wall-clock time the row was written. |
| `symbol` | string | Trading symbol (e.g. `GBPUSD`). |
| `strategy_id` | string | Canonical strategy id (lower-snake-case). |
| `regime` | string | Detected regime: `quiet`, `choppy`, `trending`, `volatile`, or `unknown` (insufficient bars / detector failure). |
| `session` | string | Bar's UTC session: `asia`, `london`, `ny_am`, `other`, or `unknown`. |
| `adx` | float \| null | ADX(14) on the last bar, or `null` if the indicator returned nothing. |
| `current_gate` | `"pass"` \| `"reject"` | The actual regime gate's decision for this evaluation. For no-signal evaluations, always `"reject"` (no order = no submission). |
| `expanded_gate_would` | `"pass"` \| `"reject"` | What the **expanded** gate would have decided: regime ∈ `{QUIET, CHOPPY, TRENDING}` AND session ∈ `{london, ny_am}`. |
| `strategy_emitted` | bool | Whether the strategy code actually produced a signal on this bar. |

## Toggle

```bash
# Off (default)
unset AYUMI_COUNTERFACTUAL_LOG

# On
export AYUMI_COUNTERFACTUAL_LOG=1
```

Only the literal string `1` enables the log. Any other value — empty,
`0`, `true`, `yes`, `1 `, ` 1` — leaves the flag off. The check is
re-evaluated on every per-bar write, so the operator can flip the flag
at runtime and the next bar picks up the new value.

## Safety

- Default off; no I/O when off.
- All `OSError` paths (open failure, full disk, permission denied,
  unwritable parent dir) are caught and logged **once**, then go
  silent — a flaky disk will not flood the operator logs.
- The flag does not touch the live-submit path. Regime gate decisions
  and downstream correlation gate / blend runner / broker submission
  are unchanged regardless of flag value.

## Out of scope (F-1, follow-up card)

There is **no** environment override for the regime gate's own allowed
regime / session maps. Those live as dict literals at
`scripts/launch_blend_forward_test.py` around the `RegimeGate` class.
The override lands on a separate card before Phase 1.3.