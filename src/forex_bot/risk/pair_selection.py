"""Pair selection policy enforcing cluster diversity for FTMO compliance.

Implements the 4-cluster correlation framework from SRB-AYUMI-001 (Satoshi,
Tier 1, 2026-07-01).  The FTMO 10-pair portfolio collapses to 4 correlation
clusters; trading more than 2 pairs from any single cluster is functionally
a single oversized position that violates the 3%/10% drawdown limits.

Usage
-----
    policy = PairSelectionPolicy()
    result = policy.validate_portfolio(["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])
    if not result.is_valid:
        print(result.summary)

This module is deliberately independent of the live trade execution path.
Integration with the execution layer is a separate card.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ------------------------------------------------------------------ #
# Cluster definitions (SRB-AYUMI-001 §2)
# ------------------------------------------------------------------ #
# A pair may appear in multiple clusters (e.g. USDCAD is both USD-strong
# and commodity-linked).  This is intentional — it captures the reality
# that CAD is influenced by both USD regime and commodity flows.

DEFAULT_CLUSTERS: dict[str, set[str]] = {
    "usd_weak_majors": {"EURUSD", "GBPUSD"},
    "usd_strong": {"USDJPY", "USDCHF", "USDCAD"},
    "commodity_linked": {"AUDUSD", "XAUUSD", "USDCAD"},
    "jpy_crosses": {"GBPJPY", "EURJPY", "EURGBP"},
}

DEFAULT_MAX_PER_CLUSTER: int = 2


@dataclass(frozen=True)
class ClusterViolation:
    """A single cluster that exceeds the max-pairs threshold."""

    cluster_name: str
    pairs: list[str]
    max_allowed: int

    @property
    def excess(self) -> int:
        return len(self.pairs) - self.max_allowed

    def __str__(self) -> str:
        return (
            f"Cluster '{self.cluster_name}' has {len(self.pairs)} pairs "
            f"({', '.join(sorted(self.pairs))}), max allowed is {self.max_allowed}"
        )


@dataclass
class PairSelectionResult:
    """Outcome of validating a portfolio against the pair selection policy."""

    is_valid: bool
    violations: list[ClusterViolation] = field(default_factory=list)
    cluster_usage: dict[str, list[str]] = field(default_factory=dict)
    total_pairs: int = 0

    @property
    def summary(self) -> str:
        if self.is_valid:
            return f"Portfolio valid: {self.total_pairs} pairs across {len(self.cluster_usage)} clusters."
        lines = [f"Portfolio INVALID: {len(self.violations)} cluster violation(s)."]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


@dataclass
class PairSelectionPolicy:
    """Cluster-based pair selection policy for FTMO portfolio compliance.

    Parameters
    ----------
    clusters
        Mapping of cluster name -> set of pair symbols in that cluster.
        Defaults to the SRB-AYUMI-001 4-cluster mapping.
    max_per_cluster
        Maximum number of pairs allowed from any single cluster.
        Default 2 per FTMO risk guidelines.
    """

    clusters: dict[str, set[str]] = field(default_factory=lambda: {k: set(v) for k, v in DEFAULT_CLUSTERS.items()})
    max_per_cluster: int = DEFAULT_MAX_PER_CLUSTER

    # ------------------------------------------------------------------ #
    # Core logic
    # ------------------------------------------------------------------ #
    def get_cluster_usage(self, pairs: list[str]) -> dict[str, list[str]]:
        """Return a mapping of cluster name -> list of active pairs in that cluster.

        Only clusters that have at least one active pair are included.
        """
        pair_set = {p.upper() for p in pairs}
        usage: dict[str, list[str]] = {}
        for cluster_name, cluster_pairs in self.clusters.items():
            active = sorted(pair_set & cluster_pairs)
            if active:
                usage[cluster_name] = active
        return usage

    def validate_portfolio(self, pairs: list[str]) -> PairSelectionResult:
        """Validate a portfolio against the max-per-cluster rule.

        Parameters
        ----------
        pairs
            List of pair symbols (case-insensitive).

        Returns
        -------
        PairSelectionResult with ``is_valid=True`` if all clusters are
        within the limit, or ``is_valid=False`` with violations listed.
        """
        usage = self.get_cluster_usage(pairs)
        violations: list[ClusterViolation] = []

        for cluster_name, cluster_pairs in usage.items():
            if len(cluster_pairs) > self.max_per_cluster:
                violations.append(
                    ClusterViolation(
                        cluster_name=cluster_name,
                        pairs=cluster_pairs,
                        max_allowed=self.max_per_cluster,
                    )
                )

        return PairSelectionResult(
            is_valid=len(violations) == 0,
            violations=violations,
            cluster_usage=usage,
            total_pairs=len({p.upper() for p in pairs}),
        )

    def flag_over_clustered(self, pairs: list[str]) -> list[str]:
        """Return a list of cluster names that exceed the max-pairs threshold.

        Convenience method for quick checks.
        """
        result = self.validate_portfolio(pairs)
        return [v.cluster_name for v in result.violations]

    def find_cluster(self, pair: str) -> list[str]:
        """Return all clusters that a given pair belongs to.

        A pair can belong to multiple clusters (e.g. USDCAD belongs to
        both ``usd_strong`` and ``commodity_linked``).
        """
        p = pair.upper()
        return [name for name, members in self.clusters.items() if p in members]

    def get_unassigned_pairs(self, pairs: list[str]) -> list[str]:
        """Return pairs that do not belong to any defined cluster.

        Useful for identifying new pairs that may need cluster assignment.
        """
        pair_set = {p.upper() for p in pairs}
        all_clustered: set[str] = set()
        for members in self.clusters.values():
            all_clustered |= members
        return sorted(pair_set - all_clustered)
