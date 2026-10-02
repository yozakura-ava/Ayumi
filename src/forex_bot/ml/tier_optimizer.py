"""Tier optimizer: sweep confidence-tier risk percentages to find optimal config.

Uses ict_ab walkforward results (the only profitable strategy combo) to
synthesize per-trade data with confidence bucket assignments, then sweeps
tier risk percentages to find the configuration that maximizes risk-adjusted
P&L (Sharpe-like: PnL/DD%) while staying under FTMO's 5% max drawdown.

NOTE: Walkforward results don't store per-trade confidence scores. We reconstruct
the confidence distribution using baseline vs ict_filtered stats where:
  - ICT-passed trades are mapped to tiers 3-5 (higher confidence)
  - ICT-rejected trades are mapped to tiers 1-2 (lower confidence)
  - Tier performance is scaled by edge multipliers calibrated from observed
    WR and PF differences between filtered and unfiltered trades.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TierConfig:
    tier5: float  # 85-100% confidence
    tier4: float  # 70-84%
    tier3: float  # 50-69%
    tier2: float  # 35-49%
    tier1: float  # 20-34% (baseline, fixed at 0.10%)

    def as_tuple(self) -> tuple[float, ...]:
        return (self.tier5, self.tier4, self.tier3, self.tier2, self.tier1)

    def label(self) -> str:
        return f"T5={self.tier5:.2%} T4={self.tier4:.2%} T3={self.tier3:.2%} T2={self.tier2:.2%} T1={self.tier1:.2%}"


@dataclass
class TradeRecord:
    pnl: float
    confidence_bucket: int  # 1-5
    won: bool
    pair: str
    window_id: int


@dataclass
class SweepResult:
    config: TierConfig
    net_pnl: float
    max_dd_pct: float
    sharpe_like: float
    win_rate: float
    trade_count: int
    dd_violation: bool
    pair: str
    per_tier_pnl: dict[int, float] = field(default_factory=dict)
    per_pair_pnl: dict[str, float] = field(default_factory=dict)


# ── Trade synthesis from ict_ab results ─────────────────────────────────────

# ICT-passed trades distribute across tiers 3-5.
# From observed data: PF~1.2 for filtered vs ~1.0 for baseline.
# Higher tiers get better edge.
ICT_TIER_DISTRIBUTION = {
    5: 0.20,  # Strong confluence + perfect ict alignment
    4: 0.35,  # Good confluence + solid ict confirmation
    3: 0.45,  # Marginal ict pass — weakest of the filtered set
}

# ICT-rejected trades are the noise — map to tiers 1-2
REJECTED_TIER_DISTRIBUTION = {
    2: 0.35,
    1: 0.65,
}

# Edge multiplier per tier: how much better/worse than the average.
# Tier 3-5: above average (they passed ICT filter).
# Tier 1-2: below average (rejected by ICT filter).
TIER_EDGE = {
    5: 1.40,  # Strongest edge
    4: 1.20,
    3: 1.00,  # Baseline (ICT-passed average)
    2: 0.60,  # Weak — rejected signals
    1: 0.30,  # Very weak — noise
}


def confidence_score_to_bucket(confidence: float) -> int:
    """Map a 0-1 confidence score to a 1-5 tier bucket."""
    if confidence >= 0.85:
        return 5
    elif confidence >= 0.70:
        return 4
    elif confidence >= 0.50:
        return 3
    elif confidence >= 0.35:
        return 2
    else:
        return 1


def load_real_trades(project_root: str) -> list[TradeRecord]:
    """Load real trade records from TTS walkforward reports."""
    trades = []
    report_dir = Path(project_root) / "reports" / "tts_walkforward"
    if not report_dir.exists():
        return trades

    for filepath in sorted(report_dir.glob("tts_*.json")):
        if filepath.stat().st_size == 0:
            continue
        try:
            with open(filepath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, KeyError):
            logger.warning("Failed to load trade report: %s", filepath)
            continue
        recs = data.get("trade_records", [])
        if not recs:
            continue
        for rec in recs:
            conf = rec.get("confidence_score", 0.5)
            bucket = confidence_score_to_bucket(conf)
            trades.append(
                TradeRecord(
                    pnl=rec["pnl"],
                    confidence_bucket=bucket,
                    won=rec["pnl"] > 0,
                    pair=rec.get("pair", "UNKNOWN"),
                    window_id=rec.get("window_id", 0),
                )
            )
    return trades


def synthesize_ict_ab_trades(data_dir: str) -> list[TradeRecord]:
    """Build trade list from ict_ab walkforward results.

    Uses the baseline (all signals) vs ict_filtered (high confidence) split
    to distribute trades across confidence tiers.
    """
    trades = []
    files = sorted(glob.glob(os.path.join(data_dir, "walkforward_session_range_mr_ict_ab_*.json")))

    for filepath in files:
        pair_name = Path(filepath).stem.replace("walkforward_session_range_mr_ict_ab_", "").upper()
        with open(filepath) as f:
            data = json.load(f)

        for w in data.get("per_window", []):
            wid = w.get("window_id", 0)
            baseline = w.get("baseline", {})
            ict_filtered = w.get("ict_filtered", {})

            b_trades = baseline.get("trades", 0)
            i_trades = ict_filtered.get("trades", 0)
            b_pnl = baseline.get("total_pnl", 0)
            i_pnl = ict_filtered.get("total_pnl", 0)
            i_wr = ict_filtered.get("win_rate", 50) / 100.0
            b_wr = baseline.get("win_rate", 50) / 100.0
            rejected = baseline.get("rejected", b_trades - i_trades)

            # ICT-passed trades → tiers 3-5
            if i_trades > 0:
                avg_ict_pnl = i_pnl / i_trades
                for tier, frac in ICT_TIER_DISTRIBUTION.items():
                    n = max(round(i_trades * frac), 1)
                    tier_wr = min(i_wr * TIER_EDGE[tier], 0.92)
                    tier_wr = max(tier_wr, 0.20)
                    tier_avg_pnl = avg_ict_pnl * TIER_EDGE[tier]

                    for j in range(n):
                        won = (j / n) < tier_wr
                        if won:
                            # Winners get a bit more than average (positive skew)
                            pnl = abs(tier_avg_pnl) * (1.2 + (tier - 3) * 0.3)
                        else:
                            # Losers lose slightly less than average
                            pnl = -abs(tier_avg_pnl) * (0.7 + (tier - 3) * 0.1)
                        trades.append(
                            TradeRecord(
                                pnl=pnl,
                                confidence_bucket=tier,
                                won=won,
                                pair=pair_name,
                                window_id=wid,
                            )
                        )

            # ICT-rejected trades → tiers 1-2
            if rejected > 0:
                # Rejected trades have worse stats — estimate from remaining PnL
                rejected_pnl = b_pnl - i_pnl
                avg_rej_pnl = rejected_pnl / rejected if rejected > 0 else -5.0

                for tier, frac in REJECTED_TIER_DISTRIBUTION.items():
                    n = max(round(rejected * frac), 0)
                    if n == 0:
                        continue
                    tier_wr = max(b_wr * 0.6 * TIER_EDGE[tier], 0.15)
                    tier_avg_pnl = avg_rej_pnl * TIER_EDGE[tier]

                    for j in range(n):
                        won = (j / n) < tier_wr
                        pnl = abs(tier_avg_pnl) * (1.0 if won else -0.8)
                        trades.append(
                            TradeRecord(
                                pnl=pnl,
                                confidence_bucket=tier,
                                won=won,
                                pair=pair_name,
                                window_id=wid,
                            )
                        )

    return trades


# ── Simulation ──────────────────────────────────────────────────────────────


def simulate_tiers(
    trades: list[TradeRecord],
    config: TierConfig,
    initial_balance: float = 10_000.0,
) -> tuple[float, float, float, dict[int, float], dict[str, float]]:
    """Simulate P&L with tier-based risk sizing.

    Returns: (net_pnl, max_dd_pct, win_rate, per_tier_pnl, per_pair_pnl)
    """
    tier_risk = {
        1: config.tier1,
        2: config.tier2,
        3: config.tier3,
        4: config.tier4,
        5: config.tier5,
    }
    baseline_risk = config.tier1

    balance = initial_balance
    peak = initial_balance
    max_dd_pct = 0.0
    wins = 0
    per_tier_pnl: dict[int, float] = {}
    per_pair_pnl: dict[str, float] = {}

    for t in trades:
        risk_ratio = tier_risk[t.confidence_bucket] / baseline_risk
        scaled_pnl = t.pnl * risk_ratio

        balance += scaled_pnl
        per_tier_pnl[t.confidence_bucket] = per_tier_pnl.get(t.confidence_bucket, 0) + scaled_pnl
        per_pair_pnl[t.pair] = per_pair_pnl.get(t.pair, 0) + scaled_pnl

        if t.won:
            wins += 1

        if balance > peak:
            peak = balance
        dd_pct = (peak - balance) / peak if peak > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    win_rate = wins / len(trades) if trades else 0
    return balance - initial_balance, max_dd_pct, win_rate, per_tier_pnl, per_pair_pnl


# ── Sweep configurations ────────────────────────────────────────────────────

SWEEP_CONFIGS = [
    # Conservative (minimize DD, prioritize Sharpe)
    (0.50, 0.30, 0.20, 0.10, 0.10),
    (0.50, 0.30, 0.20, 0.15, 0.10),
    (0.50, 0.50, 0.20, 0.10, 0.10),
    (0.50, 0.30, 0.30, 0.10, 0.10),
    (0.75, 0.30, 0.20, 0.10, 0.10),
    (0.75, 0.50, 0.20, 0.10, 0.10),
    (0.75, 0.30, 0.30, 0.15, 0.10),
    (0.75, 0.50, 0.30, 0.15, 0.10),
    (0.75, 0.75, 0.30, 0.15, 0.10),
    # Balanced (near current defaults)
    (1.00, 0.50, 0.30, 0.15, 0.10),
    (1.00, 0.50, 0.30, 0.25, 0.10),
    (1.00, 0.75, 0.50, 0.25, 0.10),  # Current default
    (1.00, 0.75, 0.30, 0.15, 0.10),
    (1.00, 0.50, 0.20, 0.10, 0.10),
    (1.00, 0.75, 0.30, 0.10, 0.10),
    (1.00, 0.50, 0.50, 0.25, 0.10),
    (0.75, 0.75, 0.50, 0.25, 0.10),
    # Aggressive (push Tier 5)
    (1.25, 0.75, 0.50, 0.25, 0.10),
    (1.25, 0.50, 0.30, 0.15, 0.10),
    (1.50, 0.75, 0.50, 0.25, 0.10),
    (1.50, 0.50, 0.30, 0.15, 0.10),
    (1.50, 1.00, 0.50, 0.25, 0.10),
    (1.25, 1.00, 0.50, 0.25, 0.10),
    (1.25, 0.75, 0.30, 0.15, 0.10),
    (1.50, 0.75, 0.30, 0.15, 0.10),
    (1.25, 0.50, 0.20, 0.10, 0.10),
    (1.50, 0.50, 0.20, 0.10, 0.10),
    # Ultra-conservative
    (0.30, 0.20, 0.15, 0.10, 0.10),
    (0.50, 0.20, 0.15, 0.10, 0.10),
    # Flat low (all same low risk — control)
    (0.10, 0.10, 0.10, 0.10, 0.10),
]

SWEEP_CONFIGS = list(dict.fromkeys(SWEEP_CONFIGS))
CURRENT_DEFAULT = TierConfig(1.00, 0.75, 0.50, 0.25, 0.10)


def run_sweep(trades: list[TradeRecord], pair: str) -> list[SweepResult]:
    results = []
    for t5, t4, t3, t2, t1 in SWEEP_CONFIGS:
        config = TierConfig(t5, t4, t3, t2, t1)
        net_pnl, max_dd, wr, per_tier, per_pair = simulate_tiers(trades, config)

        sharpe = abs(net_pnl / max_dd) if max_dd > 0.001 else 0
        if net_pnl < 0:
            sharpe = -sharpe

        results.append(
            SweepResult(
                config=config,
                net_pnl=net_pnl,
                max_dd_pct=max_dd,
                sharpe_like=sharpe,
                win_rate=wr,
                trade_count=len(trades),
                dd_violation=max_dd > 0.05,
                pair=pair,
                per_tier_pnl=per_tier,
                per_pair_pnl=per_pair,
            )
        )
    return results


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    project_root = os.environ.get("AYUMI_ROOT", "$AYUMI_ROOT")
    data_dir = os.path.join(project_root, "data", "forex", "historical")

    # Try real trade data first
    real_trades = load_real_trades(project_root)
    use_real = len(real_trades) > 0

    print("=" * 80)
    print("  TIER OPTIMIZER — Confidence Tier Risk Percentage Sweep")
    print(f"  Data source: {'REAL walkforward trades' if use_real else 'SYNTHESIZED ict_ab results'}")
    print("=" * 80)
    print()

    if use_real:
        trades = real_trades
        # Also load synthesized for comparison
        synth_trades = synthesize_ict_ab_trades(data_dir)
    else:
        trades = synthesize_ict_ab_trades(data_dir)
        synth_trades = trades

    if not trades:
        print("ERROR: No trade data found (neither real nor synthesized).")
        return

    pairs = sorted(set(t.pair for t in trades))
    print(f"Synthesized {len(trades)} trades across pairs: {pairs}")
    for p in pairs:
        n = sum(1 for t in trades if t.pair == p)
        wins = sum(1 for t in trades if t.pair == p and t.won)
        print(f"  {p}: {n} trades, {wins}/{n} wins ({wins / n:.1%})")
    print()

    # ── Sweep: per-pair ─────────────────────────────────────────────────
    print("=" * 80)
    print("  PER-PAIR RESULTS")
    print("=" * 80)

    for pair in pairs:
        pair_trades = [t for t in trades if t.pair == pair]
        results = run_sweep(pair_trades, pair)
        valid = [r for r in results if not r.dd_violation and r.net_pnl > 0]
        best = max(valid, key=lambda r: r.sharpe_like) if valid else min(results, key=lambda r: r.max_dd_pct)

        pnl_d, dd_d, wr_d, _, _ = simulate_tiers(pair_trades, CURRENT_DEFAULT)

        print(f"\n--- {pair} ({len(pair_trades)} trades) ---")
        print(f"  BEST: {best.config.label()}")
        print(
            f"    PnL=${best.net_pnl:,.2f}  DD={best.max_dd_pct:.2%}  "
            f"Sharpe={best.sharpe_like:.2f}  WR={best.win_rate:.1%}  "
            f"{'✅' if not best.dd_violation else '❌'}"
        )
        print(
            f"  DEFAULT: PnL=${pnl_d:,.2f}  DD={dd_d:.2%}  "
            f"Sharpe={abs(pnl_d / dd_d) if dd_d > 0.001 and pnl_d > 0 else 0:.2f}"
        )
        print(f"  Δ Sharpe: {best.sharpe_like - (abs(pnl_d / dd_d) if dd_d > 0.001 and pnl_d > 0 else 0):+.2f}")

    # ── Sweep: overall ──────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  OVERALL RESULTS (all pairs combined)")
    print("=" * 80)

    all_results = run_sweep(trades, "ALL")
    valid = [r for r in all_results if not r.dd_violation and r.net_pnl > 0]

    print(f"\n  {len(valid)} configs passed (DD < 5%, PnL > 0) out of {len(all_results)} tested")
    print()

    if valid:
        top10 = sorted(valid, key=lambda r: r.sharpe_like, reverse=True)[:10]
        print("  TOP 10 configs:")
        for i, r in enumerate(top10, 1):
            marker = " ★" if i == 1 else ""
            print(f"  #{i}{marker}: {r.config.label()}")
            print(
                f"       PnL=${r.net_pnl:,.2f}  DD={r.max_dd_pct:.2%}  Sharpe={r.sharpe_like:.2f}  WR={r.win_rate:.1%}"
            )

        best = top10[0]
    else:
        best = min(all_results, key=lambda r: r.max_dd_pct)
        print("  ⚠️  No configs passed both DD < 5% and PnL > 0!")
        print(f"  Best by DD: {best.config.label()}  DD={best.max_dd_pct:.2%}")

    # Current default comparison
    pnl_d, dd_d, wr_d, pt_d, pp_d = simulate_tiers(trades, CURRENT_DEFAULT)
    sh_d = abs(pnl_d / dd_d) if dd_d > 0.001 and pnl_d > 0 else (-abs(pnl_d / dd_d) if dd_d > 0.001 else 0)

    print(f"\n  CURRENT DEFAULT: {CURRENT_DEFAULT.label()}")
    print(f"    PnL=${pnl_d:,.2f}  DD={dd_d:.2%}  Sharpe={sh_d:.2f}  WR={wr_d:.1%}")
    print(f"    {'✅ DD < 5%' if dd_d < 0.05 else '❌ DD > 5%'}")

    # ── Tier impact analysis ────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  TIER IMPACT ANALYSIS")
    print("=" * 80)

    # For each tier, find the marginal impact by comparing configs
    # that are identical except in that one tier
    tier_names = ["tier5", "tier4", "tier3", "tier2"]
    tier_labels = [
        "Tier 5 (85%+)",
        "Tier 4 (70-84%)",
        "Tier 3 (50-69%)",
        "Tier 2 (35-49%)",
    ]
    tier_sweep_values = [
        [0.50, 0.75, 1.00, 1.25, 1.50],
        [0.30, 0.50, 0.75],
        [0.20, 0.30, 0.50],
        [0.10, 0.15, 0.25],
    ]

    # Build a lookup from config tuple to result
    result_map = {}
    for r in all_results:
        result_map[r.config.as_tuple()] = r

    for i, (name, label, vals) in enumerate(  # noqa: B007
        zip(tier_names, tier_labels, tier_sweep_values)  # noqa: B905
    ):
        # For each group of configs sharing the same other 3 tiers,
        # measure how much Sharpe changes when we vary only this tier.
        groups: dict[tuple, list[SweepResult]] = {}
        for r in all_results:
            if r.pair != "ALL":
                continue
            key = tuple(v for j, v in enumerate(r.config.as_tuple()) if j != i)
            groups.setdefault(key, []).append(r)

        max_impact = 0.0
        best_val = vals[0]
        worst_val = vals[-1]
        avg_impact = 0.0
        n_groups = 0

        for key, group_results in groups.items():  # noqa: B007
            if len(group_results) < 2:
                continue
            sharpes = {r.config.as_tuple()[i]: r.sharpe_like for r in group_results}
            group_best_sharpe = max(sharpes.values())
            group_worst_sharpe = min(sharpes.values())
            impact = group_best_sharpe - group_worst_sharpe
            if impact > max_impact:
                max_impact = impact
                best_val = max(sharpes, key=sharpes.get)
                worst_val = min(sharpes, key=sharpes.get)
            avg_impact += impact
            n_groups += 1

        avg_impact = avg_impact / n_groups if n_groups > 0 else 0

        print(f"\n  {label}:")
        print(f"    Sweep values: {[f'{v:.2%}' for v in vals]}")
        print(f"    Max impact across groups: {max_impact:.0f} Sharpe (best={best_val:.2%}, worst={worst_val:.2%})")
        print(f"    Avg impact per group: {avg_impact:.0f} Sharpe")
        # Correlation: higher tier value → more Sharpe?
        print(f"    Direction: {'Higher = better' if best_val >= worst_val else 'Lower = better'}")

    # ── Per-tier PnL breakdown for best config ─────────────────────────
    print()
    print("=" * 80)
    print("  BEST CONFIG — PER-TIER P&L BREAKDOWN")
    print("=" * 80)

    tier_names_full = {
        5: "Tier 5 (85-100%)",
        4: "Tier 4 (70-84%)",
        3: "Tier 3 (50-69%)",
        2: "Tier 2 (35-49%)",
        1: "Tier 1 (20-34%)",
    }
    for k in sorted(best.per_tier_pnl.keys(), reverse=True):
        v = best.per_tier_pnl[k]
        n = sum(1 for t in trades if t.confidence_bucket == k)
        pct = abs(v) / abs(sum(best.per_tier_pnl.values())) * 100 if best.per_tier_pnl else 0
        sign = "+" if v >= 0 else ""
        print(f"  {tier_names_full[k]:20s}: {sign}${v:,.2f}  ({n} trades, {pct:.0f}% of total PnL)")

    # ── Recommendation ──────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  ★ RECOMMENDATION FOR FTMO")
    print("=" * 80)

    # Best with DD < 4% (safety margin)
    safe = [r for r in all_results if r.max_dd_pct < 0.04 and r.net_pnl > 0]
    if safe:
        rec = max(safe, key=lambda r: r.sharpe_like)
        margin = "DD < 4% (safety margin)"
    elif valid:
        rec = max(valid, key=lambda r: r.sharpe_like)
        margin = "DD < 5%"
    else:
        rec = best
        margin = "best available"

    print(f"\n  Recommended config ({margin}):")
    print(f"    {rec.config.label()}")
    print(f"    PnL=${rec.net_pnl:,.2f}  DD={rec.max_dd_pct:.2%}  Sharpe={rec.sharpe_like:.2f}  WR={rec.win_rate:.1%}")
    print(f"    {'✅' if not rec.dd_violation else '❌'}")

    print("\n  Comparison to current default:")
    print(f"    Δ PnL:   ${rec.net_pnl - pnl_d:+,.2f}")
    print(f"    Δ DD:    {rec.max_dd_pct - dd_d:+.2%}")
    print(f"    Δ Sharpe: {rec.sharpe_like - sh_d:+.2f}")

    print("\n  Key findings:")
    if rec.config.tier5 < CURRENT_DEFAULT.tier5:
        print(f"    • Tier 5 reduced from {CURRENT_DEFAULT.tier5:.2%} → {rec.config.tier5:.2%}")
    if rec.config.tier4 < CURRENT_DEFAULT.tier4:
        print(f"    • Tier 4 reduced from {CURRENT_DEFAULT.tier4:.2%} → {rec.config.tier4:.2%}")
    if rec.config.tier3 < CURRENT_DEFAULT.tier3:
        print(f"    • Tier 3 reduced from {CURRENT_DEFAULT.tier3:.2%} → {rec.config.tier3:.2%}")

    # ── Real vs Synthesized comparison ─────────────────────────────────
    if use_real and synth_trades:
        print()
        print("=" * 80)
        print("  REAL vs SYNTHESIZED COMPARISON")
        print("=" * 80)

        synth_results = run_sweep(synth_trades, "SYNTH")
        synth_valid = [r for r in synth_results if not r.dd_violation and r.net_pnl > 0]
        synth_best = max(synth_valid, key=lambda r: r.sharpe_like) if synth_valid else None

        if synth_best:
            print(f"\n  Synthesized best: {synth_best.config.label()}")
            print(
                f"    PnL=${synth_best.net_pnl:,.2f}  DD={synth_best.max_dd_pct:.2%}  Sharpe={synth_best.sharpe_like:.2f}"  # noqa: E501
            )
            print(f"\n  Real best: {rec.config.label()}")
            print(f"    PnL=${rec.net_pnl:,.2f}  DD={rec.max_dd_pct:.2%}  Sharpe={rec.sharpe_like:.2f}")
            print(f"\n  Δ Sharpe: {rec.sharpe_like - synth_best.sharpe_like:+.2f}")
            print(f"  Δ DD: {rec.max_dd_pct - synth_best.max_dd_pct:+.2%}")

            # Confidence distribution comparison
            print("\n  Confidence distribution (real):")
            for bucket in range(1, 6):
                n = sum(1 for t in real_trades if t.confidence_bucket == bucket)
                wins = sum(1 for t in real_trades if t.confidence_bucket == bucket and t.won)
                wr = wins / n if n > 0 else 0
                print(f"    Tier {bucket}: {n} trades, WR={wr:.1%}")

    print("\n  Caveats:")
    if use_real:
        print("    • Using real walkforward trade data with actual confidence scores")
    else:
        print("    • Trade distribution is synthesized (no per-trade confidence in stored results)")
        print("    • Run walkforward with updated runner to generate real trade records")


if __name__ == "__main__":
    main()
