#!/bin/bash
# Walk-forward runner with resource limits (BQ-1038)
# Defaults: 20% CPU, 2GB memory
# Override with --max-cpu / --max-memory-mb flags or MAX_CPU / MAX_MEM env vars.
MAX_CPU="${MAX_CPU:-20}"
MAX_MEM="${MAX_MEM:-2048}"

# Parse --max-cpu and --max-memory-mb from args, removing them before passing through
PARSED_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-cpu)
            MAX_CPU="$2"
            shift 2
            ;;
        --max-memory-mb)
            MAX_MEM="$2"
            shift 2
            ;;
        --max-cpu=*)
            MAX_CPU="${1#*=}"
            shift
            ;;
        --max-memory-mb=*)
            MAX_MEM="${1#*=}"
            shift
            ;;
        *)
            PARSED_ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${PARSED_ARGS[@]}"

cd $AYUMI_ROOT
source .venv/bin/activate

export MAX_CPU
export MAX_MEM

# Apply OS-level limits as a safety net
# Resource limits are applied inside Python via common.resource_limits
python -c "
import os, sys
sys.path.insert(0, 'src/forex_bot')
from common.resource_limits import cpu_limited, memory_capped
print(f'Resource limits: CPU={os.environ.get(\"MAX_CPU\", \"20\")}%, Memory={os.environ.get(\"MAX_MEM\", \"2048\")}MB')
" 2>&1

python scripts/run_tts_walkforward.py "$@" \
    --max-cpu "$MAX_CPU" \
    --max-memory-mb "$MAX_MEM"
