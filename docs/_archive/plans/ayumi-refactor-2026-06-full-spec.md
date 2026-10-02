# Ayumi Infrastructure Refactor — Full Phase Specs (2026-06)

**Parent:** `ayumi-refactor-2026-06.md`  
**Purpose:** Detailed implementation specs for each phase. Council review target.

---

## Phase 1: Credentials Boundary (3 SP)

### Goal
Extract all authentication logic from the monolithic `open_api_spot_feed.py` into `CTraderAuth`. Eliminate token overwrite regressions permanently.

### Current State (The Problem)
- cTrader credentials live in `.env` alongside unrelated config (DB URLs, API keys, etc.)
- Builder agents (BQ-681) have overwritten working tokens with placeholders
- Token validation is scattered: some checks in feed, some in token_manager, some nowhere
- No atomic credential persistence — tokens can be half-written on crash
- No migration path — any change requires manual `.env` editing

### Files Created
- `src/forex_bot/adapters/ctrader/auth.py` — `CTraderAuth` class
- `src/forex_bot/adapters/ctrader/credentials.py` — Credential file management (load/save/migrate)
- `tests/test_ctrader_auth.py` — Unit tests
- `tests/test_credentials.py` — Unit tests

### Files Modified
- `src/forex_bot/open_api_spot_feed.py` — Replace inline auth with `CTraderAuth` composition
- `.gitignore` — Add `data/.credentials`
- `scripts/smoke_test.sh` — New pre-merge validation script

### Detailed Design

#### `credentials.py` — Credential File Manager

```python
CREDENTIALS_PATH = Path("data/.credentials")
CREDENTIALS_VERSION = 1

class CredentialManager:
    """Owns the credentials file. Nobody else reads or writes it."""
    
    def __init__(self, path: Path = CREDENTIALS_PATH):
        self._path = path
    
    def load(self) -> Credentials:
        """Load from JSON file. Raises CredentialsNotFoundError if missing."""
        # Validate version field
        # Validate all required fields present
        # Validate no placeholder values ('new-access', 'REPLACE', etc.)
    
    def save(self, creds: Credentials) -> None:
        """Atomic write: write to .tmp, rename to target. chmod 600."""
        # Atomic write pattern:
        # 1. Write to path.tmp
        # 2. os.chmod(path.tmp, 0o600)
        # 3. os.rename(path.tmp, path)
    
    def migrate_from_env(self) -> Credentials:
        """One-time migration: read .env tokens, write to credentials file.
        Logs warning. Does NOT delete .env values (safety)."""
        # Read CTRADER_ACCESS_TOKEN, CTRADER_REFRESH_TOKEN, etc. from .env
        # Validate they're not placeholders
        # Save to credentials file
        # Return loaded credentials
    
    def needs_migration(self) -> bool:
        """True if credentials file doesn't exist but .env has cTrader tokens."""
```

#### `auth.py` — Authentication Manager

```python
class CTraderAuth:
    """Owns ALL cTrader authentication. Nothing else touches auth."""
    
    def __init__(self, credential_manager: CredentialManager):
        self._mgr = credential_manager
        self._creds: Credentials | None = None
    
    @classmethod
    def create(cls, credentials_path: Path = CREDENTIALS_PATH) -> "CTraderAuth":
        """Factory: auto-migrates from .env if needed."""
        mgr = CredentialManager(credentials_path)
        if not mgr.exists() and mgr.needs_migration():
            logger.warning("Migrating credentials from .env to data/.credentials")
            mgr.migrate_from_env()
        return cls(mgr)
    
    def load_credentials(self) -> None:
        """Load and validate credentials. Raises on missing/invalid."""
        self._creds = self._mgr.load()
        result = self.validate_tokens()
        if not result.is_valid:
            raise InvalidCredentialsError(result.reason)
    
    def validate_tokens(self) -> TokenValidationResult:
        """Check: present, not placeholder, not expired (if expiry available)."""
        # Placeholder detection: reject 'new-access', 'REPLACE', 'xxx', etc.
        # Expiry: if we have a refresh timestamp, check staleness
    
    def app_authenticate(self, client) -> bool:
        """Perform ProtoAuthRequest. Returns success boolean.
        On success, persists any updated tokens."""
    
    def account_authenticate(self, client, account_id: str) -> bool:
        """Perform ProtoAccountAuthRequest. Returns success boolean."""
    
    def refresh_tokens(self) -> bool:
        """Refresh via cTrader REST API. Save new tokens on success.
        Uses requests/oauth flow, not protobuf."""
    
    @property
    def access_token(self) -> str: 
        if not self._creds:
            raise CredentialsNotLoadedError()
        return self._creds.access_token
    
    @property
    def refresh_token(self) -> str:
        if not self._creds:
            raise CredentialsNotLoadedError()
        return self._creds.refresh_token
    
    @property
    def client_id(self) -> str: ...
    @property
    def client_secret(self) -> str: ...
    @property
    def account_id(self) -> str: ...
```

#### Integration into `open_api_spot_feed.py`

```python
# BEFORE (scattered):
#   self.access_token = os.getenv("CTRADER_ACCESS_TOKEN")
#   ... inline ProtoAuthRequest building
#   ... inline token refresh logic

# AFTER (composed):
class OpenApiSpotFeed:
    def __init__(self, ...):
        self.auth = CTraderAuth.create()  # auto-migrates if needed
        self.auth.load_credentials()
    
    def _authenticate(self):
        if not self.auth.app_authenticate(self.client):
            raise AuthError("App auth failed")
        if not self.auth.account_authenticate(self.client, self.auth.account_id):
            raise AuthError("Account auth failed")
```

#### `smoke_test.sh`

```bash
#!/bin/bash
set -e
cd "$(dirname "$0")/.."

echo "=== Ayumi Smoke Test ==="

# 1. Import check
python3 -c "from adapters.ctrader.auth import CTraderAuth; print('✓ auth import')"
python3 -c "from adapters.ctrader.credentials import CredentialManager; print('✓ credentials import')"

# 2. No plaintext credentials in source
if grep -rn "client_secret.*=\|access_token.*=.*['\"][a-f0-9]" src/ --include="*.py"; then
    echo "✗ Plaintext credentials found in source!"
    exit 1
fi
echo "✓ No plaintext credentials"

# 3. File ownership
ROOT_FILES=$(find src/ tests/ -user root 2>/dev/null | wc -l)
if [ "$ROOT_FILES" -gt 0 ]; then
    echo "✗ $ROOT_FILES root-owned files found"
    exit 1
fi
echo "✓ File ownership OK"

# 4. Credential file permissions
if [ -f "data/.credentials" ]; then
    PERMS=$(stat -c %a data/.credentials)
    if [ "$PERMS" != "600" ]; then
        echo "✗ Credentials file has wrong permissions: $PERMS"
        exit 1
    fi
    echo "✓ Credentials file permissions OK"
fi

# 5. Unit tests
python3 -m pytest tests/test_ctrader_auth.py tests/test_credentials.py -q

echo "=== All checks passed ==="
```

### Test Coverage

| Test | What it validates |
|------|------------------|
| `test_credentials_load_valid` | Valid JSON file loads correctly |
| `test_credentials_load_missing` | Missing file raises CredentialsNotFoundError |
| `test_credentials_load_invalid_json` | Malformed JSON raises error |
| `test_credentials_save_atomic` | Write goes to .tmp then rename |
| `test_credentials_save_permissions` | File created with chmod 600 |
| `test_credentials_migrate_from_env` | Reads .env, writes to file, doesn't modify .env |
| `test_credentials_migrate_placeholder` | Rejects placeholder tokens during migration |
| `test_auth_app_authenticate_success` | Mock client, verify ProtoAuthRequest sent |
| `test_auth_app_authenticate_failure` | Mock client failure, verify returns False |
| `test_auth_account_authenticate` | Mock client, verify account auth flow |
| `test_auth_validate_tokens_valid` | Valid tokens pass validation |
| `test_auth_validate_tokens_placeholder` | Placeholder tokens rejected |
| `test_auth_validate_tokens_missing` | Missing tokens rejected |
| `test_auth_refresh_tokens` | Mock HTTP refresh, verify new tokens saved |
| `test_no_credentials_in_logs` | Capture log output, verify no tokens leaked |

### Memory Budget
- `CTraderAuth`: < 1KB resident (just holds credentials struct)
- `CredentialManager`: Stateless, no resident memory
- Tests: All mocked, no network/TCP, < 50MB per test process

### Success Criteria
1. `CTraderAuth` owns all auth — no other file reads credentials directly
2. Tokens persist across restarts without `.env` modification
3. Builder agents cannot overwrite tokens (credentials file is gitignored, not in `.env`)
4. Smoke test passes
5. Zero plaintext credentials in logs

---

## Phase 2: Connection & Bar Builder Extraction (4 SP)

### Goal
Extract TCP connection management and bar aggregation into independent, testable modules. Slim the feed from 2100+ lines to < 500.

### Files Created
- `src/forex_bot/adapters/ctrader/connection.py` — `CTraderConnection`
- `src/forex_bot/adapters/ctrader/bar_builder.py` — `BarBuilder`
- `tests/test_ctrader_connection.py`
- `tests/test_bar_builder.py`

### Files Modified
- `src/forex_bot/open_api_spot_feed.py` — Major slim, becomes orchestrator

### Detailed Design

#### `connection.py` — Connection Manager

```python
class CTraderConnection:
    """Owns TCP lifecycle, reconnection, and health monitoring."""
    
    def __init__(self, host: str, port: int, 
                 reconnect_base_delay: float = 1.0,
                 reconnect_max_delay: float = 60.0,
                 heartbeat_interval: float = 30.0):
        self._host = host
        self._port = port
        self._reconnect_base_delay = reconnect_base_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._connected_at: float | None = None
        self._reconnect_count: int = 0
        self._message_callback: Callable | None = None
    
    async def connect(self) -> bool:
        """Establish TCP connection. Returns success."""
        # Create socket, connect, start receive loop
        # On success: set _connected_at = time.time()
    
    async def disconnect(self) -> None:
        """Clean disconnect. Cancels receive loop."""
    
    async def reconnect_with_backoff(self) -> bool:
        """Exponential backoff + jitter. Max 5 attempts per cycle.
        Resets backoff on successful connect."""
        delay = min(
            self._reconnect_base_delay * (2 ** self._reconnect_count),
            self._reconnect_max_delay
        ) + random.uniform(0, 1.0)  # jitter
        await asyncio.sleep(delay)
        self._reconnect_count += 1
        success = await self.connect()
        if success:
            self._reconnect_count = 0
        return success
    
    def send(self, message) -> None:
        """Encode ProtoMessage and send. Raises if disconnected."""
    
    def set_message_callback(self, callback: Callable) -> None:
        """Register callback for received messages."""
    
    @property
    def is_connected(self) -> bool:
        """Check socket is alive and receive loop is running."""
    
    @property
    def uptime_seconds(self) -> float:
        """Seconds since last successful connect."""
    
    @property
    def reconnect_count(self) -> int:
        """Total reconnection attempts since last clean start."""
```

**Key design decisions:**
- Async throughout — matches the existing event loop pattern
- Backoff capped at 60s with jitter to prevent thundering herd
- Reconnect counter resets on success, so we track consecutive failures
- Health monitoring via heartbeat_interval — if no message received in 2x interval, trigger reconnect

#### `bar_builder.py` — Bar Aggregation

```python
@dataclass
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime
    timeframe: int  # in seconds
    is_complete: bool = False

class BarBuilder:
    """Aggregates ticks into OHLCV bars. Multi-timeframe support."""
    
    def __init__(self, timeframes: list[int], max_bars: int = 500):
        self._timeframes = sorted(timeframes)
        self._max_bars = max_bars
        self._bars: dict[int, list[Bar]] = {tf: [] for tf in timeframes}
        self._current_bars: dict[int, Bar | None] = {tf: None for tf in timeframes}
        self._total_bars_built: int = 0
    
    def update_tick(self, timestamp: datetime, bid: float, ask: float, 
                    volume: float = 0.0) -> list[Bar]:
        """Process a tick. Returns any newly completed bars (can be 0-N).
        
        For each timeframe:
        1. Determine current bar period
        2. If no current bar → create new one
        3. If tick is in same period → update high/low/close/volume
        4. If tick crosses period boundary → finalize current, create new, return completed
        """
        completed = []
        mid = (bid + ask) / 2
        
        for tf in self._timeframes:
            bar_period = self._get_bar_period(timestamp, tf)
            
            current = self._current_bars[tf]
            if current is None:
                # First tick for this timeframe
                self._current_bars[tf] = Bar(
                    open=mid, high=mid, low=mid, close=mid,
                    volume=volume, timestamp=bar_period, timeframe=tf
                )
            elif bar_period != current.timestamp:
                # Period boundary crossed — finalize
                current.close = mid
                current.is_complete = True
                completed.append(current)
                self._bars[tf].append(current)
                self._trim_bars(tf)
                self._total_bars_built += 1
                
                # Start new bar
                self._current_bars[tf] = Bar(
                    open=mid, high=mid, low=mid, close=mid,
                    volume=volume, timestamp=bar_period, timeframe=tf
                )
            else:
                # Update existing bar
                current.high = max(current.high, mid)
                current.low = min(current.low, mid)
                current.close = mid
                current.volume += volume
        
        return completed
    
    def get_bars(self, timeframe: int) -> list[Bar]:
        """Get completed bars for a timeframe."""
    
    def get_current_bar(self, timeframe: int) -> Bar | None:
        """Get the forming (incomplete) bar."""
    
    def preload_bars(self, timeframe: int, bars: list[Bar]) -> None:
        """Pre-load historical bars (e.g., from API on startup)."""
    
    def _get_bar_period(self, timestamp: datetime, timeframe: int) -> datetime:
        """Align timestamp to bar boundary."""
        # For timeframe 60 (1H): truncate to hour
        # For timeframe 1440 (1D): truncate to day
        # Generic: floor to nearest timeframe boundary
    
    def _trim_bars(self, timeframe: int) -> None:
        """Keep only max_bars per timeframe."""
        if len(self._bars[timeframe]) > self._max_bars:
            self._bars[timeframe] = self._bars[timeframe][-self._max_bars:]
    
    @property
    def total_bars_built(self) -> int:
        return self._total_bars_built
```

**Memory budget:**
- 500 bars × 8 timeframes × ~100 bytes = ~400KB max
- No unbounded growth — `_trim_bars` enforces cap

#### Slimmed `open_api_spot_feed.py` (Target: < 500 lines)

After extraction, the feed becomes:

```python
class OpenApiSpotFeed:
    """Orchestrator: composes auth, connection, bar_builder."""
    
    def __init__(self, config):
        self.auth = CTraderAuth.create()
        self.connection = CTraderConnection(
            host=config.ctrader_host,
            port=config.ctrader_port
        )
        self.bar_builder = BarBuilder(
            timeframes=config.timeframes,
            max_bars=config.max_bars
        )
        self.kill_switch = KillSwitchManager(state_path=config.kill_switch_path)
        self.risk_guard = RiskGuard(kill_switch=self.kill_switch)
        
        self.connection.set_message_callback(self._on_message)
    
    async def start(self):
        """Full startup sequence."""
        self.auth.load_credentials()
        await self.connection.connect()
        self.auth.app_authenticate(self.connection)
        self.auth.account_authenticate(self.connection, self.auth.account_id)
        self._subscribe_symbols()
    
    def _on_message(self, message):
        """Route messages to handlers."""
        if message.type == "tick":
            completed_bars = self.bar_builder.update_tick(
                message.timestamp, message.bid, message.ask
            )
            for bar in completed_bars:
                self._on_bar_close(bar)
            self._on_tick(message)
        elif message.type == "heartbeat":
            pass  # connection handles heartbeat
        else:
            logger.debug(f"Unhandled message type: {message.type}")
    
    async def stop(self):
        """Clean shutdown."""
        await self.connection.disconnect()
```

### Test Coverage

| Test | What it validates |
|------|------------------|
| `test_connection_connect_success` | Mock socket, verify connect flow |
| `test_connection_connect_failure` | Mock socket failure |
| `test_connection_reconnect_backoff` | Verify exponential backoff timing |
| `test_connection_reconnect_jitter` | Verify jitter is applied |
| `test_connection_reconnect_max_delay` | Verify cap at 60s |
| `test_connection_send_disconnected` | Raises when sending while disconnected |
| `test_connection_uptime_tracking` | Verify uptime calculation |
| `test_bar_builder_single_timeframe` | Ticks produce correct bars |
| `test_bar_builder_multi_timeframe` | Ticks update all timeframes |
| `test_bar_builder_period_boundary` | Bar closes at period boundary |
| `test_bar_builder_max_bars` | Bar history trimmed to max |
| `test_bar_builder_preload` | Historical bars loaded correctly |
| `test_bar_builder_tick_updates` | OHLCV calculations correct |
| `test_feed_orchestrator_start` | Integration: auth → connect → subscribe |
| `test_feed_orchestrator_tick_routing` | Message routing to bar builder |
| `test_feed_orchestrator_shutdown` | Clean disconnect |

### Memory Budget
- `CTraderConnection`: ~10KB (socket buffers)
- `BarBuilder`: ~400KB at max bars (500 × 8 timeframes × ~100 bytes)
- Total per-instance: < 1MB (down from unbounded in current monolith)

### Success Criteria
1. `open_api_spot_feed.py` is under 500 lines
2. Connection reconnection works with backoff (tested)
3. Bar builder produces correct OHLCV from ticks (tested)
4. All existing forward test behavior preserved
5. No memory leaks in bar builder (trim enforced)

---

## Phase 3: Builder Safety & Test Infrastructure (1 SP)

### Goal
Enforce development workflow rules that prevent regressions from builder agents.

### Files Created
- `scripts/builder_wrapper.sh` — File ownership enforcement
- `scripts/smoke_test.sh` — Enhanced with Phase 2 checks
- `tests/conftest.py` — Shared fixtures (mock kill switch, mock connection, mock auth)

### Builder Safety Rules (Enforced Programmatically)

```bash
#!/bin/bash
# scripts/builder_wrapper.sh
# Usage: builder_wrapper.sh <build_command>

# 1. Run the build
"$@"

# 2. Fix file ownership
find src/ tests/ -user root -exec chown $USER:$USER {} \; 2>/dev/null

# 3. Verify no .env modification
if git diff --name-only .env | grep -q .env; then
    echo "ERROR: Builder modified .env! Reverting."
    git checkout .env
    exit 1
fi

# 4. Verify no data/ writes from builder
if git diff --name-only data/ | grep -q .; then
    echo "WARNING: Builder wrote to data/ — these should be runtime-only."
fi

# 5. Verify no logs/ writes
if find logs/ -newer /tmp/build_start -user root | grep -q .; then
    echo "ERROR: Builder wrote to logs/ as root."
    exit 1
fi
```

### Test Infrastructure

```python
# tests/conftest.py
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.fixture
def mock_auth():
    """CTraderAuth with pre-loaded fake credentials."""
    auth = MagicMock(spec=CTraderAuth)
    auth.access_token = "test-access-token"
    auth.refresh_token = "test-refresh-token"
    auth.client_id = "test-client-id"
    auth.client_secret = "test-client-secret"
    auth.account_id = "test-account-id"
    auth.app_authenticate = MagicMock(return_value=True)
    auth.account_authenticate = MagicMock(return_value=True)
    auth.validate_tokens = MagicMock(return_value=TokenValidationResult(is_valid=True))
    return auth

@pytest.fixture
def mock_connection():
    """CTraderConnection with async mocks."""
    conn = MagicMock(spec=CTraderConnection)
    conn.connect = AsyncMock(return_value=True)
    conn.disconnect = AsyncMock(return_value=None)
    conn.send = MagicMock(return_value=None)
    conn.is_connected = True
    conn.uptime_seconds = 100.0
    return conn

@pytest.fixture
def mock_kill_switch():
    """KillSwitchManager that never triggers."""
    ks = MagicMock(spec=KillSwitchManager)
    ks.is_triggered = False
    ks.check = MagicMock(return_value=False)
    return ks

@pytest.fixture
def mock_risk_guard(mock_kill_switch):
    """RiskGuard with injected mock kill switch."""
    rg = MagicMock(spec=RiskGuard)
    rg.kill_switch = mock_kill_switch
    rg.check_position = MagicMock(return_value=True)
    return rg
```

### Kill Switch DI Completion

All tests that previously instantiated `KillSwitchManager()` with default production path must be updated to use the `mock_kill_switch` fixture. Zero tests should write to `data/state/kill_switch_state.json`.

```python
# BEFORE (WRONG):
class TestRiskGuard:
    def test_daily_loss(self):
        rg = RiskGuard()  # Creates real KillSwitchManager → writes to data/state/

# AFTER (CORRECT):
class TestRiskGuard:
    def test_daily_loss(self, mock_kill_switch):
        rg = RiskGuard(kill_switch=mock_kill_switch)  # DI, no production state
```

### Success Criteria
1. `builder_wrapper.sh` catches `.env` modification and root-owned files
2. All kill switch tests use DI mocks — zero production state writes
3. `conftest.py` provides shared fixtures for auth, connection, kill switch
4. Smoke test covers Phase 1 + Phase 2 imports

---

## Phase 4: Signal Focus (Post-Infrastructure)

### Goal
With stable infrastructure, return to getting signals flowing.

### Prerequisites
- Phase 1 complete (auth works, tokens persist)
- Phase 2 complete (connection resilient, bars building)
- Phase 3 complete (builders can't regress infrastructure)
- Real cTrader tokens from Craig (tested in Phase 1, validated end-to-end)

### Scope
This phase is intentionally light on spec — the details depend on what diagnostics reveal once we have stable infrastructure. Rough work items:

1. **Strategy diagnostics** — Why are 8/10 strategies producing zero signals?
2. **Parameter audit** — Are strategy parameters realistic for current market conditions?
3. **Walk-forward validation** — Run recent data through signal engine
4. **Blend pipeline integration** — `confidence_learner.py` and `blend_optimizer.py` exist but aren't wired in
5. **Kill switch calibration** — Daily loss limit shouldn't trigger with 0 trades (known bug from Jun 12)

### Estimated SP
- Strategy diagnostics: 2 SP
- Kill switch fix: 1 SP
- Blend pipeline wiring: 3 SP
- Total: 6 SP (likely 2+ autobuild cycles)

---

## Cross-Phase Constraints

### Memory Budget (Enforced)
| Component | Max RSS | How Enforced |
|-----------|---------|-------------|
| `CTraderAuth` | < 1KB | Struct only |
| `CTraderConnection` | < 50KB | Socket buffers |
| `BarBuilder` | < 1MB | `max_bars` trim |
| `OpenApiSpotFeed` (orchestrator) | < 2MB | Composition only |
| **Total forward test** | < 10MB | Overall cap |
| **Test suite** | < 100MB per test | Mock everything external |

### Performance Budget
| Operation | Max Latency |
|-----------|-------------|
| Tick → bar update | < 1ms |
| Auth sequence (app + account) | < 5s |
| Reconnection (single attempt) | < 3s |
| Smoke test (full) | < 30s |

### Dependency Graph
```
Phase 1 (Auth) ─────┐
                     ├──► Phase 4 (Signal Focus)
Phase 2 (Conn/Bars) ─┤
                     │
Phase 3 (Safety) ────┘
```
- Phase 1 and Phase 2 can run in parallel (different files, no overlap)
- Phase 3 depends on Phase 1+2 structure being stable
- Phase 4 depends on 1+2+3 all complete

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Builder overwrites credentials during Phase 2 | Medium | High | Phase 1 must be merged before Phase 2 starts; credentials already gitignored |
| cTrader API changes break auth flow | Low | High | Auth is isolated — only `auth.py` needs updating |
| Bar builder OHLCV calculation differs from current | Medium | Medium | Parallel run: both old and new bar builders active for 1 session, compare outputs |
| Reconnection logic has edge case (network flap) | Medium | Medium | Capped retry count (5), exponential backoff with jitter |
| Kill switch DI incomplete — test writes to production | Low | High | `conftest.py` fixture enforced, grep audit in smoke test |
| Memory leak in bar builder under high tick volume | Low | Medium | `max_bars` enforced per-timeframe, monitored in tests |
| Migration from .env fails silently | Low | High | Migration validates tokens before writing; smoke test checks file exists |
