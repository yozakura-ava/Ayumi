# BQ-681: Token TTL Refresh Manager

**SP:** 2 | **Priority:** High | **Status:** Planning

## Problem

cTrader OpenAPI access tokens expire ~30 days (2,628,000s). The forward test currently has **inline** token refresh logic embedded in `OpenApiSpotFeed`, but no standalone **TokenManager** component. This creates several issues:

1. **No startup validation** — the launcher (`launch_blend_forward_test.py`) has no pre-flight check for token freshness; a stale token isn't caught until `_auth()` fails deep in the feed.
2. **No warning system** — there's no N-day-before-expiry alert; tokens expire silently.
3. **No disk-level state tracking** — `_persist_tokens()` rewrites `.env` but there's no separate state file tracking when tokens were issued, their TTL, or last refresh timestamp.
4. **Refresh logic is coupled to the feed** — `_refresh_token_and_reauth()` lives inside `OpenApiSpotFeed`, making it hard to reuse or test independently.

## Current State (What Already Exists)

### Token Refresh: Already Implemented ✅

The inline implementation in `open_api_spot_feed.py` already handles:

| Feature | Implementation |
|---------|---------------|
| **Programmatic refresh** | `POST https://openapi.ctrader.com/apps/token` with `grant_type=refresh_token` |
| **Proactive refresh** | `_schedule_proactive_refresh()` at 80% of token lifetime |
| **Reactive refresh** | `_handle_error()` detects `CH_OAUTH_TOKEN_EXPIRED`, triggers `_refresh_token_and_reauth()` |
| **Token persistence** | `_persist_tokens()` rewrites `.env` with new `access_token` + `refresh_token` |
| **Circuit breaker** | 5 consecutive failures → circuit open, kill switch FREEZE |
| **Expiry tracking** | `_token_expires_at` set from `ProtoOAAccountAuthRes.expiresIn` or defaults to 24h |
| **Backoff** | Exponential backoff on reactive refresh failures |

### Key Finding: cTrader **does** support programmatic refresh

The OAuth flow at `https://openapi.ctrader.com/apps/token` accepts:
- `grant_type=refresh_token`
- `refresh_token` (from initial authorization)
- `client_id` + `client_secret`

Returns new `accessToken` + `refreshToken` + `expiresIn`. **No browser interaction required.**

This means the refresh is already fully automated. The gap is in **observability, startup validation, and decoupling**.

## What's Missing (The Actual Gaps)

### Gap 1: No Startup Token Validation
`launch_blend_forward_test.py` loads `.env` and passes tokens directly to the engine. There's no check for:
- Is the token expired or about to expire?
- When was it last refreshed?
- Is the `.env` value a placeholder (`new-access`, `new-refresh`)?

### Gap 2: No Token State File
There's no persistent record of:
- Token issue time
- Token TTL / expected expiry
- Last successful refresh timestamp
- Refresh history (for diagnostics)

### Gap 3: No Pre-Expiry Warning / Alerting
The proactive refresh fires at 80% TTL silently. If it fails, the only signal is the circuit breaker (5 errors → FREEZE). There's no configurable "warn N days before expiry" notification.

### Gap 4: No Standalone Testable Component
The refresh logic is a method on `OpenApiSpotFeed` — hard to unit test without a full feed instance + reactor.

## Proposed Design: `TokenManager` Component

### Architecture

```
┌─────────────────────────────────────────────────┐
│                  TokenManager                    │
│  (src/forex_bot/adapters/ctrader/token_manager.py)│
├─────────────────────────────────────────────────┤
│ - state_path: Path  (data/token_state.json)     │
│ - client_id, client_secret                       │
│ - warn_days_before: float (default: 7.0)        │
│ - on_warn callback                               │
├─────────────────────────────────────────────────┤
│ + load_state() -> TokenState                     │
│ + save_state(state)                              │
│ + validate_on_startup() -> ValidationResult      │
│ + refresh(refresh_token) -> TokenPair            │
│ + check_and_warn() -> WarningLevel               │
│ + persist_to_env(access, refresh)                │
└─────────────────────────────────────────────────┘
```

### Data Model

```python
@dataclass
class TokenState:
    access_token: str
    refresh_token: str
    issued_at: float          # Unix timestamp
    expires_in: int           # seconds (from expiresIn)
    last_refresh_at: float | None
    last_refresh_status: str  # "success" | "failed" | "never"
    refresh_history: list[RefreshRecord]  # last N records

@dataclass
class RefreshRecord:
    timestamp: float
    status: str       # "success" | "failed"
    error: str | None
    new_expires_in: int | None

class WarningLevel(Enum):
    OK = "ok"                    # > warn_days_before remaining
    WARNING = "warning"          # < warn_days_before remaining
    CRITICAL = "critical"        # < 1 day remaining
    EXPIRED = "expired"          # already expired

@dataclass
class ValidationResult:
    valid: bool
    warning_level: WarningLevel
    days_remaining: float
    message: str
    placeholder_detected: bool
```

### Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `src/forex_bot/adapters/ctrader/token_manager.py` | **CREATE** | Standalone `TokenManager` class |
| `tests/test_token_manager.py` | **CREATE** | Unit tests for TokenManager |
| `data/token_state.json` | **AUTO-CREATED** | Persistent token state (gitignored) |
| `scripts/launch_blend_forward_test.py` | **MODIFY** | Add startup token validation via TokenManager |
| `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` | **MODIFY** | Delegate refresh to TokenManager; keep inline as fallback |

### Integration Points

#### 1. Startup Validation (in `launch_blend_forward_test.py`)

```python
# Before engine creation:
from adapters.ctrader.token_manager import TokenManager

tm = TokenManager(
    state_path=PROJECT_ROOT / "data" / "token_state.json",
    env_path=PROJECT_ROOT / ".env",
    client_id=os.getenv("CTRADER_OPENAPI_CLIENT_ID"),
    client_secret=os.getenv("CTRADER_OPENAPI_CLIENT_SECRET"),
    warn_days_before=7.0,
)

result = tm.validate_on_startup(
    access_token=os.getenv("CTRADER_OPENAPI_ACCESS_TOKEN"),
    refresh_token=os.getenv("CTRADER_OPENAPI_REFRESH_TOKEN"),
)

if not result.valid:
    logger.critical("Token validation failed: %s", result.message)
    sys.exit(1)

if result.warning_level != WarningLevel.OK:
    logger.warning("Token warning: %s (%.1f days remaining)", result.message, result.days_remaining)
    # Optionally attempt immediate refresh
    if result.warning_level in (WarningLevel.CRITICAL, WarningLevel.EXPIRED):
        logger.info("Attempting proactive refresh before startup...")
        new_tokens = tm.refresh()
        if new_tokens:
            logger.info("Token refreshed successfully — %.1f days until next expiry", new_tokens.days_remaining)
        else:
            logger.error("Token refresh failed — cannot start with expired/near-expired token")
            sys.exit(1)
```

#### 2. Feed Integration (in `open_api_spot_feed.py`)

- `OpenApiSpotFeed.__init__` accepts optional `token_manager: TokenManager`
- `_refresh_token_and_reauth()` delegates to `token_manager.refresh()` if available
- Falls back to inline implementation if no TokenManager (backward compat)
- `_persist_tokens()` delegates to `token_manager.persist_to_env()` + `token_manager.save_state()`
- On successful auth, update TokenManager state via `token_manager.on_auth_success(expires_in)`

#### 3. Warning Callback

```python
tm.on_warn = lambda level, days: logger.warning(
    "Token expiry warning: %s — %.1f days remaining", level.value, days,
)
# Could also integrate with notification system (Telegram, etc.)
```

## Acceptance Criteria

- [ ] `TokenManager` class in `src/forex_bot/adapters/ctrader/token_manager.py`
- [ ] `TokenState` persisted to `data/token_state.json` after every refresh
- [ ] `validate_on_startup()` detects placeholder tokens, expired tokens, and near-expiry tokens
- [ ] `refresh()` calls cTrader OAuth endpoint, returns new token pair, persists to `.env` + state file
- [ ] `check_and_warn()` returns `WarningLevel` based on configurable `warn_days_before`
- [ ] `launch_blend_forward_test.py` calls `validate_on_startup()` before engine creation
- [ ] `OpenApiSpotFeed` delegates to `TokenManager` when available (inline fallback preserved)
- [ ] Unit tests cover: startup validation, refresh flow, state persistence, warning levels, placeholder detection
- [ ] `data/token_state.json` added to `.gitignore`
- [ ] Backward compatible — feed works without TokenManager (existing inline path)

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| cTrader changes OAuth endpoint | Low | Keep endpoint URL configurable; log raw response on failure |
| Refresh token itself expires or is revoked | Medium | Detect 4xx on refresh → clear state, log critical, exit gracefully |
| `.env` write race (concurrent processes) | Low | PID guard already prevents duplicate forward test instances |
| State file corruption | Low | JSON parse error → rebuild from `.env` values + auth response |
| Clock drift on server | Very Low | Use server-provided `expiresIn` not local calculation |

## Out of Scope

- Notification system integration (Telegram/email alerts) — separate BQ
- Token refresh for non-forward-test processes — same TokenManager can be reused later
- Admin dashboard for token status — future work
- Removing inline refresh from `OpenApiSpotFeed` — keep as fallback

## Estimated Effort

| Task | Hours |
|------|-------|
| `TokenManager` class + data model | 2h |
| State persistence + `.env` write | 1h |
| Startup validation integration | 1h |
| Feed delegation (optional TokenManager) | 1h |
| Unit tests | 1.5h |
| Manual testing + edge cases | 0.5h |
| **Total** | **~7h (2 SP)** |

## Key Answer: Can Token Refresh Be Fully Automated?

**Yes.** cTrader OpenAPI supports programmatic token refresh via `POST https://openapi.ctrader.com/apps/token` with `grant_type=refresh_token`. No browser interaction required. This is already implemented inline in `OpenApiSpotFeed._refresh_token_and_reauth()`. The TokenManager extracts, tests, and makes this reusable.
