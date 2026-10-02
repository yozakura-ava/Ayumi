# Contributing to Ayumi

## Overview

Ayumi uses a **branch-based workflow** with git worktrees. Every agent has their own worktree (isolated working directory) and creates feature branches for all work. Code only reaches `main` after review.

## Quick Reference

| Agent | Worktree | Default Branch |
|-------|----------|----------------|
| Kai (Forex) | `worktrees/kai/` | `kai/main` |
| Nash (Crypto) | `worktrees/nash/` | `nash/main` |
| Media Manager | `worktrees/media/` | `media/main` |
| Research Manager | `worktrees/research/` | `research/main` |

## Workflow

### 1. Start a Feature

```bash
cd $AYUMI_ROOT/worktrees/<your-worktree>/
git fetch origin
git checkout -b <agent>/<feature-name> main
```

Branch naming: `<agent>/<short-description>` (e.g., `kai/stop-loss-feature`, `nash/momentum-scanner`)

### 2. Do the Work

- Write code in `src/<domain>/` for source code
- Write docs in `docs/<section>/` for documentation
- Write tests in `tests/` for test files
- Never commit secrets, API keys, or credentials

### 3. Commit

```bash
git add <files>
git commit -m "<type>: <description>

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

Commit types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`

### 4. Submit for Review

Create a Paperclip issue or comment on an existing one requesting QA review. Include:
- What was changed
- Which branch
- Any testing performed

### 5. Review Process

- **QA (Sage)** validates functionality and correctness
- **Critic** reviews architecture and code quality
- Reviewers can access your branch via the worktree or by checking out the branch

### 6. Merge to Main

Only after review approval:

```bash
# From the main worktree
cd $AYUMI_ROOT/
git fetch origin
git merge <agent>/<feature-name>
git push origin main
```

**Note:** The pre-push hook will automatically:
- Scan for secrets (gitleaks)
- Run tests (pytest)
- Block the push if anything fails

## Directory Structure

```
src/
├── forex_bot/          # Kai's domain - trading systems
├── crypto/             # Nash's domain - crypto tools
├── infrastructure/     # DevOps, CI/CD, deployment
└── shared/             # Common libraries and utilities
docs/
├── plans/              # Roadmaps, specs, OKRs
├── research/           # Research findings and analysis
├── reviews/            # QA/Critic review artifacts
├── decisions/          # Architecture Decision Records (ADRs)
└── memory/             # Agent memory and context
tests/                  # All test suites
drafts/                 # Work-in-progress (pre-review)
```

## Rules

1. **Never commit secrets** — use environment variables (see `.env.example`). The pre-push hook and CI will catch and block any leaks.
2. **Always branch off main** — never commit directly to `main` or another agent's branch.
3. **Tests required** — all new code must include tests. The CI pipeline runs pytest on every push.
4. **Commit messages** — use the format above and always include the `Co-Authored-By` line.
5. **One feature per branch** — keep branches focused and small.
6. **Document decisions** — significant technical decisions go in `docs/decisions/` as ADRs.
7. **Review before merge** — nothing goes to `main` without QA + Critic sign-off.
8. **Produce artifacts** — completed work (plans, specs, research, decisions) MUST be written as markdown files in `docs/`. Do not leave final outputs only in Paperclip issue descriptions or comments.

## Secret Management

- **NEVER** hardcode API keys, tokens, or credentials in code
- Use environment variables defined in `.env` (see `.env.example` for template)
- The `.env` file is gitignored and never committed
- gitleaks scans every commit and push for leaked secrets
- GitHub Actions also runs secret scanning on the remote

## CI Pipeline

### Local (pre-push hook)
1. gitleaks secret scan
2. pytest test suite

### Remote (GitHub Actions)
1. gitleaks secret scan
2. pytest test suite
3. (Future: SAST, dependency audit, build verification)

## Getting Help

If you're unsure about anything, ask in the relevant Paperclip issue or reach out to your manager in the chain of command.
