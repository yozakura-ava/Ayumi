#!/bin/bash
# restart_forward_test.sh — deterministic restart with health verification
#
# Usage: bash scripts/restart_forward_test.sh [--paper]
#   --paper  Run in paper-only mode (no live orders)
#
# Exit codes:
#   0 = forward test running and healthy
#   1 = startup failed

set -euo pipefail

PROJECT_DIR="$AYUMI_ROOT"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
PID_FILE="${PROJECT_DIR}/data/forward_test.pid"
HEARTBEAT_FILE="${PROJECT_DIR}/data/heartbeat_trading.json"
LOG_FILE="${PROJECT_DIR}/logs/forward_test.log"
MAX_WAIT=90  # seconds to wait for heartbeat

cd "$PROJECT_DIR"

echo "=== Ayumi Forward Test Restart ==="
echo "$(date)"
echo ""

# Step 1: Stop existing forward test
echo "── Step 1: Stopping existing forward test ──"
if pgrep -f "launch_forward_test\|launch_blend_forward" >/dev/null 2>&1; then
    OLD_PID=$(pgrep -f "launch_forward_test\|launch_blend_forward" | head -1)
    echo "  Found PID $OLD_PID — sending SIGTERM..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 3
    
    # Force kill if still running
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "  Still running — sending SIGKILL..."
        kill -9 "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    echo "  ✓ Stopped"
else
    echo "  No existing forward test found"
fi

# Step 2: Verify credentials
echo ""
echo "── Step 2: Verify credentials ──"
if [ ! -f "data/.credentials" ]; then
    echo "  ✗ data/.credentials not found"
    exit 1
fi

# Sync credentials from .env if needed
"$PYTHON" -c "
import json, re
env = {}
with open('.env') as f:
    for line in f:
        m = re.match(r'^CTRADER_OPENAPI_(\w+)=(.+)$', line.strip())
        if m: env[m.group(1)] = m.group(2)

with open('data/.credentials') as f:
    creds = json.load(f)

# Check if .env has newer tokens
env_at = env.get('ACCESS_TOKEN', '')
cred_at = creds.get('access_token', '')
if env_at and env_at != cred_at:
    print('  ⚠️  .env and .credentials mismatch — syncing from .env')
    creds['access_token'] = env_at
    creds['refresh_token'] = env.get('REFRESH_TOKEN', creds.get('refresh_token', ''))
    with open('data/.credentials', 'w') as f:
        json.dump(creds, f, indent=2)
    print('  ✓ Synced')
else:
    print('  ✓ Credentials match')
" || { echo "  ✗ Credential check failed"; exit 1; }

# Step 3: Clear kill switch
echo ""
echo "── Step 3: Clear kill switch ──"
if [ -f "data/kill_switches/global.state" ]; then
    echo '{}' > "data/kill_switches/global.state"
    echo "  ✓ Kill switch cleared"
else
    echo "  ✓ No kill switch file"
fi

# Step 4: Start forward test
echo ""
echo "── Step 4: Starting forward test ──"

MODE="--live"
if [ "${1:-}" = "--paper" ]; then
    MODE=""
    echo "  Mode: paper-only"
else
    echo "  Mode: live"
fi

# Launcher pinned to blend launcher (legacy launcher removed per card a7c12aea)
LAUNCHER="scripts/launch_blend_forward_test.py"

export PYTHONPATH="src/forex_bot:src"
nohup "$PYTHON" "$LAUNCHER" --symbols GBPUSD,USDJPY $MODE >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
echo "  Started PID $NEW_PID"
echo "  Log: $LOG_FILE"

# Step 5: Wait for heartbeat
echo ""
echo "── Step 5: Waiting for heartbeat (${MAX_WAIT}s timeout) ──"
for i in $(seq 1 "$MAX_WAIT"); do
    sleep 1
    
    # Check if process is still alive
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        echo "  ✗ Process died after ${i}s"
        echo ""
        echo "=== Last 30 log lines ==="
        tail -30 "$LOG_FILE" 2>/dev/null || echo "(no log output)"
        exit 1
    fi
    
    # Check heartbeat
    if [ -f "$HEARTBEAT_FILE" ]; then
        RUNNING=$("$PYTHON" -c "
import json
try:
    d = json.load(open('$HEARTBEAT_FILE'))
    print('True' if d.get('engine_running') else 'False')
except:
    print('False')
" 2>/dev/null)
        
        if [ "$RUNNING" = "True" ]; then
            echo "  ✓ Forward test healthy after ${i}s"
            echo ""
            echo "=== Status ==="
            "$PYTHON" -c "
import json
d = json.load(open('$HEARTBEAT_FILE'))
print(f'  PID: {d.get(\"pid\", \"?\")}')
print(f'  Ticks: {d.get(\"ticks_received\", 0)}')
print(f'  Engine: {\"running\" if d.get(\"engine_running\") else \"stopped\"}'  )
" 2>/dev/null
            echo ""
            echo "✅ Forward test started successfully"
            
            # Run sanity check if available
            if [ -f "scripts/verify_ctrader_live.py" ]; then
                echo ""
                echo "=== Running sanity check ==="
                "$PYTHON" scripts/verify_ctrader_live.py 2>/dev/null || true
            fi
            
            exit 0
        fi
    fi
    
    # Progress indicator every 10s
    if [ $((i % 10)) -eq 0 ]; then
        echo "  ...waiting (${i}s elapsed)"
    fi
done

echo ""
echo "  ✗ Forward test did not become healthy within ${MAX_WAIT}s"
echo ""
echo "=== Last 30 log lines ==="
tail -30 "$LOG_FILE" 2>/dev/null || echo "(no log output)"
exit 1
