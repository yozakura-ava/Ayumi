# Legacy cTrader Modules — Archived

**Date:** 2026-06-16  
**Reason:** Replaced by new infrastructure modules in BQ-1043 cTrader Infrastructure Rebuild.

## Archived Files (top-level)

| Legacy File | Replaced By |
|-------------|-------------|
| `auth.py` | `session.py` |
| `credentials.py` | `credential_store.py` |
| `oauth_refresh.py` | `token_lifecycle.py` |
| `open_api_spot_feed.py` | `market_data_feed.py` |
| `token_manager.py` | `token_lifecycle.py` |

## Snapshot Package (`_pkg/`)

A frozen copy of these files plus all modules they reference via relative imports. Used by the compatibility shims in `src/forex_bot/adapters/ctrader/` so the legacy launcher and its tests continue to work.

Do not import from `_pkg` directly in new code. The modern equivalents are in `src/forex_bot/adapters/ctrader/`.

## Compatibility Shims

Five shim files in `src/forex_bot/adapters/ctrader/` re-export from `archive.legacy_ctrader._pkg`:
- `auth.py` → `archive.legacy_ctrader._pkg.auth`
- `credentials.py` → `archive.legacy_ctrader._pkg.credentials`
- `oauth_refresh.py` → `archive.legacy_ctrader._pkg.oauth_refresh`
- `open_api_spot_feed.py` → `archive.legacy_ctrader._pkg.open_api_spot_feed`
- `token_manager.py` → `archive.legacy_ctrader._pkg.token_manager`

Remove the shims once the v2 launcher is confirmed stable and the legacy launcher (`launch_blend_forward_test.py`) is retired.

## Verification

- 3504 tests collected, 0 collection errors after archive + shim.
- `archive/` itself is a Python package (`__init__.py` present) so shims can import from it.

## Notes

- Hyphens in the original task spec (`legacy-ctrader`) were changed to underscores (`legacy_ctrader`) because Python module names cannot contain hyphens.
