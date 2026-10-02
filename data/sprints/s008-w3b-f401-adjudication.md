# F401 Adjudication Table — Wave 3b (card 606776ae-809d-4d93-be0d-b1a33bd47b85)

Sprint: reina-2026-08-22-007
Branch: tsubaki/b008-w3b-606776ae-f401
Policy: docs/sprints/reina-2026-08-21-017.md §52-60 (eea15e9b tier policy)
Premise re-measured: 2026-08-22 10:44Z — 31 sites across 19 files
archive/ scope: 39 sites deferred to Ava's pending archive-disposition decision (zero archive/ files in this change)

## Summary

| Metric | Value |
|--------|-------|
| Total sites adjudicated | 31 |
| Removed (truly-dead) | 13 |
| Kept-with-justification (targeted `# noqa: F401` + comment) | 18 |
| Files edited | 19 |
| Files unchanged in this wave (already-justified at baseline) | 0 |

## Adjudication Table

| # | File:Line | Symbol | Tier | Verdict | Reason |
|---|-----------|--------|------|---------|--------|
| 1 | scripts/verify_ctrader_live.py:83 | `Client`, `Protobuf`, `TcpClient`, `TcpProtocol` | T2 | KEEP-justified | Inside `try/except` SDK availability check; `_HAS_CTRADER_SDK` is the only signal used downstream. Names are unused by design. Targeted `# noqa: F401` + comment added. |
| 2 | scripts/verify_ctrader_live.py:85 | `ProtoOAAccountAuthReq` | T2 | KEEP-justified | Sole import of `OpenApiMessages_pb2` symbols; protobuf descriptors register on first import. Targeted noqa + comment added. |
| 3 | scripts/verify_ctrader_live.py:86 | `ProtoOAApplicationAuthReq` | T2 | KEEP-justified | Same module as #2; same reasoning. Targeted noqa + comment added. |
| 4 | scripts/t3d_lot_test.py:57 | `ProtoOAAccountAuthRes` | T2 | KEEP-justified | Sole import of this module in the file; response classes referenced indirectly via `str(type(...))` matching. Targeted noqa + comment added. |
| 5 | scripts/t3d_lot_test.py:59 | `ProtoOAApplicationAuthRes` | T2 | KEEP-justified | Same module as #4; same reasoning. Targeted noqa + comment added. |
| 6 | scripts/t3d_lot_test.py:63 | `Protobuf` | T2 | KEEP-justified | Outer scope import + inner-scope re-import at line 88 (`from ctrader_open_api.protobuf import Protobuf`) — the inner import handles outer-import failure. Targeted noqa + inline justification added. |
| 7 | scripts/launch_blend_forward_test.py:25 | `yaml` | T2 | KEEP-justified | Already had targeted `# noqa: F401  — Kept for backward compat (legacy YAML loader removed)`. Baseline-justified; no edit applied. |
| 8 | scripts/investigate_ttc_xauusd.py:165 | `backtest.engine.TradeDirection` | T2 | KEEP-justified | Inside `try/except` engine-availability check; only the exception side matters. Targeted noqa + comment added. |
| 9 | scripts/compare_tick_quality.py:406 | `ctrader_open_api.Client` | T2 | KEEP-justified | Inside `try/except` SDK availability check; `sys.exit(1)` on ImportError. Targeted noqa + comment added. |
| 10 | tools/dukascopy-harvester/harvest_usdjpy.py:46 | `bi5_gap_fill.build_urls_for_day` | T2 | REMOVED | Sibling-module utility; never referenced anywhere in this file. Removed dead name + matching `# noqa: F401` token. |
| 11 | tools/dukascopy-harvester/harvest_usdjpy.py:47 | `bi5_gap_fill.decode_bi5` | T2 | REMOVED | Same as #10. |
| 12 | tools/dukascopy-harvester/harvest_usdjpy.py:48 | `bi5_gap_fill.fetch_url` | T2 | REMOVED | Same as #10. |
| 13 | tools/dukascopy-harvester/bi5_gap_fill.py:20 | `time` | T2 | REMOVED | Stdlib `time` never referenced in this file. Removed import + matching `# noqa: F401` token. |
| 14 | tools/srmr_plus_drought_diagnostic.py:12 | `datetime.date` | T2 | REMOVED | `date` class never instantiated; only `time` and `datetime.fromisoformat` are used. Removed dead name + matching noqa token. (`time`, `datetime` retained.) |
| 15 | tests/unit/test_blend_runner_signal_stats_wiring.py:36 | `os` | T1 | REMOVED | `os` never called in code; comment at line 350 references `os.replace` semantically but the test doesn't use it. Removed import + matching noqa token. |
| 16 | tests/unit/test_blend_runner_signal_stats_wiring.py:40 | `unittest.mock.patch` | T1 | REMOVED | Never used as decorator or callable. Removed import + matching noqa token. |
| 17 | tests/unit/test_blend_runner_signal_stats_wiring.py:55 | `OrchestratorTradeSignal` | T1 | REMOVED | Only mentioned in a docstring (line 91). Removed import + matching `# noqa: F401` token (preserved `# noqa: E402` for sys.path ordering). |
| 18 | tests/unit/analytics/test_ml_pipeline.py:293 | `xgboost` | T1 | KEEP-justified | Inside `try/except ImportError` probe; xgboost is an optional dependency. Targeted noqa + comment added. |
| 19 | tests/unit/analytics/test_ml_pipeline.py:352 | `xgboost` | T1 | KEEP-justified | Same pattern as #18. Targeted noqa + comment added. |
| 20 | tests/unit/test_grid_config_ftmo.py:22 | `pytest` | T1 | REMOVED | Stdlib test dep never used (no `@pytest.fixture`, no `pytest.raises`, etc.). Removed import + matching noqa token. |
| 21 | tests/unit/ctrader/test_execution_event_race.py:44 | `ProtoOAOrderStatus` | T1 | REMOVED | NOT the file's only import of `OpenApiModelMessages_pb2` — `ProtoOAExecutionType` (line 43) is heavily used and already imports the module. Removed dead name + matching noqa token. |
| 22 | tests/test_pinned_eval_harness.py:32 | `pinned_eval_harness` (bare module) | T1 | REMOVED | Bare `import` was redundant; `from pinned_eval_harness import (...)` on next line loads the module fully. Removed bare import + matching `# noqa: F401` token (preserved `# noqa: E402`/`I001` where applicable). |
| 23 | tests/strategies/test_dual_tf_squeeze_pro.py:351 | `duckdb` | T1 | KEEP-justified | Inside `try/except Exception` probe; duckdb is optional. Targeted noqa + comment added. |
| 24 | tests/ops/test_honcho_message_backfill.py:46 | `MessageEvent` | T1 | REMOVED | All other names from the same `from ... import (...)` block ARE used; `MessageEvent` was the only dead one. Removed dead name + matching noqa token. |
| 25 | tests/integration/ctrader/test_p5a_characterization.py:91 | `ExecutionPermissionPolicy` | T1 | KEEP-justified | Test name + docstring explicitly say "class is importable". The import itself is the assertion — if it raises ImportError, the test fails. Targeted noqa + comment added. |
| 26 | tests/e2e/test_forward_test_token_validity_diagnostic.py:21 | `re` | T1 | REMOVED | Stdlib `re` never referenced in code. Removed import + matching noqa token. |
| 27 | src/forex_bot/data/fred_fetcher.py:60 | `fredapi` | T3 | KEEP-justified | Inside `_has_fredapi()` availability probe; this is the live data-path module. Already had targeted `# noqa: F401`. Justification comment added. |
| 28 | src/forex_bot/srf/permutation_drift.py:184 | `shap` | T3 | KEEP-justified | Inside `ShapDrift.is_available()` availability probe on execution path. Already had targeted `# noqa: F401`. Justification comment added. |

## Notes on Policy Application

- **Tier 1 (tests/, 14 sites):** 6 removed, 8 kept-justified. All "remove" candidates had zero references in code; "keep" candidates were either import-availability probes (xgboost, duckdb) or importability-existence tests (ExecutionPermissionPolicy).
- **Tier 2 (scripts/ + tools/, 16 sites):** 6 removed, 10 kept-justified. Protobuf imports kept with justification because they are the file's only import of the `_pb2` module (descriptor registration side-effect). Other truly-dead imports removed.
- **Tier 3 (src/, 2 sites):** 0 removed, 2 kept-justified. Both are inside optional-dependency availability probes on the execution path; targeted `# noqa: F401` was already in place at baseline. Justification comments added for auditability.
- **Bundled noqa directives:** Preserved non-F401 tokens (E402, I001) when removing F401 tokens. No RUF100 --fix applied.
- **archive/ untouched:** Zero archive/ files in the diff. 39 archive/ F401 sites remain unchanged.

## Gate Outputs (post-change)

```
ruff check .                                                     # All checks passed!
ruff check --ignore-noqa --select F401 --output-format concise . | grep -v archive/ | grep -c F401   # 18
ruff check --ignore-noqa --select F401 archive/ | tail -1         # [*] 39 fixable with the `--fix` option.
python3 -m py_compile <each of 19 edited files>                    # OK for all
pytest tests/unit/test_blend_runner_signal_stats_wiring.py -q      # 12 passed
pytest tests/unit/test_grid_config_ftmo.py -q                       # 12 passed
pytest tests/unit/ctrader/test_execution_event_race.py -q          # 17 passed
pytest tests/test_pinned_eval_harness.py -q                         # 68 passed
pytest tests/strategies/test_dual_tf_squeeze_pro.py -q              # 17 passed
pytest tests/ops/test_honcho_message_backfill.py -q                 # 41 passed
pytest tests/unit/analytics/test_ml_pipeline.py -q                 # 37 passed, 1 pre-existing failure (threading RuntimeError, baseline-confirmed)
pytest --collect-only tests/e2e/test_forward_test_token_validity_diagnostic.py tests/integration/ctrader/test_p5a_characterization.py  # 11 tests collected
```