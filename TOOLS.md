# TOOLS.md — Senior Software Engineer

## Development
- Python: `src/forex_bot/`
- Tests: `pytest tests/ -q`
- Shared venv: `$AYUMI_ROOT/.venv`
- Activate: `source $AYUMI_ROOT/.venv/bin/activate`

## Git
- Sync: `git fetch origin && git rebase main`
- Branch: `git checkout -b senior-dev/<feature-name> main`
- Commit: conventional format, include `Co-Authored-By: Paperclip <noreply@paperclip.ing>`

## Key Paths
- Strategies: `src/forex_bot/strategies/`
- Backtest: `src/forex_bot/backtest/`
- ML pipeline: `src/forex_bot/ml/`
- Tests: `tests/`
- Docs: `docs/forex/`, `docs/decisions/`
