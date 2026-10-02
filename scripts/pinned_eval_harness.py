#!/usr/bin/env python3
"""Pinned-Eval Harness — capability-tier evaluation over fixed reference tasks (BQ-1380).

SRB-027 introduced a tiered self-evaluation protocol: every dispatched task is
classified against the T0–T4 capability ladder, scored for capability headroom
and decomposition fitness, then pinned by content hash so the same prompt
re-evaluated later produces the same verdict. This script is the offline
harness that drives that protocol.

What it does
------------
1. **Loads** a set of pinned tasks (JSONL on disk, in-memory list, or the
   built-in demo corpus).
2. **Hashes** each task's content payload (prompt + acceptance criteria +
   expected output schema) so re-runs are deterministic and tamper-evident.
3. **Capability-scores** each task against the T0–T4 ladder
   (T0=trivial, T1=simple, T2=moderate, T3=complex, T4=expert). The score
   reflects whether an agent's measured capability clears the threshold for
   the task's assigned tier.
4. **Decomposition-scores** each task along three axes — atomicity,
   dependency depth, and clarity of acceptance criteria — producing a 0–1
   number that predicts whether the task can be safely fanned out to sub-tasks.
5. **Aggregates** per-tier and overall summaries into a JSONL report at
   ``data/ops/pinned_eval_results.jsonl`` (one row per task), plus a human
   summary printed to stdout.

What it does NOT do
-------------------
- It does not call any LLM. Scoring is deterministic against the harness's
  own heuristics + the agent's measured capability profile.
- It does not dispatch to subagents or touch the workboard.
- It does not modify any other script or pipeline file.

Usage
-----
::

    # Demo run against the built-in corpus (no files needed):
    python scripts/pinned_eval_harness.py --demo

    # Real run over a JSONL file of pinned tasks:
    python scripts/pinned_eval_harness.py \\
        --tasks data/ops/pinned_tasks.jsonl \\
        --capability 0.72 \\
        --output data/ops/pinned_eval_results.jsonl

JSONL schema for ``--tasks`` (one row per task)
----------------------------------------------
::

    {
      "task_id": "pinned-001",
      "prompt": "Add a guard to ...",            // str, the task body
      "acceptance_criteria": "...",              // str, optional but scored
      "expected_outputs": ["diff", "test"],      // list[str], schema hints
      "tier": "T2",                              // T0..T4
      "agent_profile": {                         // optional override
        "success_rate": 0.78,
        "avg_tokens": 4200,
        "rework_rate": 0.05
      },
      "tags": ["risk", "ftmo"]                   // optional
    }

Exit code is 0 on success, 1 on fatal error (bad CLI args, unparseable input).
Aggregate stats are also surfaced through the JSONL output, not the exit code,
so the script can be chained into cron / CI without blocking unrelated work.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ayumi.pinned_eval")

_REPO = Path(__file__).resolve().parents[1]


# ═══════════════════════════════════════════════════════════════════════════
# T0–T4 capability threshold definitions
# ═══════════════════════════════════════════════════════════════════════════
#
# The T0–T4 ladder is the spine of the harness. Each tier pins three numbers:
#
#   min_capability  – the floor of agent measured capability that the task
#                     demands. Below this the agent is not expected to succeed.
#   max_atomic_subtasks – the smallest number of atomic sub-tasks we expect
#                     the task to break into when decomposed. Used by the
#                     decomposition scorer as a coherence check.
#   tolerance       – how forgiving the tier is to capability under-shoot
#                     (0 = no under-shoot accepted, 1 = any under-shoot OK).
#
# Tier labels mirror SRB-027 §2.1.
# ──────────────────────────────────────────────────────────────────────────

TIER_ORDER: tuple[str, ...] = ("T0", "T1", "T2", "T3", "T4")


@dataclass(frozen=True)
class TierThreshold:
    """Capability + decomposition floor for a single T0–T4 tier."""

    tier: str
    label: str
    description: str
    min_capability: float  # 0..1, floor of agent success-rate
    max_atomic_subtasks: int  # upper bound on decomposition fan-out
    tolerance: float  # 0..1, under-shoot forgiveness

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "label": self.label,
            "description": self.description,
            "min_capability": self.min_capability,
            "max_atomic_subtasks": self.max_atomic_subtasks,
            "tolerance": self.tolerance,
        }


TIER_THRESHOLDS: dict[str, TierThreshold] = {
    "T0": TierThreshold(
        tier="T0",
        label="trivial",
        description=(
            "Mechanical edits, single-line changes, copy-paste renames, "
            "or known-good file moves. Failure indicates a broken tool, "
            "not a capability gap."
        ),
        min_capability=0.50,
        max_atomic_subtasks=2,
        tolerance=0.40,
    ),
    "T1": TierThreshold(
        tier="T1",
        label="simple",
        description=(
            "Single-file changes with one clear acceptance criterion. "
            "Examples: add a config flag, wire a logger, patch a typo."
        ),
        min_capability=0.60,
        max_atomic_subtasks=3,
        tolerance=0.30,
    ),
    "T2": TierThreshold(
        tier="T2",
        label="moderate",
        description=(
            "Multi-file changes with 2–3 acceptance criteria and basic "
            "edge-case handling. Examples: add a helper + tests, refactor "
            "a strategy parameter."
        ),
        min_capability=0.70,
        max_atomic_subtasks=5,
        tolerance=0.20,
    ),
    "T3": TierThreshold(
        tier="T3",
        label="complex",
        description=(
            "Cross-module changes with non-obvious dependencies, nuanced "
            "correctness criteria, or significant test scaffolding. "
            "Examples: add a new signal filter, redesign risk sizing."
        ),
        min_capability=0.80,
        max_atomic_subtasks=8,
        tolerance=0.15,
    ),
    "T4": TierThreshold(
        tier="T4",
        label="expert",
        description=(
            "Architectural changes, novel algorithms, or work requiring "
            "external research synthesis. Failure modes are non-local and "
            "hard to triage without deep domain review."
        ),
        min_capability=0.90,
        max_atomic_subtasks=12,
        tolerance=0.10,
    ),
}


def _validate_tier_thresholds() -> None:
    """Defensive sanity check on the threshold table at import time."""
    if set(TIER_THRESHOLDS) != set(TIER_ORDER):
        raise RuntimeError(f"TIER_THRESHOLDS missing/extra tiers: {sorted(TIER_THRESHOLDS)} vs {sorted(TIER_ORDER)}")
    prev_cap = -math.inf
    for tier in TIER_ORDER:
        tt = TIER_THRESHOLDS[tier]
        if tt.min_capability <= prev_cap:
            raise RuntimeError(f"Tier {tier} min_capability {tt.min_capability} not strictly greater than predecessor")
        prev_cap = tt.min_capability
        if not 0.0 <= tt.tolerance <= 1.0:
            raise RuntimeError(f"Tier {tier} tolerance {tt.tolerance} outside [0,1]")
        if tt.max_atomic_subtasks < 1:
            raise RuntimeError(f"Tier {tier} max_atomic_subtasks {tt.max_atomic_subtasks} < 1")


_validate_tier_thresholds()


# ═══════════════════════════════════════════════════════════════════════════
# PinnedTask dataclass + content hashing
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class PinnedTask:
    """A single pinned evaluation task, content-addressable by hash.

    ``task_id`` is the human label; ``content_hash`` is the canonical
    fingerprint of the prompt + acceptance + expected outputs, used for
    dedupe and tamper detection across runs.
    """

    task_id: str
    prompt: str
    acceptance_criteria: str = ""
    expected_outputs: list[str] = field(default_factory=list)
    tier: str = "T1"
    agent_profile: dict[str, float] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.tier not in TIER_THRESHOLDS:
            raise ValueError(f"PinnedTask {self.task_id!r} has unknown tier {self.tier!r}; valid: {list(TIER_ORDER)}")
        if not self.task_id:
            raise ValueError("PinnedTask.task_id must be non-empty")
        if not isinstance(self.expected_outputs, list):
            raise TypeError("expected_outputs must be a list[str]")
        if not isinstance(self.agent_profile, dict):
            raise TypeError("agent_profile must be a dict[str, float]")
        # content_hash is derived; recompute if caller left it blank or stale.
        canonical = self._canonical_payload()
        if not self.content_hash:
            self.content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _canonical_payload(self) -> str:
        """Stable JSON of the fields that define the task's identity."""
        return json.dumps(
            {
                "task_id": self.task_id,
                "prompt": self.prompt,
                "acceptance_criteria": self.acceptance_criteria,
                "expected_outputs": sorted(self.expected_outputs),
                "tier": self.tier,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def verify_hash(self) -> bool:
        """Return True if the stored content_hash still matches the payload."""
        return self.content_hash == hashlib.sha256(self._canonical_payload().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PinnedTask":
        """Hydrate from a JSONL row, dropping unknown keys defensively."""
        if not isinstance(raw, dict):
            raise TypeError(f"PinnedTask row must be a dict, got {type(raw)}")
        known = {f.name for f in dataclasses.fields(cls)}
        clean = {k: v for k, v in raw.items() if k in known}
        # Reject task_id-less rows outright; downstream scoring keys on it.
        if "task_id" not in clean or not clean["task_id"]:
            raise ValueError(f"PinnedTask row missing task_id: {raw!r}")
        return cls(**clean)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def content_hash(task: PinnedTask) -> str:
    """Public accessor so callers don't poke at the field directly."""
    return task.content_hash


# ═══════════════════════════════════════════════════════════════════════════
# Capability scoring
# ═══════════════════════════════════════════════════════════════════════════
#
# The capability score answers: "given an agent's measured success rate on
# similar tasks, does it clear the floor for this tier, with the tier's
# tolerance applied?" The result is a 0–1 number where 1.0 means
# comfortable headroom and 0.0 means far below the floor.
# ──────────────────────────────────────────────────────────────────────────


def capability_score(
    task: PinnedTask,
    measured_capability: float,
    *,
    rework_rate: Optional[float] = None,
    avg_tokens: Optional[float] = None,
) -> dict[str, Any]:
    """Score a task against the T0–T4 ladder.

    Parameters
    ----------
    task:
        The pinned task under evaluation.
    measured_capability:
        0..1 — agent's recent success rate on comparable tasks. Overrides
        ``task.agent_profile["success_rate"]`` if supplied.
    rework_rate:
        Optional 0..1 override for the rework-rate penalty.
    avg_tokens:
        Optional avg tokens override; used to estimate cost pressure.

    Returns
    -------
    dict with keys: tier, label, min_capability, tolerance, measured,
    raw_score, headroom, rework_penalty, token_penalty, final_score, verdict.
    """
    if not 0.0 <= measured_capability <= 1.0:
        raise ValueError(f"measured_capability {measured_capability} outside [0,1]")
    profile = dict(task.agent_profile or {})
    rr = rework_rate if rework_rate is not None else float(profile.get("rework_rate", 0.0))
    at = avg_tokens if avg_tokens is not None else float(profile.get("avg_tokens", 0.0))
    if not 0.0 <= rr <= 1.0:
        raise ValueError(f"rework_rate {rr} outside [0,1]")
    if at < 0:
        raise ValueError(f"avg_tokens {at} must be >= 0")

    tt = TIER_THRESHOLDS[task.tier]
    headroom = measured_capability - tt.min_capability
    # Tolerance widens the "pass" band. Below the floor we still get a
    # partial credit scaled by how close we are to the floor minus tolerance.
    if headroom >= 0:
        raw_score = min(1.0, 0.5 + 0.5 * (1.0 + headroom / (1.0 - tt.min_capability + 1e-9)))
    else:
        # Linear ramp from 0 (at -tolerance) to 0.5 (at floor)
        span = max(tt.tolerance, 1e-9)
        raw_score = max(0.0, 0.5 * (1.0 + headroom / span))

    # Rework penalty: each 10% rework knocks 5% off, capped at 25%.
    rework_penalty = min(0.25, 0.5 * rr)
    # Token pressure: above 8k tokens avg we start penalizing; cap at 15%.
    token_penalty = min(0.15, max(0.0, (at - 8000.0) / 80000.0))

    final_score = max(0.0, min(1.0, raw_score - rework_penalty - token_penalty))

    if final_score >= 0.85:
        verdict = "STRONG"
    elif final_score >= 0.65:
        verdict = "READY"
    elif final_score >= 0.45:
        verdict = "MARGINAL"
    elif final_score >= 0.20:
        verdict = "WEAK"
    else:
        verdict = "FAIL"

    return {
        "tier": tt.tier,
        "label": tt.label,
        "min_capability": tt.min_capability,
        "tolerance": tt.tolerance,
        "measured_capability": measured_capability,
        "headroom": headroom,
        "raw_score": raw_score,
        "rework_penalty": rework_penalty,
        "token_penalty": token_penalty,
        "final_score": final_score,
        "verdict": verdict,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Decomposition scoring
# ═══════════════════════════════════════════════════════════════════════════
#
# Decomposition score predicts whether a task can be safely fanned out into
# atomic sub-tasks. Three sub-scores are combined:
#
#   atomicity   – does the prompt describe one thing or many?
#   clarity     – are acceptance criteria crisp enough to split on?
#   dependency  – are there hidden ordering / data-flow couplings?
# ──────────────────────────────────────────────────────────────────────────


# Words that tend to flag atomicity violations. Tight list — false positives
# are worse than false negatives here.
_ATOMICITY_FLAGS = (
    " and ",
    " then ",
    " plus ",
    " also ",
    " while ",
    " afterwards ",
    " afterwards,",
    " additionally ",
    " as well as ",
)
_CLARITY_HINTS = (
    "must ",
    "should ",
    "exactly ",
    "returns ",
    "raises ",
    "verifies ",
    "asserts ",
    "given ",
    "when ",
    "then ",
)


def _word_count(text: str) -> int:
    return len([w for w in text.split() if w.strip()])


def decomposition_score(task: PinnedTask) -> dict[str, Any]:
    """Estimate how cleanly the task decomposes into atomic sub-tasks.

    Returns a dict with sub-scores, the combined 0–1 score, and an estimate
    of the atomic sub-task count.
    """
    prompt = task.prompt or ""
    acceptance = task.acceptance_criteria or ""
    prompt_words = _word_count(prompt)
    acceptance_words = _word_count(acceptance)

    # Atomicity: penalize when the prompt mentions multiple actions or runs
    # long. We deliberately under-penalize: the *capability* score catches
    # over-scope, while this score catches under-decomposed attempts.
    flag_hits = sum(1 for f in _ATOMICITY_FLAGS if f in prompt.lower())
    if prompt_words <= 25:
        length_penalty = 0.0
    elif prompt_words <= 75:
        length_penalty = 0.15
    elif prompt_words <= 200:
        length_penalty = 0.35
    else:
        length_penalty = 0.55
    atomicity = max(0.0, 1.0 - 0.18 * flag_hits - length_penalty)

    # Clarity: presence of crisp acceptance language raises the score.
    acceptance_lower = acceptance.lower()
    clarity_hits = sum(1 for h in _CLARITY_HINTS if h in acceptance_lower)
    expected_outputs = len(task.expected_outputs or [])
    if acceptance_words == 0 and expected_outputs == 0:
        clarity = 0.30  # nothing to score against
    else:
        base = 0.40 if acceptance_words >= 5 else 0.20
        clarity = min(1.0, base + 0.12 * clarity_hits + 0.10 * expected_outputs)

    # Dependency: count tags that imply coupling. We deliberately keep this
    # list short — coupling is hard to detect from text alone and false
    # coupling penalties are expensive.
    coupling_tags = {
        "race",
        "lock",
        "shared-state",
        "global",
        "crossover",
        "migration",
        "schema",
    }
    tag_hits = sum(1 for t in task.tags if t.lower() in coupling_tags)
    if prompt_words > 150:
        tag_hits += 1
    dependency = max(0.0, 1.0 - 0.20 * tag_hits)

    combined = 0.45 * atomicity + 0.35 * clarity + 0.20 * dependency
    combined = max(0.0, min(1.0, combined))

    # Estimate sub-task count: starts at 1, scales with prompt length and
    # coupling, then is clamped to the tier's max_atomic_subtasks.
    estimated_subtasks = max(
        1,
        1 + flag_hits + (1 if prompt_words > 100 else 0) + (1 if prompt_words > 250 else 0) + tag_hits,
    )
    tier_max = TIER_THRESHOLDS[task.tier].max_atomic_subtasks
    clamped_subtasks = min(estimated_subtasks, tier_max)

    if combined >= 0.80:
        verdict = "DECOMPOSE_OK"
    elif combined >= 0.55:
        verdict = "DECOMPOSE_CAUTIOUS"
    else:
        verdict = "DECOMPOSE_RISKY"

    return {
        "atomicity": atomicity,
        "clarity": clarity,
        "dependency": dependency,
        "combined": combined,
        "estimated_subtasks": estimated_subtasks,
        "clamped_subtasks": clamped_subtasks,
        "tier_max_subtasks": tier_max,
        "verdict": verdict,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Aggregate reporting
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class PinnedEvalResult:
    """One JSONL row per task: scores + metadata."""

    task_id: str
    content_hash: str
    tier: str
    label: str
    capability: dict[str, Any]
    decomposition: dict[str, Any]
    measured_capability: float
    agent_profile: dict[str, float]
    tags: list[str]
    prompt_chars: int
    acceptance_chars: int
    expected_outputs: list[str]
    timestamp: str
    schema_version: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


SCHEMA_VERSION = "pinned-eval/1.0"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def evaluate(
    task: PinnedTask,
    measured_capability: float,
    *,
    rework_rate: Optional[float] = None,
    avg_tokens: Optional[float] = None,
    timestamp: Optional[str] = None,
) -> PinnedEvalResult:
    """Run capability + decomposition scoring for a single task."""
    cap = capability_score(
        task,
        measured_capability,
        rework_rate=rework_rate,
        avg_tokens=avg_tokens,
    )
    decomp = decomposition_score(task)
    return PinnedEvalResult(
        task_id=task.task_id,
        content_hash=task.content_hash,
        tier=task.tier,
        label=TIER_THRESHOLDS[task.tier].label,
        capability=cap,
        decomposition=decomp,
        measured_capability=measured_capability,
        agent_profile=dict(task.agent_profile or {}),
        tags=list(task.tags or []),
        prompt_chars=len(task.prompt or ""),
        acceptance_chars=len(task.acceptance_criteria or ""),
        expected_outputs=list(task.expected_outputs or []),
        timestamp=timestamp or _now_iso(),
        schema_version=SCHEMA_VERSION,
    )


def aggregate(results: Iterable[PinnedEvalResult]) -> dict[str, Any]:
    """Roll up per-task results into tier + overall summary stats."""
    by_tier: dict[str, list[PinnedEvalResult]] = {t: [] for t in TIER_ORDER}
    overall: list[PinnedEvalResult] = []
    for r in results:
        overall.append(r)
        by_tier.setdefault(r.tier, []).append(r)

    def _tier_block(rows: list[PinnedEvalResult]) -> dict[str, Any]:
        if not rows:
            return {"count": 0}
        cap_scores = [r.capability["final_score"] for r in rows]
        decomp_scores = [r.decomposition["combined"] for r in rows]
        verdicts: dict[str, int] = {}
        for r in rows:
            verdicts[r.capability["verdict"]] = verdicts.get(r.capability["verdict"], 0) + 1
        return {
            "count": len(rows),
            "capability_mean": _mean(cap_scores),
            "capability_min": min(cap_scores),
            "capability_max": max(cap_scores),
            "decomposition_mean": _mean(decomp_scores),
            "verdict_counts": verdicts,
            "ready_share": sum(1 for r in rows if r.capability["verdict"] in ("STRONG", "READY")) / len(rows),
        }

    tier_summary = {t: _tier_block(by_tier.get(t, [])) for t in TIER_ORDER}
    overall_block = _tier_block(overall)
    return {
        "schema_version": SCHEMA_VERSION,
        "total_tasks": len(overall),
        "by_tier": tier_summary,
        "overall": overall_block,
    }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# ═══════════════════════════════════════════════════════════════════════════
# I/O — load tasks, write results
# ═══════════════════════════════════════════════════════════════════════════


def load_tasks_jsonl(path: Path) -> list[PinnedTask]:
    """Load pinned tasks from a JSONL file. Bad rows are logged and skipped."""
    if not path.exists():
        raise FileNotFoundError(f"Tasks file not found: {path}")
    tasks: list[PinnedTask] = []
    with path.open("r", encoding="utf-8") as fh:
        for ln, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                raw = json.loads(line)
                tasks.append(PinnedTask.from_dict(raw))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                log.warning("Skipping %s:%d: %s", path, ln, exc)
    return tasks


def write_results_jsonl(path: Path, results: Iterable[PinnedEvalResult]) -> int:
    """Append results as JSONL. Returns the number of rows written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r.as_dict(), default=str) + "\n")
            n += 1
    return n


# ═══════════════════════════════════════════════════════════════════════════
# Demo corpus + CLI
# ═══════════════════════════════════════════════════════════════════════════


def _demo_tasks() -> list[PinnedTask]:
    """Built-in corpus so ``--demo`` works without any files on disk."""
    return [
        PinnedTask(
            task_id="demo-T0-rename",
            prompt=("Rename the function ``_old_name`` to ``_new_name`` in ``src/forex_bot/util.py``."),
            acceptance_criteria=(
                "All call sites updated; ``grep -rn _old_name src/forex_bot`` returns zero hits; tests still pass."
            ),
            expected_outputs=["diff", "test_log"],
            tier="T0",
            tags=["refactor"],
        ),
        PinnedTask(
            task_id="demo-T1-flag",
            prompt=(
                "Add a ``--strict`` flag to ``scripts/audit_bar_close.py`` that "
                "returns exit code 2 when any FAIL verdict is produced."
            ),
            acceptance_criteria=(
                "Flag is wired through argparse; the audit exits 2 on any FAIL; "
                "the existing exit-0 contract for clean runs is preserved."
            ),
            expected_outputs=["diff", "test_log"],
            tier="T1",
            tags=["scripts"],
        ),
        PinnedTask(
            task_id="demo-T2-filter",
            prompt=(
                "Add a session filter to the ``SessionRangeMeanReversion`` "
                "strategy that blocks entries during the first 5 minutes of "
                "London and New York. Add tests covering both block and "
                "allow paths."
            ),
            acceptance_criteria=(
                "Filter is configurable via config dict; unit tests cover "
                "London open block, NY open block, and post-open allow; "
                "existing backtest results do not regress."
            ),
            expected_outputs=["diff", "test_log", "backtest_summary"],
            tier="T2",
            tags=["strategy", "filter"],
        ),
        PinnedTask(
            task_id="demo-T3-sizer",
            prompt=(
                "Refactor ``signal_engine.risk_sizer`` to support per-pair "
                "max-risk overrides loaded from a YAML config. Wire the new "
                "config through ``launch_blend_forward_test.py`` and add a "
                "test that asserts the override is honoured when the YAML "
                "value differs from the global default."
            ),
            acceptance_criteria=(
                "YAML schema is documented in ``docs/forex/risk-sizing.md``; "
                "the per-pair override beats the global default; missing "
                "config falls back cleanly without raising; CI green."
            ),
            expected_outputs=["diff", "test_log", "config_schema_doc"],
            tier="T3",
            tags=["risk", "config"],
        ),
        PinnedTask(
            task_id="demo-T4-arch",
            prompt=(
                "Design a multi-agent regime classification pipeline that "
                "ingests DXY, EURUSD, and USDJPY daily bars plus a curated "
                "news corpus, and produces a daily regime label (trending / "
                "ranging / intervention-risk) consumed by ``forward_test``."
            ),
            acceptance_criteria=(
                "Architecture document covers data contracts, agent "
                "boundaries, failure modes, and a 30-day dry-run plan; "
                "synthesised against existing SRB-AYUMI-007 + BoJ "
                "intervention research."
            ),
            expected_outputs=["architecture_doc", "dry_run_plan", "data_contracts"],
            tier="T4",
            tags=["architecture", "research", "regime"],
        ),
    ]


def _demo_capability() -> float:
    """Calibrated for the demo corpus: succeeds on T0–T2, struggles on T3+."""
    return 0.72


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pinned_eval_harness.py",
        description=(
            "Offline capability + decomposition evaluation over pinned "
            "tasks. See module docstring (SRB-027) for details."
        ),
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--tasks",
        type=Path,
        help="Path to a JSONL file of pinned tasks (one per row).",
    )
    src.add_argument(
        "--demo",
        action="store_true",
        help="Run against the built-in demo corpus (no files needed).",
    )
    p.add_argument(
        "--capability",
        type=float,
        default=None,
        help=("Agent's measured capability (0..1). Overrides any agent_profile.success_rate in the task rows."),
    )
    p.add_argument(
        "--rework-rate",
        type=float,
        default=None,
        help="Optional 0..1 rework rate applied uniformly.",
    )
    p.add_argument(
        "--avg-tokens",
        type=float,
        default=None,
        help="Optional average tokens per task applied uniformly.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=_REPO / "data" / "ops" / "pinned_eval_results.jsonl",
        help="JSONL output path (default: data/ops/pinned_eval_results.jsonl).",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print the aggregate summary to stdout after writing JSONL.",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Truncate the output file before writing (default: append).",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)

    try:
        if args.demo:
            tasks = _demo_tasks()
            measured = args.capability if args.capability is not None else _demo_capability()
            source = "demo"
        else:
            if not args.tasks:
                log.error("Provide --tasks <path> or --demo")
                return 1
            tasks = load_tasks_jsonl(args.tasks)
            measured = (
                args.capability if args.capability is not None else 0.65  # conservative default if user didn't pass one
            )
            source = str(args.tasks)

        if not tasks:
            log.error("No tasks to evaluate (source=%s)", source)
            return 1
        if not 0.0 <= measured <= 1.0:
            log.error("--capability must be in [0,1], got %s", measured)
            return 1

        if args.reset and args.output.exists():
            args.output.unlink()

        log.info(
            "Evaluating %d pinned task(s) from %s at capability=%.3f",
            len(tasks),
            source,
            measured,
        )
        results: list[PinnedEvalResult] = []
        for t in tasks:
            if not t.verify_hash():
                log.warning(
                    "Task %s has stale content_hash; recomputed in-memory",
                    t.task_id,
                )
            results.append(
                evaluate(
                    t,
                    measured,
                    rework_rate=args.rework_rate,
                    avg_tokens=args.avg_tokens,
                )
            )

        n_written = write_results_jsonl(args.output, results)
        log.info("Wrote %d result row(s) to %s", n_written, args.output)

        if args.summary:
            summary = aggregate(results)
            print(json.dumps(summary, indent=2, default=str))

        # Always print a compact per-task line for human inspection.
        for r in results:
            cap = r.capability
            d = r.decomposition
            log.info(
                "%-22s | %s %-8s | cap=%5s (%.2f) | decomp=%5s (%.2f) | subtasks=%d/%d",
                r.task_id,
                r.tier,
                r.label,
                cap["verdict"],
                cap["final_score"],
                d["verdict"],
                d["combined"],
                d["clamped_subtasks"],
                d["tier_max_subtasks"],
            )

        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("Fatal: %s", exc)
        log.debug("%s", exc.__class__.__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
