#!/usr/bin/env python3
"""Builder Quality Gate — deterministic post-builder verification script.

Runs after every builder completes, before workboard_complete() or council review.
Cannot be rubber-stamped: produces structured JSON evidence that Ava must cite.

Checks:
  1. py_compile on all modified Python files
  2. node --check on all modified JS files
  3. ruff lint on all modified Python files
  4. mypy type-check on all modified Python files
  5. eslint on all modified JS files (if config exists)
  6. tsc type-check on all modified TypeScript files (if tsconfig.json exists)
  7. Path constant consistency (grep for path constants in 2+ files)
  8. Unwired function detection (functions defined but never called)
  9. Targeted test suite for modified modules
 10. JSON validity for any modified .json files

Usage:
    python3 scripts/builder_quality_gate.py [--workspace PATH] [--files file1,file2,...]
    python3 scripts/builder_quality_gate.py --git-diff   # auto-detect from git diff

    # For non-git-tracked extension directories (e.g. ~/.openclaw/extensions/my-ext):
    python3 scripts/builder_quality_gate.py --files ~/.openclaw/extensions/my-ext/src/index.ts --skip-tests --skip-mypy

    # tsc typecheck overrides (card f3c668bf — large-project OOM fix):
    python3 scripts/builder_quality_gate.py --files src/foo.ts \
        --tsc-timeout 240 --tsc-node-options "--max-old-space-size=12288" \
        --tsconfig plugins/foo/tsconfig.json
    # Env-var equivalents: BQG_TSC_TIMEOUT, BQG_TSC_NODE_OPTIONS, BQG_TSC_CONFIG

Output:
    JSON report to stdout + writes to data/ops/quality_gate_reports/<timestamp>.json
    Exit code 0 = all checks passed, 1 = any check failed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Bootstrap sys.path so the import works whether BQG is run as a module
# (python3 -m scripts.builder_quality_gate) or as a script
# (python3 scripts/builder_quality_gate.py).
_SCRIPT_DIR = Path(__file__).resolve().parent
_WORKSPACE_ROOT = _SCRIPT_DIR.parent
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from scripts.lib.test_scoping import scope_tests  # noqa: E402

_DEFAULT_WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", os.path.expanduser("~/.openclaw/workspace")))
WORKSPACE = _DEFAULT_WORKSPACE
REPORTS_DIR = WORKSPACE / "data" / "ops" / "quality_gate_reports"
# Baseline-diff toggle for ruff checks. Default ON; the CLI flips this via
# --no-baseline-diff. Module-level so the helper functions (which read it
# at call time) pick up the user's choice.
BASELINE_DIFF_ENABLED = True
# tsc typecheck config (card f3c668bf). The gate used to hardcode a 30s
# timeout and no NODE_OPTIONS, so any large project OOMed at ~24s and
# failed the gate as an infra artifact (EXIT=134). These module globals
# default to values that keep tsc healthy on large monorepos (120s + 8GB
# heap) and are overridable per-invocation via env (BQG_TSC_TIMEOUT,
# BQG_TSC_NODE_OPTIONS, BQG_TSC_CONFIG) or CLI flags (--tsc-timeout,
# --tsc-node-options, --tsconfig). When TSC_CONFIG is set the gate runs
# ONE `tsc -p <scoped-tsconfig> --noEmit` invocation rather than the
# per-tsconfig-root walk, so extensions projects with a slim tsconfig
# can typecheck without dragging in the full-project OOM artifact.
DEFAULT_TSC_TIMEOUT = int(os.environ.get("BQG_TSC_TIMEOUT", "120"))
DEFAULT_TSC_NODE_OPTIONS = os.environ.get(
    "BQG_TSC_NODE_OPTIONS", "--max-old-space-size=8192"
)
DEFAULT_TSC_CONFIG = os.environ.get("BQG_TSC_CONFIG")  # None = auto-discover
TSC_TIMEOUT = DEFAULT_TSC_TIMEOUT
TSC_NODE_OPTIONS = DEFAULT_TSC_NODE_OPTIONS
TSC_CONFIG = DEFAULT_TSC_CONFIG


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# GIT_* plumbing env vars that the pre-commit hook sets for its own
# git operations. These leak into every subprocess we spawn and would
# override the ``-C`` flag on git commands, plus they let a downstream
# subprocess (notably pytest fixtures) write to the wrong repo — the
# Aug 14 ``core.bare=true`` corruption vector was exactly this. We
# strip them on the pytest subprocess ONLY (BQES card 7f743d24).
# The gate's own git subprocesses continue to inherit the parent env
# unchanged — they pass ``cwd=workspace`` explicitly and the env
# context they need (``GIT_DIR`` for the worktree's own .git) is
# preserved.
#
# List completeness per BQES edge case 2 (var-list completeness):
# the eight entries below cover every plumbing var documented in
# ``git(1)`` that changes where git looks for its data or what config
# git evaluates, plus the four CONFIG_* scopes. The hook-side scrub
# already closes ``GIT_CONFIG_PARAMETERS``; this set restores QG-side
# hook-parity for the eighth var (card 7f627b0a — closes the
# ``git -c key=value`` leak vector: a fixture running ``git commit``
# under a QG-spawned pytest would otherwise inherit the parent
# hook's list-form config injection and mis-resolve repo config).
# New plumbing vars would be additive only; this list is the minimal
# set required to close the hook-leak.
GIT_VARS_TO_SCRUB_FROM_PYTEST = frozenset(
    (
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
    )
)


def scrubbed_pytest_env() -> dict[str, str]:
    """Return ``os.environ`` with hook-leaked ``GIT_*`` plumbing stripped.

    Returns a fresh dict so callers can pass it as ``env=`` to
    ``subprocess.run`` without mutating the parent's environment
    (BQES edge case 1: environment_isolation). The gate's own
    ``git -C`` subprocesses never call this helper and therefore
    keep their context.
    """
    return {k: v for k, v in os.environ.items() if k not in GIT_VARS_TO_SCRUB_FROM_PYTEST}


def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> dict:
    """Run a command and return structured result.

    Args:
        cmd: argv list to execute.
        cwd: Working directory. Defaults to ``WORKSPACE`` when None.
        timeout: Subprocess timeout in seconds.
        env: Optional environment mapping to pass to the subprocess.
            When None (the default), the subprocess inherits the
            parent's environment unchanged. Pass
            ``scrubbed_pytest_env()`` at pytest invocation sites to
            strip hook-leaked GIT_* plumbing (card 7f743d24).
    """
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd or WORKSPACE,
            timeout=timeout,
            env=env,
        )
        return {
            "cmd": " ".join(cmd),
            "exit_code": result.returncode,
            "stdout": result.stdout.strip()[:2000],
            "stderr": result.stderr.strip()[:2000],
            "passed": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "cmd": " ".join(cmd),
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Timeout after {timeout}s",
            "passed": False,
        }
    except FileNotFoundError:
        return {
            "cmd": " ".join(cmd),
            "exit_code": -1,
            "stdout": "",
            "stderr": "Command not found",
            "passed": False,
        }


def get_git_diff_files(workspace: Path | None = None) -> list[str]:
    """Get list of modified files from git diff.

    Uses main...HEAD (branch-scoped) to ensure only the current branch's
    changes are checked. This prevents the QG from compiling files
    belonging to sibling branches in linked worktrees.

    Falls back to --cached only when main...HEAD is empty AND we're
    on main (pre-commit hook context where files are staged but not
    yet committed).

    All git invocations run inside `workspace` (the worktree the user
    passed via --workspace). This is the fix for the Aug 2026 vacuous-
    PASS incident (card 3608f04c): previously the function ran git in
    the PROCESS CWD, so invoking the gate from the main checkout while
    pointing --workspace at a linked worktree silently checked zero
    files. We pass `cwd=workspace` to every git subprocess so the diff
    is enumerated against the user's intended repo, not wherever the
    Python process happened to start.

    Args:
        workspace: Repo root to run git inside. When None, defaults to
            the module-level WORKSPACE (which main() rebinds from the
            --workspace arg before this function is called). Tests can
            pass an explicit tmp_path-derived repo to drive isolation.

    Returns:
        List of repo-relative paths. May be empty when invoked on main
        with no staged changes — in that case main() applies the
        CWD-mismatch guard to refuse a silent PASS.
    """
    cwd = workspace if workspace is not None else WORKSPACE
    # Primary: branch-scoped diff (current branch vs main)
    result = subprocess.run(
        ["git", "diff", "main...HEAD", "--name-only"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )
    files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    if files:
        return files

    # Fallback: on main with staged files (pre-commit hook context)
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )
    staged = [f.strip() for f in result.stdout.splitlines() if f.strip()]

    # SAFETY GUARD: if we're on a feature branch and both diffs are empty,
    # something is wrong. Error rather than silently checking zero files
    # (which would produce a false-PASS on the quality gate).
    if not staged:
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
        current_branch = branch_result.stdout.strip()
        # Detached HEAD (rebase/bisect/tag) — allow pass, can't determine branch
        if current_branch == "HEAD":
            return staged
        if current_branch and current_branch != "main":
            print(
                f"ERROR: QG found no changed files on branch '{current_branch}' "
                "— possible git state issue. Aborting to prevent false-pass.",
                file=sys.stderr,
            )
            sys.exit(1)

    return staged


def check_py_compile(python_files: list[Path]) -> list[dict]:
    """Run py_compile on all modified Python files."""
    results = []
    for f in python_files:
        results.append(
            {
                "check": "py_compile",
                "file": str(f),
                **run_cmd(["python3", "-m", "py_compile", str(f)]),
            }
        )
    return results


def check_node(node_files: list[Path]) -> list[dict]:
    """Run node --check on all modified JS files."""
    results = []
    for f in node_files:
        results.append(
            {
                "check": "node_check",
                "file": str(f),
                **run_cmd(["node", "--check", str(f)]),
            }
        )
    return results


# --- Baseline-diff support (Sprint 056, card 33188149) ----------------------
#
# Findings present in the merge-base with main and unchanged in the working
# diff are reported as informational (counted, logged) but NEVER block.
# NEW findings (absent in baseline, present in current) block per the prior
# strict behavior. The default mode is baseline-diff ON; pass
# --no-baseline-diff to restore strict (every finding blocks).


def _fingerprint_ruff_finding(finding: dict) -> tuple:
    """Stable key for matching a ruff finding across baseline & current.

    Uses (code, message) — NOT filename (because ruff sets filename to
    "-" for stdin input) and NOT row (because line numbers shift with
    diff context).

    Match-by-(code, message) is intentionally coarse: within a single
    file we disambiguate using the diff-line map (_added_line_ranges)
    so two F401s in the same file at different rows don't all get
    lumped together — only findings that don't appear in the diff's
    added regions are classified as unchanged.
    """
    return (
        finding.get("code", ""),
        finding.get("message", ""),
    )


def _parse_ruff_json_output(stdout: str) -> list[dict]:
    """Parse a ruff JSON output blob into a list of finding dicts."""
    if not stdout or not stdout.strip():
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return data


def _run_ruff_json(target: Path | str) -> list[dict]:
    """Run ruff with --output-format=json and return parsed findings.

    Accepts either a filesystem path or '-' (for stdin).

    NOTE: we bypass the run_cmd() 2000-char stdout truncation (which is
    intended for human-readable summary output) by running ruff directly,
    because truncated JSON is invalid JSON and a single file with many
    findings can easily exceed 2 KiB of JSON.
    """
    cmd = ["ruff", "check", "--no-fix", "--output-format=json"]
    if target == "-":
        cmd.append("-")
    else:
        cmd.append(str(target))
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv (literal + caller-validated target)
            cmd,  # noqa: S607 — ruff on PATH
            capture_output=True,
            text=True,
            cwd=WORKSPACE,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    return _parse_ruff_json_output(result.stdout)


def _added_line_ranges(workspace: Path, merge_base: str, rel_path: str) -> list[tuple[int, int]]:
    """Return [(start_line, end_line), ...] of lines added in `rel_path` vs merge_base.

    Uses `git diff --unified=0` to extract precise added-line ranges in
    the CURRENT file. These ranges let us classify ruff findings as
    NEW (line is in an added range) vs unchanged (line is outside any
    added range).

    Returns [] on error or empty diff.
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv (literal + caller-validated merge_base/rel_path)
            ["git", "diff", "--unified=0", f"{merge_base}", "--", rel_path],  # noqa: S607 — fixed executable
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    ranges: list[tuple[int, int]] = []
    # Parse unified-diff @@ -a,b +c,d @@ headers
    for match in re.finditer(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", result.stdout):
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        end = start + max(count, 1) - 1
        ranges.append((start, end))
    return ranges


def _line_in_any_range(line: int, ranges: list[tuple[int, int]]) -> bool:
    """True if `line` falls within any (start, end) inclusive range."""
    return any(start <= line <= end for start, end in ranges)


def _classify_findings(
    baseline_findings: list[dict],
    current_findings: list[dict],
    added_ranges: list[tuple[int, int]] | None = None,
) -> dict:
    """Diff baseline vs current ruff findings.

    The classification is two-stage:
      1. Use (code, message) fingerprints to detect EXACT matches
         between baseline and current — these are unchanged.
      2. For remaining current findings, check whether their line
         falls in the diff's added ranges (from _added_line_ranges).
         Findings in added regions = NEW (blocking). Findings
         outside added regions but absent from baseline = ALSO NEW
         (could be a finding that exists at a slightly shifted line
         because of pure-context edits; conservative = block).

    Returns:
        {
            "new":       [finding, ...]  # blocking
            "informational": [finding, ...]  # counted only
            "fixed":     [finding, ...]  # in baseline, gone now
            "baseline_count": int
            "current_count": int
        }
    """
    baseline_keys = {_fingerprint_ruff_finding(f) for f in baseline_findings}
    current_keys = {_fingerprint_ruff_finding(f) for f in current_findings}
    baseline_by_key = {_fingerprint_ruff_finding(f): f for f in baseline_findings}
    current_by_key = {_fingerprint_ruff_finding(f): f for f in current_findings}

    if added_ranges is None:
        # No diff info — fall back to fingerprint-only classification.
        new_findings = [current_by_key[k] for k in current_keys - baseline_keys]
        informational = [current_by_key[k] for k in current_keys & baseline_keys]
    else:
        # Two-stage: matched fingerprints are informational; remaining
        # current findings are NEW only if in added ranges, else also
        # informational (line-shifted but pre-existing pattern).
        matched_keys = current_keys & baseline_keys
        informational_matched = [current_by_key[k] for k in matched_keys]
        unmatched_current = [current_by_key[k] for k in current_keys - baseline_keys]
        in_added = []
        outside_added = []
        for finding in unmatched_current:
            row = finding.get("location", {}).get("row", 0)
            if _line_in_any_range(row, added_ranges):
                in_added.append(finding)
            else:
                # Outside any added range — line-shifted pre-existing
                # pattern (e.g., context edit that pushed the warning
                # down a row). Treat as informational to avoid false
                # blocks on touch-only commits.
                outside_added.append(finding)
        new_findings = in_added
        informational = informational_matched + outside_added

    fixed = [baseline_by_key[k] for k in baseline_keys - current_keys]

    return {
        "new": new_findings,
        "informational": informational,
        "fixed": fixed,
        "baseline_count": len(baseline_findings),
        "current_count": len(current_findings),
    }


def _compute_merge_base(workspace: Path) -> str | None:
    """Return the merge-base commit hash of HEAD with main, or None on failure.

    Tries `git merge-base HEAD main` first (standard case). Falls back to
    `git merge-base HEAD origin/main` for worktrees where the local
    main ref is not present. Returns None if neither resolves — caller
    must treat that as a strict-fallback condition.
    """
    for ref in ("main", "origin/main"):
        try:
            result = subprocess.run(  # noqa: S603 — fixed argv, ref is a known literal
                ["git", "merge-base", "HEAD", ref],  # noqa: S607 — fixed executable
                capture_output=True,
                text=True,
                cwd=workspace,
                timeout=15,
            )
            sha = result.stdout.strip()
            if result.returncode == 0 and sha and len(sha) >= 7:
                return sha
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return None


def _baseline_blob(workspace: Path, merge_base: str, rel_path: str) -> str | None:
    """Return the contents of `<merge-base>:<rel_path>` or None if absent.

    Uses `git show <merge-base>:<path>` and returns None on:
      - file not present in baseline (empty stdout, exit 0/128)
      - subprocess failure
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, merge_base+rel_path are caller-validated
            ["git", "show", f"{merge_base}:{rel_path}"],  # noqa: S607 — fixed executable
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def check_ruff(python_files: list[Path]) -> list[dict]:
    """Run ruff lint on all modified Python files.

    Default behavior (baseline-diff ON): for each file, compute the
    merge-base with main, run ruff on the baseline blob, then on the
    current file. Findings that exist in the baseline and remain in
    the current file are counted as informational and never block.
    NEW findings (absent in baseline, present now) block per the
    pre-existing strict contract.

    Pass --no-baseline-diff to restore strict mode (every finding
    blocks).

    Edge cases:
      - merge-base unresolvable: strict fallback + WARN logged
      - file not present in baseline: all current findings are NEW
      - file unchanged vs baseline: skip ruff entirely (no diff to lint)
    """
    if not python_files:
        return []

    if not BASELINE_DIFF_ENABLED:
        # Strict mode — every finding blocks.
        results = []
        for f in python_files:
            results.append(
                {
                    "check": "ruff",
                    "mode": "strict",
                    "file": str(f),
                    **run_cmd(["ruff", "check", str(f), "--no-fix"]),
                }
            )
        return results

    # Baseline-diff mode
    merge_base = _compute_merge_base(WORKSPACE)
    if merge_base is None:
        # Strict fallback + WARN
        results = []
        for f in python_files:
            results.append(
                {
                    "check": "ruff",
                    "mode": "strict_fallback",
                    "file": str(f),
                    "baseline_diff": {
                        "merge_base": None,
                        "warning": "merge-base with main unresolvable — falling back to strict mode",
                    },
                    **run_cmd(["ruff", "check", str(f), "--no-fix"]),
                }
            )
        return results

    results = []
    for f in python_files:
        # Resolve path relative to workspace so the git show command
        # addresses the right tree.
        try:
            rel_path = str(f.resolve().relative_to(WORKSPACE.resolve()))
        except ValueError:
            rel_path = str(f)

        # Diff baseline vs current at the bytes level — skip if unchanged.
        baseline_bytes = _baseline_blob(WORKSPACE, merge_base, rel_path)
        if baseline_bytes is None:
            # File not in baseline → every current finding is NEW
            current_findings = _run_ruff_json(f)
            new_findings = current_findings
            informational = []
            fixed = []
            baseline_count = 0
        else:
            try:
                current_bytes = f.read_text()
            except OSError:
                # Unreadable working file — fall back to strict on this file
                results.append(
                    {
                        "check": "ruff",
                        "mode": "baseline_diff",
                        "file": str(f),
                        "baseline_diff": {
                            "merge_base": merge_base,
                            "warning": "working file unreadable — strict check applied",
                        },
                        **run_cmd(["ruff", "check", str(f), "--no-fix"]),
                    }
                )
                continue

            if baseline_bytes == current_bytes:
                # File unchanged vs baseline — skip lint entirely.
                results.append(
                    {
                        "check": "ruff",
                        "mode": "baseline_diff",
                        "file": str(f),
                        "passed": True,
                        "skipped": True,
                        "stdout": "file unchanged vs baseline — lint skipped",
                        "baseline_diff": {
                            "merge_base": merge_base,
                            "skipped_reason": "unchanged_vs_baseline",
                        },
                    }
                )
                continue

            # Lint the baseline blob via stdin so we don't write a temp file.
            # Use --stdin-filename so findings reference the same path as the
            # current-file findings (fingerprint-based classification).
            baseline_findings = _baseline_ruff_for_blob(baseline_bytes, filename=str(f))
            current_findings = _run_ruff_json(f)
            added_ranges = _added_line_ranges(WORKSPACE, merge_base, rel_path)
            classified = _classify_findings(baseline_findings, current_findings, added_ranges)
            new_findings = classified["new"]
            informational = classified["informational"]
            fixed = classified["fixed"]
            baseline_count = classified["baseline_count"]

        passed = len(new_findings) == 0
        # Render the report the way the strict path would, so downstream
        # tooling that reads stderr/stdout sees consistent shape.
        summary_lines = [
            f"baseline: {baseline_count} finding(s) (informational)",
            f"informational unchanged: {len(informational)}",
            f"new (blocking): {len(new_findings)}",
            f"fixed by change: {len(fixed)}",
        ]
        stderr = ""
        if not passed:
            for finding in new_findings:
                loc = finding.get("location", {})
                stderr += f"{rel_path}:{loc.get('row', 0)}:{loc.get('column', 0)}: "
                stderr += f"{finding.get('code', '')} {finding.get('message', '')}\n"
        results.append(
            {
                "check": "ruff",
                "mode": "baseline_diff",
                "file": str(f),
                "passed": passed,
                "stdout": "\n".join(summary_lines),
                "stderr": stderr,
                "baseline_diff": {
                    "merge_base": merge_base,
                    "baseline_count": baseline_count,
                    "informational_count": len(informational),
                    "new_count": len(new_findings),
                    "fixed_count": len(fixed),
                },
            }
        )
    return results


def _baseline_ruff_for_blob(blob_contents: str, filename: str | None = None) -> list[dict]:
    """Run ruff on a baseline blob by piping it through stdin.

    Returns parsed findings. On any subprocess error returns [].

    `filename` is passed via --stdin-filename so the finding records
    carry the same filename as the current file's findings (otherwise
    ruff sets filename to "-" for stdin input, which would defeat the
    fingerprint-based classifier).

    NOTE: runs ruff directly to bypass run_cmd()'s 2000-char stdout
    truncation (truncated JSON is invalid JSON).
    """
    cmd = ["ruff", "check", "--no-fix", "--output-format=json"]
    if filename:
        cmd += ["--stdin-filename", filename]
    cmd.append("-")
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv + caller-provided blob
            cmd,  # noqa: S607 — ruff on PATH
            input=blob_contents,
            capture_output=True,
            text=True,
            cwd=WORKSPACE,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    return _parse_ruff_json_output(result.stdout)


_MYPY_FINDING_RE = re.compile(
    r"^(?P<path>[^:]+):(?P<row>\d+)(?::\d+)?:\s*(?P<severity>error|warning|note):\s*(?P<message>.*?)(?:\s{2}\[(?P<code>[^\]]+)\])?$")


def _parse_mypy_output(stdout: str) -> list[dict]:
    """Parse mypy stdout into ruff-shaped finding dicts.

    Shape: {"code", "message", "location": {"row", "column"}} so the
    shared _classify_findings() machinery can diff them against a
    baseline run without any special-casing.
    """
    findings = []
    for line in stdout.splitlines():
        m = _MYPY_FINDING_RE.match(line.strip())
        if not m or m.group("severity") != "error":
            continue
        findings.append(
            {
                "code": m.group("code") or "",
                "message": m.group("message"),
                "location": {"row": int(m.group("row")), "column": 0},
            }
        )
    return findings


def _run_mypy_capture(target: Path | str) -> tuple[int, str]:
    """Run mypy on a path, return (returncode, stdout). Never raises."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv + caller-validated target
            ["mypy", str(target), "--ignore-missing-imports", "--no-error-summary"],  # noqa: S607 — mypy on PATH
            capture_output=True,
            text=True,
            cwd=WORKSPACE,
            timeout=120,
        )
        return result.returncode, result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return -1, str(exc)


def _baseline_mypy_for_blob(blob_contents: str, filename: str) -> tuple[list[dict], str | None]:
    """Run mypy on a baseline blob written to a temp file.

    Returns (findings, warning). `warning` is non-None when the run
    failed outright (timeout/binary missing) — callers surface it;
    findings may still be empty legitimately (clean baseline).
    """
    tmp_dir = None
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="bqg_mypy_baseline_"))
        tmp_file = tmp_dir / Path(filename).name
        tmp_file.write_text(blob_contents)
        code, stdout = _run_mypy_capture(tmp_file)
        if code == -1:
            return [], f"baseline mypy run failed: {stdout}"
        return _parse_mypy_output(stdout), None
    except OSError as exc:
        return [], f"baseline temp-file write failed: {exc}"
    finally:
        if tmp_dir is not None:
            for child in tmp_dir.iterdir():
                child.unlink(missing_ok=True)
            tmp_dir.rmdir()


def check_mypy(python_files: list[Path]) -> list[dict]:
    """Run mypy type-check on all modified Python files.

    Baseline-diff mode (default) mirrors check_ruff(): findings present
    in the merge-base blob and unchanged now are informational and never
    block; only NEW findings block (card af96a2fe — standalone --git-diff
    used to fail on pre-existing baseline errors while the hook, which
    passes --skip-mypy, sailed through). Failure detail is populated
    from mypy stdout (mypy writes findings to stdout, never stderr —
    the historic "empty message" blocker).
    """
    if not python_files:
        return []

    def _strict(f: Path, mode: str = "strict", extra: dict | None = None) -> dict:
        code, stdout = _run_mypy_capture(f)
        passed = code == 0
        result = {
            "check": "mypy",
            "mode": mode,
            "file": str(f),
            "cmd": "mypy --ignore-missing-imports --no-error-summary",
            "exit_code": code,
            "stdout": stdout.strip()[:2000],
            "stderr": "",  # populated below for failures
            "passed": passed,
        }
        if extra:
            result.update(extra)
        if not passed and not result["stderr"]:
            # mypy reports findings on stdout — surface them where the
            # summary printer looks, so failures are never "empty message".
            result["stderr"] = stdout.strip()[:2000] or f"mypy exited {code} with no output"
        return result

    if not BASELINE_DIFF_ENABLED:
        return [_strict(f) for f in python_files]

    merge_base = _compute_merge_base(WORKSPACE)
    if merge_base is None:
        return [
            _strict(
                f,
                mode="strict_fallback",
                extra={
                    "baseline_diff": {
                        "merge_base": None,
                        "warning": "merge-base with main unresolvable — falling back to strict mode",
                    }
                },
            )
            for f in python_files
        ]

    results = []
    for f in python_files:
        try:
            rel_path = str(f.resolve().relative_to(WORKSPACE.resolve()))
        except ValueError:
            rel_path = str(f)

        baseline_bytes = _baseline_blob(WORKSPACE, merge_base, rel_path)
        if baseline_bytes is None:
            # File not in baseline → every current finding is NEW.
            _, stdout = _run_mypy_capture(f)
            current_findings = _parse_mypy_output(stdout)
            new_findings, informational = current_findings, []
            baseline_count, fixed, warning = 0, [], None
        else:
            try:
                current_bytes = f.read_text()
            except OSError:
                results.append(
                    _strict(
                        f,
                        mode="baseline_diff",
                        extra={
                            "baseline_diff": {
                                "merge_base": merge_base,
                                "warning": "working file unreadable — strict check applied",
                            }
                        },
                    )
                )
                continue

            if baseline_bytes == current_bytes:
                # Unchanged vs baseline — baseline findings (if any) are
                # pre-existing and informational; nothing new can exist.
                _, stdout = _run_mypy_capture(f)
                findings = _parse_mypy_output(stdout)
                results.append(
                    {
                        "check": "mypy",
                        "mode": "baseline_diff",
                        "file": str(f),
                        "passed": True,
                        "stdout": (
                            f"file unchanged vs baseline — {len(findings)} "
                            "pre-existing finding(s) informational"
                        ),
                        "stderr": "",
                        "baseline_diff": {
                            "merge_base": merge_base,
                            "skipped_reason": "unchanged_vs_baseline",
                            "informational_count": len(findings),
                            "new_count": 0,
                        },
                    }
                )
                continue

            baseline_findings, warning = _baseline_mypy_for_blob(baseline_bytes, str(f))
            _, stdout = _run_mypy_capture(f)
            current_findings = _parse_mypy_output(stdout)
            added_ranges = _added_line_ranges(WORKSPACE, merge_base, rel_path)
            classified = _classify_findings(baseline_findings, current_findings, added_ranges)
            new_findings = classified["new"]
            informational = classified["informational"]
            fixed = classified["fixed"]
            baseline_count = classified["baseline_count"]

        passed = len(new_findings) == 0
        summary_lines = [
            f"baseline: {baseline_count} error(s) (informational)",
            f"informational unchanged: {len(informational)}",
            f"new (blocking): {len(new_findings)}",
            f"fixed by change: {len(fixed)}",
        ]
        stderr = ""
        if not passed:
            for finding in new_findings:
                stderr += (
                    f"{rel_path}:{finding['location']['row']}: "
                    f"{finding.get('code', '')} {finding.get('message', '')}\n"
                )
        baseline_block = {
            "merge_base": merge_base,
            "baseline_count": baseline_count,
            "informational_count": len(informational),
            "new_count": len(new_findings),
            "fixed_count": len(fixed),
        }
        if warning:
            baseline_block["warning"] = warning
        results.append(
            {
                "check": "mypy",
                "mode": "baseline_diff",
                "file": str(f),
                "passed": passed,
                "stdout": "\n".join(summary_lines),
                "stderr": stderr,
                "baseline_diff": baseline_block,
            }
        )
    return results


# Eslint config filenames recognised by check_eslint (card e16c2511).
# Listed in priority order — flat-config names (eslint.config.{js,mjs,cjs})
# come first because they're the modern format that v9+ prefers; the
# legacy .eslintrc.* forms remain supported for older projects.
_ESLINT_CONFIG_FILENAMES: tuple[str, ...] = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yaml",
    ".eslintrc.yml",
)


def _find_nearest_eslint_project(start: Path) -> Path | None:
    """Walk upward from `start` looking for the nearest directory that
    owns the eslint run for files underneath it.

    A directory "owns" the eslint run when EITHER:

    1. An eslint config file (flat-config or legacy .eslintrc.*) lives
       directly in that directory — that directory is the project root,
       and the project's node_modules/.bin/eslint is the binary to use.
    2. No eslint config exists but a package.json + node_modules/eslint
       do (i.e. eslint is installed as a dep but the project hasn't
       published a config) — the directory containing node_modules/eslint
       is the project root.

    Resolution stops at the first ancestor that matches either condition.
    Returns the project root (Path), or None when no eslint project is
    found in any ancestor of `start` (caller should skip with informative
    stderr).

    Note: this deliberately ignores the WORKSPACE-relative root when no
    config exists anywhere in the chain — running eslint from a directory
    with no config silently applies the user's home-dir global config,
    which masks project issues and contaminates gate reports (card e16c2511).
    """
    cur = start.resolve() if start.exists() else start
    for parent in [cur, *cur.parents]:
        if any((parent / name).is_file() for name in _ESLINT_CONFIG_FILENAMES):
            return parent
        pkg_json = parent / "package.json"
        if pkg_json.is_file() and (parent / "node_modules" / "eslint").is_dir():
            return parent
    return None


def check_eslint(js_files: list[Path]) -> list[dict]:
    """Run eslint on each modified JS/TS file using the project's
    locally-pinned eslint binary (card e16c2511).

    For every file we walk up from its directory to find the nearest
    eslint project root (a directory that either has an eslint config
    or has eslint installed in node_modules). We then run
    ``<project>/node_modules/.bin/eslint <file>`` from that project
    root so the project's pinned eslint version is used. This avoids
    the v9-vs-v10 pin trap where the host's npx cache pulls eslint v10
    and trips plugins (e.g. eslint-plugin-react@7.x bundled in
    eslint-config-next@16.2.6) that rely on v9-only shims.

    Files outside any eslint project are reported as skipped with an
    informative stderr rather than silently falling back to the host
    eslint config — that fallback was the root cause of the
    contextOrFilename.getFilename TypeError documented in d5de4b76.

    Args:
        js_files: Repo-relative paths (absolute Path objects) for each
            JS/TS file under review.

    Returns:
        One result dict per input file (plus a single skipped result
        for the empty-input case). Each result preserves the
        (check, file, cmd, exit_code, stdout, stderr, passed) shape
        consumed by downstream tooling.
    """
    if not js_files:
        return [
            {
                "check": "eslint",
                "file": "(none in changed set)",
                "cmd": "eslint",
                "exit_code": 0,
                "stdout": "",
                "stderr": "No JS/TS files in changed set — skipped",
                "passed": True,
                "skipped": True,
            }
        ]

    results: list[dict] = []
    for f in js_files:
        project_root = _find_nearest_eslint_project(f.parent)
        if project_root is None:
            results.append(
                {
                    "check": "eslint",
                    "file": str(f),
                    "cmd": "eslint",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": (
                        f"No eslint project found for {f} — skipped "
                        "(no eslint config + no node_modules/eslint in any ancestor)"
                    ),
                    "passed": True,
                    "skipped": True,
                }
            )
            continue

        eslint_bin = project_root / "node_modules" / ".bin" / "eslint"
        if not eslint_bin.is_file():
            results.append(
                {
                    "check": "eslint",
                    "file": str(f),
                    "cmd": "eslint",
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": (
                        f"eslint config found at {project_root} but "
                        f"{eslint_bin} is missing — run npm ci in that project"
                    ),
                    "passed": False,
                    "skipped": False,
                }
            )
            continue

        # eslint is invoked with cwd=project_root, so the file argument
        # must be relative to that root — callers pass repo-relative paths
        # like "projects/mission-control/src/types/cron.ts", which need to
        # be rewritten to "src/types/cron.ts" before being handed to eslint
        # (otherwise eslint v9 reports "No files matching the pattern").
        # Falls back to the absolute path when the file lives outside the
        # discovered project_root (rare, but preserves old semantics).
        try:
            file_arg = str(f.resolve().relative_to(project_root.resolve()))
        except ValueError:
            file_arg = str(f)

        results.append(
            {
                "check": "eslint",
                "file": str(f),
                **run_cmd([str(eslint_bin), file_arg], cwd=str(project_root)),
            }
        )
    return results


def _find_tsconfig(start: Path) -> Path | None:
    """Walk upward from `start` looking for the nearest tsconfig.json.

    Returns the directory containing tsconfig.json, or None if not found.
    """
    cur = start.resolve() if start.exists() else start
    for parent in [cur, *cur.parents]:
        candidate = parent / "tsconfig.json"
        if candidate.is_file():
            return parent
    return None


def _tsc_subprocess_env() -> dict[str, str] | None:
    """Build the env dict for a tsc subprocess (card f3c668bf).

    Inherits the parent environment and overlays ``NODE_OPTIONS`` so tsc
    (a node process) gets the configured heap size. Returns ``None`` when
    no NODE_OPTIONS override is set — run_cmd then inherits parent env
    unchanged, preserving byte-identical behavior for callers that
    explicitly opted out of the default 8GB heap.
    """
    if not TSC_NODE_OPTIONS:
        return None
    return {**os.environ, "NODE_OPTIONS": TSC_NODE_OPTIONS}


def check_tsc(ts_files: list[Path]) -> list[dict]:
    """Run `tsc --noEmit` on modified TypeScript files if a tsconfig.json exists.

    Walks up from each file's directory to locate the nearest tsconfig.json.
    If none is found across the set, the whole check is skipped (single result).
    Otherwise, runs `npx tsc --noEmit` from each tsconfig root with a
    ``TSC_TIMEOUT`` per tsconfig root timeout, passing the modified
    files in that root as explicit arguments so tsc typechecks only
    the changed files (and their transitive imports) instead of the
    whole project (card b754f6b0 — the old default ran project-wide
    tsc and OOM-killed the server at ~24GB on large monorepos). We
    keep one entry per file for parity with the other per-file checks.

    When ``TSC_CONFIG`` is set (card f3c668bf), runs ONE
    ``npx tsc -p <scoped-tsconfig> --noEmit`` against that config instead
    of the per-file walk — this is the "scoped-tsconfig mode" extensions
    builds use to avoid the full-project OOM artifact. The scoped mode
    is the documented escape hatch for callers that genuinely need
    project-wide typecheck (e.g. an extensions repo where the change
    set is the whole project). All tsc subprocesses inherit
    ``NODE_OPTIONS=TSC_NODE_OPTIONS`` via the env overlay in
    ``_tsc_subprocess_env()`` so the configured V8 heap reaches tsc.

    The configurable timeout / NODE_OPTIONS / tsconfig values are recorded
    on every result entry under ``tsc_timeout``, ``tsc_node_options``, and
    ``tsc_mode`` so gate reports make the active config first-class — the
    pre-change behavior (hardcoded 30s, no NODE_OPTIONS, per-file walk)
    surfaces as ``tsc_mode=auto-discovered tsc_timeout=30
    tsc_node_options=null`` for backward-compatibility audits.
    """
    ts_files = [f for f in ts_files if f.suffix in (".ts", ".tsx")]
    if not ts_files:
        return [
            {
                "check": "tsc_typecheck",
                "file": "(none in changed set)",
                "cmd": "tsc --noEmit",
                "exit_code": 0,
                "stdout": "",
                "stderr": "No .ts/.tsx files in changed set — skipped",
                "passed": True,
                "skipped": True,
                "tsc_timeout": TSC_TIMEOUT,
                "tsc_node_options": TSC_NODE_OPTIONS,
                "tsc_mode": (
                    "scoped" if TSC_CONFIG is not None else "auto-discovered"
                ),
            }
        ]

    # Scoped-tsconfig mode (card f3c668bf): one tsc invocation against the
    # user-supplied tsconfig, every input file gets a per-file result entry
    # that all share the same cmd_result. Files are not required to live
    # under TSC_CONFIG — the scoped tsconfig is its own root, and tsc
    # itself decides what files to include.
    if TSC_CONFIG is not None:
        scoped_root = Path(TSC_CONFIG).resolve().parent
        cmd_result = run_cmd(
            ["npx", "tsc", "-p", TSC_CONFIG, "--noEmit"],
            cwd=scoped_root,
            timeout=TSC_TIMEOUT,
            env=_tsc_subprocess_env(),
        )
        results: list[dict] = []
        for f in ts_files:
            entry = {
                "check": "tsc_typecheck",
                "file": str(f),
                "tsconfig": TSC_CONFIG,
                "tsc_timeout": TSC_TIMEOUT,
                "tsc_node_options": TSC_NODE_OPTIONS,
                "tsc_mode": "scoped",
                **cmd_result,
            }
            results.append(entry)
        return results

    # Auto-discover mode: find tsconfig roots for each file (unique roots
    # only). Each tsconfig root becomes one tsc invocation; every file
    # under that root gets a result entry that shares the same cmd_result.
    file_root_pairs: list[tuple[Path, Path | None]] = []
    for f in ts_files:
        root = _find_tsconfig(f.parent)
        file_root_pairs.append((f, root))

    roots = {root for _, root in file_root_pairs if root is not None}
    if not roots:
        return [
            {
                "check": "tsc_typecheck",
                "file": ", ".join(str(f) for f in ts_files),
                "cmd": "tsc --noEmit",
                "exit_code": 0,
                "stdout": "",
                "stderr": "No tsconfig.json found for any .ts/.tsx file — skipped",
                "passed": True,
                "skipped": True,
                "tsc_timeout": TSC_TIMEOUT,
                "tsc_node_options": TSC_NODE_OPTIONS,
                "tsc_mode": "auto-discovered",
            }
        ]

    # One tsc invocation per unique tsconfig root (avoids redundant work).
    # Each file under that root gets a result entry so callers see per-file outcomes.
    # Files without a tsconfig (orphans in a mixed set) get an explicit skipped
    # entry so that `passed ∪ skipped == input set` — every input file has a result.
    root_files: dict[Path, list[Path]] = {}
    results = []
    for f, root in file_root_pairs:
        if root is None:
            results.append(
                {
                    "check": "tsc_typecheck",
                    "file": str(f),
                    "cmd": "tsc --noEmit",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "No tsconfig.json found — skipped",
                    "passed": True,
                    "skipped": True,
                    "tsc_timeout": TSC_TIMEOUT,
                    "tsc_node_options": TSC_NODE_OPTIONS,
                    "tsc_mode": "auto-discovered",
                }
            )
            continue
        root_files.setdefault(root, []).append(f)

    for root, files_in_root in root_files.items():
        # Scope tsc to the modified files in this root instead of
        # typechecking the entire project under `root`. Passing the file
        # list as tsc arguments bypasses tsconfig.json's `include`/`files`
        # arrays — tsc only checks the explicit files (plus their
        # transitive imports). This is the fix for the
        # ``builder_quality_gate.py --git-diff`` 24GB OOM artifact
        # (card b754f6b0): the old invocation ran project-wide tsc
        # from every tsconfig root in the change set and OOMed the
        # server on large monorepos. Every f in `files_in_root`
        # is already grouped under the tsconfig root that owns it
        # (see ``_find_tsconfig(f.parent)`` above), so absolute paths
        # resolve correctly from ``cwd=root``.
        cmd = ["npx", "tsc", "--noEmit"] + [
            str(f) for f in files_in_root
        ]
        cmd_result = run_cmd(
            cmd,
            cwd=root,
            timeout=TSC_TIMEOUT,
            env=_tsc_subprocess_env(),
        )
        for f in files_in_root:
            entry = {
                "check": "tsc_typecheck",
                "file": str(f),
                "tsconfig": str(root / "tsconfig.json"),
                "tsc_timeout": TSC_TIMEOUT,
                "tsc_node_options": TSC_NODE_OPTIONS,
                "tsc_mode": "auto-discovered",
                **cmd_result,
            }
            results.append(entry)

    return results


def check_json_validity(json_files: list[Path]) -> list[dict]:
    """Validate JSON files."""
    results = []
    for f in json_files:
        result = run_cmd(["python3", "-c", f"import json; json.load(open('{f}'))"])
        results.append(
            {
                "check": "json_valid",
                "file": str(f),
                **result,
            }
        )
    return results


# Heuristic: which JSON files count as "affirmation" files?
# - Any JSON under a path containing "subliminal" (e.g. src/subliminals/scripts/)
# - Any JSON whose filename contains "affirm" (e.g. track_affirmations.json)
_AFFIRMATION_PATH_RE = re.compile(
    r"subliminal|/affirmation|affirmation",
    re.IGNORECASE,
)


def _is_affirmation_file(path: Path) -> bool:
    """Return True if a JSON file looks like an affirmation source file."""
    s = str(path)
    if "/subliminal" in s.lower():
        return True
    if "affirmation" in s.lower():
        return True
    return "affirm" in path.name.lower()


def check_affirmations(json_files: list[Path]) -> list[dict]:
    """Run the affirmation linter against any affirmation JSON files in the changed set.

    This is a soft gate: lint warnings do not block, but lint errors do.
    Returns a single result dict:
      - "skipped" if no affirmation files are present
      - "passed" if linter exits 0
      - "failed" if linter exits non-zero (parse errors, violations, etc.)
    """
    affirmation_files = [f for f in json_files if _is_affirmation_file(f)]
    if not affirmation_files:
        return [
            {
                "check": "affirmations",
                "file": "(none in changed set)",
                "cmd": "lint_affirmations.py",
                "exit_code": 0,
                "stdout": "",
                "stderr": "No affirmation files in changed set — skipped",
                "passed": True,
                "skipped": True,
            }
        ]

    script_path = WORKSPACE / "scripts" / "lint_affirmations.py"
    if not script_path.exists():
        return [
            {
                "check": "affirmations",
                "file": str(script_path),
                "cmd": "lint_affirmations.py",
                "exit_code": 1,
                "stdout": "",
                "stderr": f"lint_affirmations.py not found at {script_path}",
                "passed": False,
            }
        ]

    cmd = ["python3", str(script_path)] + [str(f) for f in affirmation_files]
    result = run_cmd(cmd, cwd=WORKSPACE, timeout=60)
    return [
        {
            "check": "affirmations",
            "file": ", ".join(str(f) for f in affirmation_files),
            "cmd": "lint_affirmations.py",
            **result,
        }
    ]


def check_path_constants(python_files: list[Path]) -> list[dict]:
    """Check that path constants are consistent across files.

    Finds constants ending in _PATH and verifies they don't point to
    different physical files across modules.

    Card 1b6fa73b: same-name different-value constants that are MODULE-LOCAL
    (never referenced by a non-defining module in the checked set) emit a
    SOFT warning (passed=True, warning=True) instead of a hard failure.
    Cross-module conflicts (constant referenced/imported by a non-defining
    file) remain hard failures.
    """
    results = []
    path_pattern = re.compile(r'^(\w+_PATH)\s*=\s*(?:Path\(["\'])([^"\']+)', re.MULTILINE)

    all_constants: dict[str, dict[str, str]] = {}  # constant_name -> {file: value}
    file_contents: dict[str, str] = {}  # file path -> content, for locality scan

    for f in python_files:
        try:
            content = f.read_text()
        except Exception:  # noqa: S112
            continue
        file_contents[str(f)] = content
        for match in path_pattern.finditer(content):
            const_name, path_value = match.group(1), match.group(2)
            if const_name not in all_constants:
                all_constants[const_name] = {}
            all_constants[const_name][str(f)] = path_value

    for const_name, file_map in all_constants.items():
        if len(file_map) > 1:
            unique_values = set(file_map.values())
            if len(unique_values) > 1:
                definers = set(file_map.keys())
                # Module-locality: does any NON-defining file in the checked
                # set reference the constant name (import or bare use)?
                # Defining files trivially contain the name (the assignment);
                # only external references make this a cross-module conflict.
                referenced_externally = any(
                    const_name in content
                    for fp, content in file_contents.items()
                    if fp not in definers
                )
                if referenced_externally:
                    results.append(
                        {
                            "check": "path_constant_consistency",
                            "constant": const_name,
                            "files": file_map,
                            "passed": False,
                            "stderr": f"Path constant {const_name} has different values across files: {file_map}",
                        }
                    )
                else:
                    results.append(
                        {
                            "check": "path_constant_consistency",
                            "constant": const_name,
                            "files": file_map,
                            "passed": True,
                            "warning": True,
                            "stderr": (
                                f"SOFT WARNING: Path constant {const_name} has different "
                                f"values across files, but it is module-local (no "
                                f"cross-module reference found): {file_map}"
                            ),
                        }
                    )

    if not results:
        results.append(
            {
                "check": "path_constant_consistency",
                "passed": True,
                "stdout": "All path constants consistent",
            }
        )

    return results


def check_unwired_functions(python_files: list[Path]) -> list[dict]:
    """Detect functions defined but never called in the workspace.

    Only checks functions defined in the modified files — searches for
    their usage across scripts/ and src/.
    """
    results = []
    def_pattern = re.compile(r"^def (\w+)\(", re.MULTILINE)

    # Dunder methods and common entry points are exempt
    exempt = {
        "__init__",
        "__main__",
        "__enter__",
        "__exit__",
        "__str__",
        "__repr__",
        "__eq__",
        "__hash__",
        "__len__",
        "__iter__",
        "__getitem__",
        "__setitem__",
        "__call__",
        "__contains__",
        "main",
        "setUp",
        "tearDown",
        "setUpClass",
        "tearDownClass",
    }

    for f in python_files:
        # Skip test files — test functions are discovered by pytest, not called directly
        if "tests/" in str(f) or "test" in f.name:
            continue

        try:
            content = f.read_text()
        except Exception:  # noqa: S112
            continue

        funcs = def_pattern.findall(content)
        for func in funcs:
            if func in exempt or func.startswith("_") or func.startswith("test_"):
                continue

            # Search for usage across scripts/, src/, tests/, and skills/.
            # --include keeps matches to .py files (skips the markdown
            # reference files that live inside skills/).
            grep_result = subprocess.run(  # noqa: S603
                ["grep", "-rn", "--include=*.py", func, "scripts/", "src/", "tests/", "skills/"],  # noqa: S607
                capture_output=True,
                text=True,
                cwd=WORKSPACE,
                timeout=30,
            )
            # Count lines that aren't the definition itself
            usage_lines = [
                line
                for line in grep_result.stdout.splitlines()
                if f"def {func}(" not in line  # exclude the definition
                and f"def {func} (" not in line
            ]

            if len(usage_lines) == 0:
                results.append(
                    {
                        "check": "unwired_function",
                        "function": func,
                        "file": str(f),
                        "passed": False,
                        "stderr": f"Function {func}() defined in {f} but never called in scripts/, src/, or tests/",
                    }
                )

    if not results:
        results.append(
            {
                "check": "unwired_function",
                "passed": True,
                "stdout": "All functions wired",
            }
        )

    return results


def check_targeted_tests(python_files: list[Path]) -> list[dict]:
    """Run targeted tests for modified modules via shared scope_tests().

    Batches all matching test files into a single pytest invocation to
    amortise collection overhead (~8s per run with 19.5k tests). Falls
    back to per-file runs only if the batch fails, so individual file
    results can still be reported.
    """
    results: list[dict[str, Any]] = []
    test_files = scope_tests(python_files, WORKSPACE)
    if not test_files:
        return results

    # Batch: single pytest call for all scoped test files
    test_paths = [str(tf) for tf in test_files]
    # Card 7f743d24: scrub hook-leaked GIT_* plumbing so pytest
    # fixtures can't ``git config`` the main repo (Aug 14
    # core.bare=true corruption vector).
    batch_result = run_cmd(
        ["python3", "-m", "pytest", *test_paths, "-q", "--tb=short"],
        timeout=180,
        env=scrubbed_pytest_env(),
    )
    if batch_result.get("passed"):
        # Batch passed — report each file as passed
        for tf in test_files:
            results.append(
                {
                    "check": "targeted_test",
                    "test_file": str(tf),
                    "passed": True,
                    "skipped": False,
                    "output": batch_result.get("output", ""),
                }
            )
    else:
        # Batch failed — re-run per file for granular reporting
        for tf in test_files:
            # Card 7f743d24: same scrub as the batch invocation
            # above — per-file fallbacks inherit the same hook
            # env, so they need the same protection.
            result = run_cmd(
                ["python3", "-m", "pytest", str(tf), "-q", "--tb=short"],
                timeout=120,
                env=scrubbed_pytest_env(),
            )
            results.append(
                {
                    "check": "targeted_test",
                    "test_file": str(tf),
                    **result,
                }
            )
    return results


# ── stdin transport + chunking helpers (Sprint reina-2026-08-21-015, ───────
# ── card 6c7a7613)                                                          ─
#
# Background: the pre-commit hook Part 4 previously passed the full
# staged file list as a single ``--files "$staged_files"`` argv element.
# At >1,500 files (~87B avg paths) it exceeded ``MAX_ARG_STRLEN`` (128 KB)
# and triggered ``execve`` E2BIG / OOM-kill (root cause of 3× BYPASS_QG
# in sprint reina-2026-08-20-005). The fix mirrors Part 5's forensic
# logger pattern: pipe staged paths via stdin, which (a) sidesteps argv
# length limits entirely and (b) avoids shell quoting bugs on paths
# with spaces / quotes / backslashes.
#
# Selection logic is unchanged: ``_parse_files_stdin_input()`` and the
# existing ``_parse_files_arg()`` (extracted from the prior main() body)
# both produce the same logical file list — comma-separated, newline-
# separated, or stdin-piped. Internal iteration is chunked at ≤200 files
# per batch via ``_chunked_apply()`` so memory stays bounded for huge
# diffs without changing the selected-path-set or the per-check output.


_CHUNK_SIZE = 200  # maximum files per internal iteration batch


def _parse_files_arg(files_arg: str) -> list[str]:
    """Parse the legacy ``--files`` argument into a list of path strings.

    Backward-compat contract: identical to the pre-change implementation
    (regex split on ``r\"[,\\n]+\"`` + per-token ``.strip()`` + empty
    filter). A comma-separated, newline-separated, or mixed input all
    produce the same list as before. Whitespace-only tokens are dropped.

    Returns:
        List of non-empty path strings in input order.
    """
    if not files_arg:
        return []
    raw_tokens = re.split(r"[,\n]+", files_arg)
    return [tok.strip() for tok in raw_tokens if tok.strip()]


def _parse_files_stdin_input(payload: str) -> list[str]:
    """Parse a stdin payload into a list of path strings.

    The pre-commit hook pipes staged paths via stdin (newline-separated).
    Unlike ``_parse_files_arg`` (which also splits on commas for the legacy
    ``--files`` flag), stdin transport does NOT split on commas — paths
    with commas in their names would round-trip byte-exact through stdin
    but be mangled by a comma-split regex.

    Contract:
      - Empty payload (``''``) -> ``[]``
      - Whitespace-only lines -> dropped (consistent with the legacy
        empty-filter behavior)
      - Lines containing spaces, quotes, backslashes, tabs -> preserved
        verbatim. No shell expansion, no tokenization beyond the
        newline split.

    Returns:
        List of non-empty path strings in input order.
    """
    if not payload:
        return []
    # Split on newline ONLY (no comma split) so paths with commas survive
    # byte-exact. ``splitlines()`` handles trailing-newline edge cases
    # consistently across platforms.
    lines = payload.splitlines()
    # Drop empty and whitespace-only lines (parity with _parse_files_arg's
    # empty-filter behavior — closed stdin + 5 blank lines must yield []).
    return [line for line in lines if line.strip()]


def _chunk_paths(paths: list[str], chunk_size: int = _CHUNK_SIZE) -> list[list[str]]:
    """Split ``paths`` into consecutive batches of ``chunk_size`` items.

    Order is preserved across batches: flattening the result yields a
    byte-identical sequence to ``paths``. The last batch may be shorter
    than ``chunk_size`` (the remainder). Empty input returns ``[]``.

    Selection logic is unchanged by design: this helper partitions the
    existing selected-path-set without dropping, reordering, or
    deduplicating any element.

    Args:
        paths: Ordered list of path strings to partition.
        chunk_size: Maximum items per batch. Must be >= 1.

    Returns:
        List of batches (each a list[str]), order-preserving.
    """
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
    if not paths:
        return []
    batches: list[list[str]] = []
    for start in range(0, len(paths), chunk_size):
        batches.append(list(paths[start : start + chunk_size]))
    return batches


def _chunked_apply(
    fn: Callable[[list[Path]], list[dict]],
    items: list[Path],
    chunk_size: int = _CHUNK_SIZE,
) -> list[dict]:
    """Apply ``fn`` to ``items`` in consecutive ≤chunk_size batches.

    Each batch is processed independently; results from all batches are
    concatenated in order. Selection logic inside ``fn`` is preserved
    (the chunks are the SAME inputs, just smaller batches), so per-file
    outcomes are byte-identical to a single-pass invocation. Memory is
    bounded because at most ``chunk_size`` files are in flight at once
    per check.

    Args:
        fn: A check_*() function — takes ``list[Path]``, returns
            ``list[dict]`` of result rows.
        items: Ordered list of ``Path`` objects to dispatch to ``fn``.
        chunk_size: Maximum items per batch (default 200).

    Returns:
        Concatenated list of result rows from every batch.
    """
    if not items:
        return []
    out: list[dict] = []
    for batch in _chunk_paths([str(p) for p in items], chunk_size=chunk_size):
        batch_paths = [Path(p) for p in batch]
        out.extend(fn(batch_paths))
    return out


def _build_argparser() -> argparse.ArgumentParser:
    """Build the gate's argparse parser.

    Extracted from main() so tests can drive argparse in isolation (e.g.
    to assert the presence of new flags without invoking the rest of
    the gate pipeline). All flags and defaults are byte-identical to
    the pre-change parser; ``--files-stdin`` is the additive new flag.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Builder Quality Gate\n\n"
            "Default mode for lint (ruff) checks is baseline-diff: only NEW findings "
            "(absent in the merge-base with main, present now) are blocking. Findings "
            "present in baseline and unchanged are reported as informational counts only. "
            "Pass --no-baseline-diff to restore strict mode (every finding blocks). "
            "When merge-base is unresolvable the gate falls back to strict mode and "
            "logs a WARN. The baseline commit hash is always logged in the report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workspace", default=str(WORKSPACE))
    parser.add_argument(
        "--files",
        help=(
            "Comma-separated list of files to check. Backward-compat: "
            "also accepts newline-separated input (the legacy hook "
            "passed staged files this way). For large staged sets "
            "(>1,000 files), prefer --files-stdin to avoid argv "
            "length limits."
        ),
    )
    parser.add_argument(
        "--files-stdin",
        action="store_true",
        help=(
            "Read newline-separated paths from stdin. Mirrors the pre-"
            "commit hook's own Part 5 forensic-logger stdin pattern; "
            "sidesteps MAX_ARG_STRLEN (128KB argv limit) for huge "
            "staged diffs. Empty stdin = no files (equivalent to "
            "omitting both --files and --files-stdin would error; "
            "use --files-stdin explicitly when stdin may be empty)."
        ),
    )
    parser.add_argument("--git-diff", action="store_true", help="Auto-detect from git diff")
    parser.add_argument(
        "--no-baseline-diff",
        action="store_true",
        help=(
            "Disable baseline-diff mode for lint checks. By default the gate only "
            "blocks on NEW ruff findings vs the merge-base with main. With this "
            "flag set, every ruff finding blocks (legacy strict behavior)."
        ),
    )
    parser.add_argument("--skip-tests", action="store_true", help="(deprecated) Skip targeted tests")
    parser.add_argument(
        "--scoped-tests", action="store_true", help="Run scoped tests for changed files (default with --files)"
    )
    parser.add_argument("--full", action="store_true", help="Run full test suite instead of scoped tests")
    parser.add_argument("--skip-mypy", action="store_true", help="Skip mypy (slow)")
    parser.add_argument("--strict", action="store_true", help="Fail-fast on any check failure (for pre-commit hook)")
    # --- BUILD-METADATA emit stage (card 9fdefeee) -----------------------------
    # The gate's "completion stage" hook: when a card id + sprint doc are
    # passed, the gate append-safely writes a ``---BUILD-METADATA---`` block
    # to the sprint doc after the quality checks finish. This closes the
    # stuck-review class at source — inline / orchestrator-coordinated
    # builds now emit BUILD-METADATA as a property of the gate completion
    # (not the dispatch path), so BQES proof extraction never fails closed
    # on a missing provenance row.
    #
    # Both flags must be passed together; passing only one is a usage error
    # (the report's ``emit_metadata`` field is omitted when neither is set,
    # which is the case for ad-hoc non-build invocations — e.g. dry runs
    # and cron sweeps — where no sprint doc is in play).
    parser.add_argument(
        "--emit-metadata-card-id",
        default=None,
        help=(
            "Workboard card id for the inline-build completion-stage "
            "BUILD-METADATA emit (card 9fdefeee). When set, "
            "--emit-metadata-sprint-doc must also be set; the gate "
            "appends a terminated ---BUILD-METADATA---/---END-METADATA--- "
            "block to the sprint doc append-safely after the quality "
            "checks finish. Defaults to None (no emit; legacy behavior)."
        ),
    )
    parser.add_argument(
        "--emit-metadata-sprint-doc",
        default=None,
        help=(
            "Path to the sprint doc to append the BUILD-METADATA block "
            "to (card 9fdefeee). Must be paired with "
            "--emit-metadata-card-id. Path is relative to --workspace "
            "unless absolute."
        ),
    )
    parser.add_argument(
        "--emit-metadata-clearance-dir",
        default=None,
        help=(
            "Optional directory holding <cardid>-cleared.json (card "
            "9fdefeee). When set, the emit stage loads a clearance "
            "file if present; inline builds usually pass --emit-"
            "metadata-clearance-dir=None and the helper composes the "
            "block without the 'rin-cleared by ...' note."
        ),
    )
    # --- tsc typecheck config (card f3c668bf) ---------------------------------
    # The gate's check_tsc used to hardcode 30s timeout and no NODE_OPTIONS,
    # which OOMs large projects at ~24s (EXIT=134) as an infra artifact.
    # These flags let builders raise the timeout, raise the V8 heap, and
    # pin a scoped tsconfig without modifying the gate's source. All three
    # also accept env-var overrides (BQG_TSC_TIMEOUT, BQG_TSC_NODE_OPTIONS,
    # BQG_TSC_CONFIG) so pre-commit hooks and cron invocations can set them
    # without changing argv. Defaults: 120s, --max-old-space-size=8192, no
    # scoped tsconfig (auto-discover from file path as before).
    parser.add_argument(
        "--tsc-timeout",
        type=int,
        default=None,
        help=(
            "Per-tsconfig-root tsc timeout in seconds (card f3c668bf). "
            "Default 120; the previous hardcoded 30s reliably OOMed large "
            "projects before type errors could surface. Env override: "
            "BQG_TSC_TIMEOUT."
        ),
    )
    parser.add_argument(
        "--tsc-node-options",
        default=None,
        help=(
            "Value passed as NODE_OPTIONS to the tsc subprocess (card "
            "f3c668bf). Default --max-old-space-size=8192 (8GB V8 heap). "
            "Set to empty string to disable. Env override: "
            "BQG_TSC_NODE_OPTIONS."
        ),
    )
    parser.add_argument(
        "--tsconfig",
        default=None,
        help=(
            "Scoped tsconfig path for tsc (card f3c668bf). When set, the "
            "gate runs ONE `tsc -p <path> --noEmit` against this config "
            "rather than the per-file _find_tsconfig walk — extensions "
            "builds can use a slim tsconfig to avoid the full-project OOM "
            "artifact. Env override: BQG_TSC_CONFIG."
        ),
    )
    return parser


def _collect_input_paths(
    args: argparse.Namespace,
    stdin_payload: str | None = None,
) -> list[str]:
    """Collect the logical file list from ``--files`` and/or ``--files-stdin``.

    Both flags contribute additively when both are provided (a future
    caller may pass --files to pre-include hardcoded paths AND pipe
    dynamic paths via stdin). Whichever path is used, the returned list
    preserves input order.

    Args:
        args: Parsed argparse namespace (must have ``files`` and
            ``files_stdin`` attributes).
        stdin_payload: The stdin payload to parse when ``--files-stdin``
            is set. When None, ``sys.stdin.read()`` is used. Tests pass
            an explicit payload to avoid tying them to real stdin.

    Returns:
        List of path strings (logical, not yet resolved against WORKSPACE).
    """
    paths: list[str] = []
    if getattr(args, "files", None):
        paths.extend(_parse_files_arg(args.files))
    if getattr(args, "files_stdin", False):
        if stdin_payload is None:
            stdin_payload = sys.stdin.read()
        paths.extend(_parse_files_stdin_input(stdin_payload))
    return paths


def _resolve_input_files(
    raw_paths: list[str],
    workspace: Path,
) -> list[Path]:
    """Resolve logical path strings to ``Path`` objects against ``workspace``.

    Backward-compat with the pre-change ``--files`` resolution (card
    03f447e2): relative paths are anchored at ``workspace`` so paths
    resolve correctly regardless of the gate's CWD; absolute paths are
    kept as-is.

    Args:
        raw_paths: Logical path strings from ``--files`` / ``--files-stdin``.
        workspace: Workspace root for resolving relative paths.

    Returns:
        List of ``Path`` objects in input order.
    """
    resolved: list[Path] = []
    for raw in raw_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = workspace / p
        resolved.append(p)
    return resolved


def _emit_build_metadata(
    args: argparse.Namespace,
    workspace: Path,
    quality_failed: int,
) -> tuple[dict, str]:
    """Completion-stage BUILD-METADATA emit (card 9fdefeee).

    Called from ``main()`` after the quality checks finish. Wraps
    ``scripts.build_metadata_backfill.compose_block()`` (the shared public
    emit API) and turns its ``AppendResult`` into the report's
    ``emit_metadata`` block.

    Wiring contract:

    * Both ``--emit-metadata-card-id`` AND ``--emit-metadata-sprint-doc``
      must be set (or both unset). Setting only one is a usage error
      (the gate records ``status="error"`` with a ``reason`` explaining
      the missing partner and returns exit code 1 — the build fails
      closed, otherwise we'd re-create the stuck-review class).
    * The sprint doc path is resolved relative to ``workspace`` when
      relative (mirrors the legacy ``--files`` resolution semantics, card
      03f447e2).
    * Optional ``--emit-metadata-clearance-dir`` is passed through to
      ``compose_block()`` for drainer-style builds that already have a
      clearance file in hand.
    * Quality-gate failures do NOT block the emit — a failed gate still
      writes the block (the block is a property of the BUILD, not of
      the gate's verdict). Ava reads the report's ``emit_metadata``
      alongside ``overall``; if both pass, she merges; if the gate
      failed, she re-runs the build. Either way, the BUILD-METADATA row
      is in the sprint doc by the time the build ends.
    * Append-safety is preserved: ``compose_block()`` verifies the line
      count grows and is idempotent per card_id (HR43).

    Returns:
        ``(report_block, block_text)`` — ``report_block`` is the dict to
        drop into the gate report under ``emit_metadata``; ``block_text``
        is the rendered ``---BUILD-METADATA---`` body (printed in the
        summary so builders can verify the block shape at a glance).
    """
    cid = getattr(args, "emit_metadata_card_id", None)
    sprint_doc_arg = getattr(args, "emit_metadata_sprint_doc", None)

    if bool(cid) != bool(sprint_doc_arg):
        missing = "card-id" if not cid else "sprint-doc"
        return (
            {
                "status": "error",
                "reason": (
                    f"--emit-metadata-{missing} missing: --emit-metadata-card-id and "
                    f"--emit-metadata-sprint-doc must be passed together (card 9fdefeee)"
                ),
                "card_id": cid,
                "sprint_doc": sprint_doc_arg,
            },
            "",
        )

    # Both set (or both unset — caller checked first). Resolve sprint
    # doc against workspace for relative paths (legacy --files semantics).
    sprint_doc = Path(sprint_doc_arg)
    if not sprint_doc.is_absolute():
        sprint_doc = workspace / sprint_doc

    # Lazy import: scripts.build_metadata_backfill is a sibling module
    # that may not be on sys.path when BQG is run from an extension dir.
    # The bootstrap at module top inserts the workspace root, so a
    # normal ``import scripts.build_metadata_backfill`` works here.
    try:
        from scripts.build_metadata_backfill import compose_block  # noqa: E402
    except Exception as exc:  # pragma: no cover - import guard
        return (
            {
                "status": "error",
                "reason": f"failed to import build_metadata_backfill.compose_block: {exc}",
                "card_id": cid,
                "sprint_doc": str(sprint_doc),
            },
            "",
        )

    clearance_dir = getattr(args, "emit_metadata_clearance_dir", None)
    if clearance_dir:
        clearance_dir = Path(clearance_dir)
        if not clearance_dir.is_absolute():
            clearance_dir = workspace / clearance_dir

    try:
        result = compose_block(
            cid,
            sprint_doc,
            workspace=workspace,
            clearance_dir=clearance_dir,
        )
    except ValueError as exc:
        return (
            {
                "status": "error",
                "reason": f"compose_block raised ValueError: {exc}",
                "card_id": cid,
                "sprint_doc": str(sprint_doc),
            },
            "",
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        return (
            {
                "status": "error",
                "reason": f"compose_block raised {type(exc).__name__}: {exc}",
                "card_id": cid,
                "sprint_doc": str(sprint_doc),
            },
            "",
        )

    status = "appended" if result.appended else "skipped_idempotent"
    return (
        {
            "status": status,
            "card_id": cid,
            "sprint_doc": str(result.sprint_doc),
            "before_lines": result.before_lines,
            "after_lines": result.after_lines,
            "appended": result.appended,
            "reason": result.reason,
            "quality_gate_overall": "FAIL" if quality_failed else "PASS",
            "quality_gate_quality_failed_count": quality_failed,
        },
        "",  # block text already appended to sprint doc; no need to re-print
    )


def main() -> int:
    parser = _build_argparser()
    args = parser.parse_args()

    # Toggle baseline-diff for lint checks. Default ON; --no-baseline-diff opts out.
    global BASELINE_DIFF_ENABLED
    BASELINE_DIFF_ENABLED = not args.no_baseline_diff

    # Wire tsc typecheck config (card f3c668bf). Module defaults already
    # honor the BQG_TSC_* env vars; CLI flags override those. We rebind
    # the globals so check_tsc() picks up the user's choices the same
    # way BASELINE_DIFF_ENABLED is read at call time.
    global TSC_TIMEOUT, TSC_NODE_OPTIONS, TSC_CONFIG
    TSC_TIMEOUT = (
        args.tsc_timeout if args.tsc_timeout is not None else DEFAULT_TSC_TIMEOUT
    )
    TSC_NODE_OPTIONS = (
        args.tsc_node_options
        if args.tsc_node_options is not None
        else DEFAULT_TSC_NODE_OPTIONS
    )
    TSC_CONFIG = (
        args.tsconfig if args.tsconfig is not None else DEFAULT_TSC_CONFIG
    )

    workspace = Path(args.workspace).resolve()
    # Update module-level WORKSPACE for helper functions. All checks read
    # WORKSPACE from module scope at call time, so this rebinding takes
    # effect for subsequent check_*() calls (including check_path_constants,
    # check_unwired_functions, check_affirmations, run_cmd's default cwd).
    globals()["WORKSPACE"] = workspace
    globals()["REPORTS_DIR"] = workspace / "data" / "ops" / "quality_gate_reports"

    # Determine files to check. The transport layer was split out into
    # _collect_input_paths() / _resolve_input_files() so the same code
    # path serves both ``--files`` (legacy comma/newline argv) and
    # ``--files-stdin`` (newline-only stdin) without duplicating the
    # resolution + selection logic.
    if args.files or args.files_stdin:
        raw_paths = _collect_input_paths(args)
        files = _resolve_input_files(raw_paths, workspace)
    elif args.git_diff:
        git_files = get_git_diff_files(workspace)
        # ANTI-FALSE-PASS GUARD (card 3608f04c, sprint
        # 2026-08-16-workboard-gate-integrity): when the gate is run from
        # one repo CWD but --workspace points at a *different* repo and
        # the diff is empty, refuse a silent PASS. The earlier branch-
        # !=-main guard inside get_git_diff_files() only fires inside a
        # SINGLE repo; this catches the cross-repo case where the user
        # intends the worktree to be checked but the diff-vs-main on that
        # worktree happens to be empty (e.g. just-merged branch, or a
        # worktree sitting on top of main with no new commits).
        if not git_files:
            try:
                cwd_repo_root = Path(
                    subprocess.run(
                        ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
                        capture_output=True,
                        text=True,
                        timeout=10,
                    ).stdout.strip()
                ).resolve()
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                cwd_repo_root = None
            if cwd_repo_root is not None and cwd_repo_root != workspace.resolve():
                print(
                    f"ERROR: --git-diff found zero changed files in workspace "
                    f"{workspace} but the gate CWD resolves to a different repo "
                    f"({cwd_repo_root}). Refusing to PASS a gate that checked "
                    f"nothing — re-run from the worktree CWD or pass --files.",
                    file=sys.stderr,
                )
                return 1
        files = [workspace / f for f in git_files if (workspace / f).exists()]
    else:
        print("Error: must specify --files, --files-stdin, or --git-diff", file=sys.stderr)
        return 1

    python_files = [f for f in files if f.suffix == ".py"]
    node_files = [f for f in files if f.suffix in (".js", ".mjs", ".cjs")]
    # TS-flavored files are NOT node-parseable (node --check throws
    # ERR_UNKNOWN_FILE_EXTENSION on .ts/.tsx) — keep them out of
    # check_node's bucket but retain them for eslint/tsc coverage
    # (card 0fc3b4cf-ada4-462a-9e6d-06a20afa055c).
    ts_files = [f for f in files if f.suffix in (".ts", ".tsx")]
    json_files = [f for f in files if f.suffix == ".json"]

    all_results: list[dict] = []
    # Internal iteration is chunked at <=200 files per batch (card
    # 6c7a7613) so memory stays bounded on huge diffs. Selection logic
    # inside each check_*() is unchanged — _chunked_apply just dispatches
    # the same files in smaller batches, and the per-file outcomes are
    # byte-identical to a single-pass invocation.
    all_results.extend(_chunked_apply(check_py_compile, python_files))
    all_results.extend(_chunked_apply(check_node, node_files))
    all_results.extend(_chunked_apply(check_ruff, python_files))
    if not args.skip_mypy:
        all_results.extend(_chunked_apply(check_mypy, python_files))
    all_results.extend(_chunked_apply(check_eslint, node_files + ts_files))
    all_results.extend(_chunked_apply(check_tsc, node_files + ts_files))
    all_results.extend(_chunked_apply(check_json_validity, json_files))
    all_results.extend(_chunked_apply(check_affirmations, json_files))
    # check_path_constants and check_unwired_functions do per-file
    # sweeps that are themselves bounded; they don't need chunking at
    # the dispatch layer. Keep them as direct calls.
    all_results.extend(check_path_constants(python_files))
    all_results.extend(check_unwired_functions(python_files))
    run_tests = args.full or args.scoped_tests or (not args.skip_tests and bool(python_files))
    if run_tests and not args.skip_tests:
        if args.full:
            # Card 7f743d24: scrub hook-leaked GIT_* plumbing on
            # the full-suite invocation too — same Aug 14
            # corruption vector, same defense.
            full_result = run_cmd(
                ["python3", "-m", "pytest", str(WORKSPACE / "tests"), "-q", "--tb=short"],
                timeout=300,
                env=scrubbed_pytest_env(),
            )
            all_results.append(
                {
                    "check": "full_test_suite",
                    **full_result,
                }
            )
        else:
            # check_targeted_tests batches internally; no chunking needed
            # here.
            all_results.extend(check_targeted_tests(python_files))

    # Build report
    passed = sum(1 for r in all_results if r.get("passed"))
    failed = sum(1 for r in all_results if not r.get("passed"))
    skipped = sum(1 for r in all_results if r.get("skipped"))

    report = {
        "timestamp": utc_now(),
        "workspace": str(workspace),
        "files_checked": len(files),
        "checks_total": len(all_results),
        "checks_passed": passed,
        "checks_failed": failed,
        "checks_skipped": skipped,
        "overall": "PASS" if failed == 0 else "FAIL",
        "results": all_results,
    }

    # Write report
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))

    # --- Completion-stage BUILD-METADATA emit (card 9fdefeee) -----------------
    #
    # Inline / orchestrator-coordinated builds (Ava Option-1, Himari-coordinated)
    # bypass the dispatch path that auto-writes BUILD-METADATA, so BQES proof
    # extraction used to fail closed and the cards dead-ended in review. This
    # hook makes BUILD-METADATA emission a property of the build itself
    # (completion-stage hook), not the dispatch path.
    #
    # Wiring rules:
    #   * --emit-metadata-card-id and --emit-metadata-sprint-doc must be
    #     passed together or not at all — passing only one is a usage
    #     error (the gate records the error in the report and returns 1
    #     so the build fails closed).
    #   * The emit runs AFTER the quality checks complete, BEFORE the
    #     summary prints, and writes a fresh ``emit_metadata`` block on
    #     the report. The quality-report path is unchanged otherwise.
    #   * The emit is idempotent per card_id (HR43): a second gate run
    #     on the same card id is a no-op (``appended=false``), so
    #     callers that retry the gate don't get duplicate blocks.
    #   * ``compose_block()`` is the public emit API on
    #     scripts/build_metadata_backfill.py (card 9fdefeee). It
    #     internally calls ``build_metadata_block()`` + ``append_block()``
    #     so the renderer is shared with the drainer (card 7113d82f).
    if args.emit_metadata_card_id or args.emit_metadata_sprint_doc:
        emit_result, emit_block = _emit_build_metadata(args, workspace, failed)
        report["emit_metadata"] = emit_result
        report_path.write_text(json.dumps(report, indent=2, default=str))
        # Emit errors fail the gate (build is incomplete without a
        # BUILD-METADATA row in the sprint doc). The completion gate is
        # the WHOLE point of this hook — letting an emit error pass
        # silently would re-create the stuck-review class.
        if emit_result.get("status") == "error":
            failed += 1

    # Print summary
    print(f"\n{'=' * 60}")
    print(f"Builder Quality Gate: {report['overall']}")
    print(f"{'=' * 60}")
    print(f"Files checked: {len(files)}")
    print(f"Checks: {passed} passed, {failed} failed, {skipped} skipped")
    print()

    for r in all_results:
        status = "✓" if r.get("passed") else "✗"
        check_name = r.get("check", "?")
        file_name = r.get("file", r.get("function", r.get("constant", "")))
        detail = f"  {status} {check_name}: {file_name}"
        if not r.get("passed"):
            stderr = r.get("stderr", "")[:200]
            detail += f"\n     → {stderr}"
            if args.strict:
                print(detail)
                print("\n⚡ --strict mode: stopping at first failure.")
                return 1
        print(detail)

    print(f"\nReport: {report_path}")

    # Print BUILD-METADATA emit summary when the completion-stage hook ran.
    # The hook only runs when --emit-metadata-card-id + --emit-metadata-
    # sprint-doc are both set; otherwise the field is absent from the
    # report and this branch is skipped.
    emit_block = report.get("emit_metadata")
    if emit_block:
        em_status = emit_block.get("status", "?")
        em_cid = emit_block.get("card_id", "?")
        em_doc = emit_block.get("sprint_doc", "?")
        if em_status == "error":
            print(f"\n⚠️  BUILD-METADATA emit failed: {emit_block.get('reason')}")
            print(f"   card_id={em_cid} sprint_doc={em_doc}")
        else:
            print(
                f"\nBUILD-METADATA emit ({em_status}): card_id={em_cid} "
                f"sprint_doc={em_doc} "
                f"lines {emit_block.get('before_lines', '?')} -> {emit_block.get('after_lines', '?')}"
            )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
