"""TTC/TBD Signal & Confidence Engine — Phase 1-4."""

from .backtest_bridge import SignalEngineBridge
from .confluence_scorer import ConfluenceScorer
from .filters import ATRFilter, FilterChain, FVGFilter, TrendFilter
from .gate_validator import GateValidator
from .htf_analyzer import HTFAnalyzer
from .level_counter import LevelCounter
from .pattern_detector import PatternDetector
from .session_logic import SessionAnalyzer
from .signal_output import create_signal, format_signal_json, parse_signal_json
from .stop_target import StopTargetCalculator
from .swing_detector import SwingDetector
from .tp_manager import TPManager

BacktestBridge = SignalEngineBridge  # alias

from .data_types import (  # noqa: I001
    HTFPhase,
    HTFState,
    Level,
    LevelType,
    SessionState,
    Signal,
    Swing,
    SwingType,
)

__all__ = [
    "SwingDetector",
    "LevelCounter",
    "HTFAnalyzer",
    "SessionAnalyzer",
    "PatternDetector",
    "GateValidator",
    "ConfluenceScorer",
    "format_signal_json",
    "parse_signal_json",
    "create_signal",
    "StopTargetCalculator",
    "TPManager",
    "SignalEngineBridge",
    "BacktestBridge",
    "Signal",
    "Level",
    "Swing",
    "HTFState",
    "SessionState",
    "LevelType",
    "SwingType",
    "HTFPhase",
    # Filters
    "TrendFilter",
    "ATRFilter",
    "FVGFilter",
    "FilterChain",
]
