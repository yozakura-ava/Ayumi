# Ayumi Repository Cleanup — 2026-06-16

**Performed by:** Ava (with Craig approval)
**Reason:** 6+ weeks of accumulated stale branches, stashes, and sister worktrees taxing every AI session and confusing `git worktree list`.

## Stashes Dropped (2)

Both stashes were old log file diffs (forward test log appends) with no unique code changes. Dropped permanently.

- `stash@{0}` — WIP on main: f27c32b "fix: archive stale test files referencing archived modules" (old log modifications)
- `stash@{1}` — WIP on main: 9c2436f "chore: add generated data files to .gitignore" (old log modifications)

## Worktrees Removed (3 git worktrees + 1 orphan)

| Path | Type | Branch | Last Modified | Reason |
|------|------|--------|---------------|--------|
| `worktrees/kai/` | git worktree | `feat/AYUAA-802-multi-strategy-arch` | 2026-04-21 (8 weeks) | WIP-UNREVIEWED, never merged, main has refactored beyond |
| `worktrees/nash/` | git worktree | `nash/main` | 2026-04-16 (9 weeks) | 0 commits ahead of main, pure staleness |
| `worktrees/junior-dev-2/` | git worktree | `junior-dev-2/AYUAA-785-srmr-multi-pair` | 2026-05-18 (4 weeks) | SRMR+ multi-pair work; main has progressed |
| `worktrees/junior-dev-1-old/` | orphan dir (not git) | n/a | 2026-04-11 (2 months) | Pre-refactor forex research experiments, 185 files, no longer relevant |

Architecture-v2 references Kai's `core/engine/indicators` packages in Section 6. The architecture concept has been incorporated into the current `src/forex_bot/` structure (refactor completed June 12). The Kai prototype work is no longer needed.

## Local Branches Pruned (33)

Deleted with `git branch -D`:
- `feat/AYU-120-cli-signal-command` (8 weeks)
- `feat/AYU-54-signal-engine-trade-direction` (9 weeks)
- `feat/AYU-75-portfolio-backtest` (8 weeks)
- `feat/AYUAA-799-bb-rsi-reversion` (8 weeks)
- `feat/AYUAA-802-multi-strategy-arch` (8 weeks, Kai WIP)
- `feat/AYUAA-846-sentinel-defaults-H1-H5` (7 weeks)
- `feat/AYUAA-808-wire-ttc-xauUSD` (8 weeks)
- `fix/AVA-829-ctrader-socket-liveness-check` (7 weeks)
- `fix/ayuaa-778-paper-mode-real-fills` (6 weeks)
- `fix/ayuaa-847-sentinel-defaults` (6 weeks)
- `fix/fwd-test-remediation-2026-05-22` (11 days)
- `forex-manager/forward-test-deploy` (10 weeks)
- `forex-manager/main` (2 months)
- `junior-dev-1/AYUAA-643-q2-lod-hod-stop-rate` (10 weeks)
- `junior-dev-1/AYUAA-643-q2-lod-hod-v2` (10 weeks)
- `junior-dev-1/ayuaa-783-vrb-strategy` (9 weeks)
- `junior-dev-1/main` (2 months)
- `junior-dev-2/AYUAA-785-srmr-multi-pair` (4 weeks)
- `junior-dev-2/main` (2 months)
- `kai/57-migrate-quant-ml-indicators` (9 weeks)
- `kai/AYUAA-802-recovery` (8 weeks)
- `kai/ayu-41-architecture-spec` (9 weeks)
- `kai/ayu-47-fix` (9 weeks)
- `kai/ayu-47-v2-engine` (9 weeks)
- `kai/main` (9 weeks)
- `nash/main` (9 weeks)
- `research/main` (2 months)
- `sage/main` (10 weeks)
- `senior-dev/ayuaa-778-paper-mode-real-fills` (9 weeks)
- `senior-dev/ayuaa-779-confidence-calibration` (9 weeks)
- `senior-dev/confluence-pipeline-rebuild` (9 weeks)
- `senior-dev/main` (2 months)
- `senior-dev/ttc-commit-main` (6 weeks)
- `temp-sync` (10 weeks)

## Stale Refs Cleaned

- Removed stale `.git/refs/remotes/local/main` (orphan ref pointing to non-existent remote)
- `git fetch --prune origin` to sync remote tracking refs
- `git gc --auto --prune=now` to clean loose objects

## Final Repo State

**Local branches:** `main`, `media/main` (2 total — clean)
**Remote branches:** `origin/main`, `origin/HEAD -> origin/main`, plus 9 `origin/dependabot/pip/*` (auto-created security update branches — preserved)
**Worktrees:** `Ayumi` (main) and `worktrees/media` (2 total — clean)
**Stashes:** 0 (clean)
**`.git` size:** 104 MB (down from larger pre-cleanup size)

## Remote Sync

`git fetch --prune origin` was run. The remote `origin` (github.com/jalapeno777/Ayumi.git) was already in a clean state — it had only `main` plus recent `dependabot/*` branches. No push needed for branch deletion because the local stale branches had no remote counterparts (they were local-only). This is the desired end state: local and remote both clean.

## Files Preserved

This cleanup record at `docs/architecture/legacy/ayumi-repo-cleanup-2026-06-16.md`.

## BQ Cross-Reference

This cleanup implements task D2 of sprint 2026-06-16 and BQ-1039.
