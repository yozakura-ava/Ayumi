# Deprecated Orphaned Features — 2026-07-17

## Context

During the ayumi-cleanup sprint (Jul 2026), 6 test files were deleted because they tested modules that do not exist in `src/`. This document records the disposition of each orphaned feature area.

## Classification Summary

| Feature Area | Status | Rationale |
|---|---|---|
| Crypto copy-trading | NOT_WANTED | No Python package exists. Only artifact is orphaned `data/crypto/copy_trading.db`. |
| Engine v2 / Prop Firm Rules | NOT_WANTED | Never implemented. Only references in archived docs/plans. |
| Transport Cooldown Gate | NOT_WANTED | Zero references in source or docs. Never implemented. |

## Detail

### 1. Crypto Copy-Trading

- **Tested modules:** `crypto.models.trade`, `crypto.services.repository`
- **Source status:** No `src/forex_bot/crypto/` package exists
- **Data artifacts:** `data/crypto/copy_trading.db` (110 KB SQLite, last modified Apr 8 2026)
- **Existing crypto references:** Other crypto references in codebase are for BTC regime overlays and instrument metadata (pip values, symbol gating) — unrelated to copy-trading
- **Decision:** NOT_WANTED. The copy-trading concept was never integrated. The `copy_trading.db` is orphaned data and can be safely removed in a future cleanup.

### 2. Engine v2 / Prop Firm Rules

- **Tested modules:** `engine_v2`, `prop_firm_rules`
- **Source status:** No source files found
- **References:** Only in `docs/_archive/plans/p5a-test-mod-plan.md` and `docs/p5a/test-move-manifest.tsv` (archived planning docs)
- **Decision:** NOT_WANTED. These were planning-stage concepts that were never implemented. Archived docs remain for historical reference.

### 3. Transport Cooldown Gate

- **Tested module:** `_TRANSPORT_COOLDOWN_SEC`
- **Source status:** Zero references in any source file
- **Docs status:** Zero references in any doc file
- **Decision:** NOT_WANTED. This feature was never implemented at any level. No traces remain beyond the deleted tests.

## Actions

- All 6 deleted test files confirmed as correctly removed
- No implementation work needed
- `data/crypto/copy_trading.db` flagged for removal in future cleanup (separate card)
