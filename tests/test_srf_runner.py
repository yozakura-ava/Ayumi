"""Tests for the SRF runner hypothesis doc gate.

Verifies that StrategyRunner refuses to start a sweep when the strategy's
hypothesis doc is missing, and proceeds past the gate when it exists.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Path setup ──────────────────────────────────────────────────────────
# The SRF module lives under src/forex_bot/ which is not a standard
# installable package. We add it to sys.path so imports work in the
# test environment.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src" / "forex_bot"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from srf.runner import StrategyRunner

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a temporary directory simulating a repo root."""
    return tmp_path


@pytest.fixture
def runner(tmp_repo: Path) -> StrategyRunner:
    """StrategyRunner pointed at a temp repo (DB path is irrelevant — we
    never get far enough to open it)."""
    return StrategyRunner(db_path=str(tmp_repo / "nonexistent.duckdb"), repo_path=str(tmp_repo))


# ── Tests ───────────────────────────────────────────────────────────────


class TestHypothesisGate:
    """Acceptance criteria 1-3: runner refuses without doc, passes with doc."""

    def test_hypothesis_gate_rejects_missing_doc(self, runner: StrategyRunner):
        """Runner raises RuntimeError when hypothesis doc is absent.

        Simulates git-clean check passing (returns a commit hash) so the
        hypothesis gate is the first guard that can fail.
        """
        with patch.object(runner, "_get_git_commit", return_value="abc1234"):
            with pytest.raises(RuntimeError) as exc_info:
                runner.run(
                    strategy_name="nonexistent_strategy",
                    strategy_factory=object,
                    pair="EURUSD",
                    timeframe=60,
                    data_path="dummy.csv",
                )

        msg = str(exc_info.value)
        # Must mention the strategy name and the missing doc path
        assert "nonexistent_strategy" in msg
        assert "hypothesis" in msg.lower()
        # Must explain WHY (acceptance criterion 2)
        assert "post-hoc rationalization" in msg.lower()

    def test_hypothesis_gate_passes_with_doc(self, runner: StrategyRunner, tmp_repo: Path):
        """Runner proceeds past the hypothesis gate when the doc exists.

        We create the doc, mock git-clean to pass, and verify the runner
        advances to the data-loading step (where it fails with a file-not-found
        error — proving the hypothesis gate let it through).
        """
        strategy_name = "test_edge"
        edges_dir = tmp_repo / "docs" / "edges"
        edges_dir.mkdir(parents=True)
        (edges_dir / f"{strategy_name}-hypothesis.md").write_text(
            "# Test Edge Hypothesis\n\nThis is a pre-registered hypothesis.\n"
        )

        with patch.object(runner, "_get_git_commit", return_value="abc1234"):
            with pytest.raises((FileNotFoundError, OSError, RuntimeError)) as exc_info:
                runner.run(
                    strategy_name=strategy_name,
                    strategy_factory=object,
                    pair="EURUSD",
                    timeframe=60,
                    data_path="nonexistent_data.csv",
                )

        msg = str(exc_info.value)
        # The error should be about the data file, NOT about hypothesis
        assert "hypothesis" not in msg.lower(), (
            f"Hypothesis gate should have passed but error mentions hypothesis: {msg}"
        )


class TestHypothesisGatePathResolution:
    """Verify the gate checks the correct path under repo_root."""

    def test_gate_checks_docs_edges_subdirectory(self, runner: StrategyRunner, tmp_repo: Path):
        """Hypothesis doc must be under docs/edges/{strategy_name}-hypothesis.md.

        Creating the doc in a wrong location should still trigger the gate.
        """
        # Create doc in wrong location (docs/ instead of docs/edges/)
        wrong_dir = tmp_repo / "docs"
        wrong_dir.mkdir(parents=True)
        (wrong_dir / "my_edge-hypothesis.md").write_text("wrong location")

        with patch.object(runner, "_get_git_commit", return_value="abc1234"):
            with pytest.raises(RuntimeError) as exc_info:
                runner.run(
                    strategy_name="my_edge",
                    strategy_factory=object,
                    pair="GBPUSD",
                    timeframe=240,
                    data_path="dummy.csv",
                )

        assert "hypothesis" in str(exc_info.value).lower()
