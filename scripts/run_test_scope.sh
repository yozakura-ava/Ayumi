#!/usr/bin/env bash
set -euo pipefail
REPO=$AYUMI_ROOT
cd "$REPO"

usage() {
  cat <<EOF
Usage: $0 [SCOPE] [OPTIONS] [PYTEST_ARGS...]

Scopes:
  --unit        Run tests/unit/ (all subcategories)
  --integration Run tests/integration/ (including ctrader/)
  --strategies  Run tests/strategies/ (including ict/)
  --factory     Run tests/factory/ (SFA-1 strategy-factory contract tests)
  --e2e         Run tests/e2e/
  --regression  Run tests/regression/
  --heavy       Run only heavy-import files (numpy/pandas/scipy/sklearn)
  --full        Run all categories sequentially in separate processes

Options:
  --collect-only  Pass --collect-only to pytest (dry run)
  --live          Include tests marked @pytest.mark.live
  --help          Show this help

Default: excludes live tests (-m "not live")
EOF
}

# Parse flags
SCOPE=""
COLLECT_ONLY=""
LIVE=""
PYTEST_EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --unit)         SCOPE="unit"; shift ;;
    --integration)  SCOPE="integration"; shift ;;
    --strategies)   SCOPE="strategies"; shift ;;
    --factory)      SCOPE="factory"; shift ;;
    --e2e)          SCOPE="e2e"; shift ;;
    --regression)   SCOPE="regression"; shift ;;
    --heavy)        SCOPE="heavy"; shift ;;
    --full)         SCOPE="full"; shift ;;
    --collect-only) COLLECT_ONLY="--collect-only"; shift ;;
    --live)         LIVE="1"; shift ;;
    --help|-h)      usage; exit 0 ;;
    *)              PYTEST_EXTRA+=("$1"); shift ;;
  esac
done

if [[ -z "$SCOPE" ]]; then
  usage
  exit 1
fi

# Build marker filter
MARKER=()
if [[ -z "$LIVE" ]]; then
  MARKER=(-m "not live")
fi

# Heavy-import discovery
find_heavy() {
  grep -rlE '^\s*(import|from)\s+(numpy|pandas|scipy|sklearn)' \
    tests/unit tests/integration tests/strategies tests/e2e tests/regression \
    --include='*.py' 2>/dev/null || true
}

run_pytest() {
  local label="$1"
  shift
  local files=("$@")
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "==> ${label}: no files to run"
    return 0
  fi
  echo "==> Running scope: ${label} (${#files[@]} files)"
  python3 -m pytest "${MARKER[@]}" $COLLECT_ONLY --tb=short "${files[@]}" "${PYTEST_EXTRA[@]}"
}

case "$SCOPE" in
  unit)
    run_pytest "unit" tests/unit/
    ;;
  integration)
    run_pytest "integration" tests/integration/
    ;;
  strategies)
    run_pytest "strategies" tests/strategies/
    ;;
  factory)
    run_pytest "factory" tests/factory/
    ;;
  e2e)
    run_pytest "e2e" tests/e2e/
    ;;
  regression)
    run_pytest "regression" tests/regression/
    ;;
  heavy)
    mapfile -t HEAVY_FILES < <(find_heavy)
    if [[ ${#HEAVY_FILES[@]} -eq 0 ]]; then
      echo "==> heavy: no heavy-import files found"
      exit 0
    fi
    run_pytest "heavy (${#HEAVY_FILES[@]} files)" "${HEAVY_FILES[@]}"
    ;;
  full)
    # Run each category in a separate process for memory isolation
    # Does NOT also run --heavy (mutually exclusive use case)
    run_pytest "unit" tests/unit/
    run_pytest "integration" tests/integration/
    run_pytest "strategies" tests/strategies/
    run_pytest "regression" tests/regression/
    # Skip e2e by default in --full unless --live is also passed
    if [[ -n "$LIVE" ]]; then
      run_pytest "e2e" tests/e2e/
    else
      echo "==> e2e: skipped (use --live to include)"
    fi
    ;;
esac
