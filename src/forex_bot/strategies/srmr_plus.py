from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from backtest.strategies.isignal_strategy import ISignalStrategy
from config.sessions import SessionRangeHours
from core.types import (
    Bar,
    MarketState,
    SessionType,
    StrategySignal,
    TradeDirection,
)
from utils.pip_value import DEFAULT_PIP, JPY_PIP, pip_value_for_symbol

_DEFAULT_STRATEGIES_YAML = Path("src/forex_bot/config/strategies.yaml")

try:
    from overlays.dxy_regime_overlay import DxyBar, DxyRegimeOverlay
except ImportError:
    DxyRegimeOverlay = None  # type: ignore[assignment,misc]
    DxyBar = None  # type: ignore[assignment,misc]

NO_SIGNAL = None


@dataclass(frozen=True)
class SRMRPlusConfig:
    # Tuned per research §A.5 (strategy-optimization-research.md)
    atr_period: int = 14
    rsi_period: int = 14
    rsi_long_level: float = 30.0  # was 35.0 — tighter oversold requirement, fewer better signals
    rsi_short_level: float = 70.0  # was 65.0 — tighter overbought requirement
    adx_period: int = 14
    adx_max_threshold: float = 20.0  # was 25.0 — only fire in low-trend conditions
    session_range_min_pips: float = 10.0  # was 15.0 — allow quieter sessions (especially EURUSD M15)
    entry_near_extreme_pips: float = 8.0  # was 15.0 — tighter proximity = more exhaustion, less mid-range
    hard_cap_sl_pips: float = 18.0  # was 25.0 — tighter cap for mean reversion
    tp1_rr: float = 1.5  # was 1.0; raised to pass min_risk_reward=1.5 gate
    tp2_rr: float = 1.5
    ema_trend_period: int = 50
    use_same_day_range: bool = False
    pip_value: float | None = None
    symbol: str | None = None  # set to enable symbol-aware pip-size lookup
    dxy_overlay: bool = False  # enable DXY regime confidence adjustment
    # Require at least N bars to pass since the price last touched the
    # session range extreme. Default 0 = disabled. Set to 3+ to enforce
    # multi-bar reversal confirmation per research §A.5 ("NEW: minimum
    # bars since range extreme touch — bars_since_touch > 3"). When > 0,
    # the strategy walks back through ``state.bars`` to find the most
    # recent bar that touched the relevant extreme and rejects entries
    # whose bars-since-touch is below the threshold.
    min_bars_since_extreme_touch: int = 0


_LONDON_START = SessionRangeHours.LONDON_START
_LONDON_END = SessionRangeHours.LONDON_END
_NY_OPEN_START = SessionRangeHours.NY_OPEN_START
_NY_OPEN_END = SessionRangeHours.NY_OPEN_END
_LONDON_NY_OVERLAP_START = SessionRangeHours.LONDON_NY_OVERLAP_START
_LONDON_NY_OVERLAP_END = SessionRangeHours.LONDON_NY_OVERLAP_END
_NY_CLOSE_START = SessionRangeHours.NY_CLOSE_START
_NY_CLOSE_END = SessionRangeHours.NY_CLOSE_END

logger = logging.getLogger(__name__)

_MIN_SL_PIPS = 5.0  # Minimum SL distance in pips


# ── strategies.yaml config loader (card 25cbea7a) ──────────────────────────
# The forward test launcher historically hard-coded SRMRPlusConfig() defaults,
# producing 0 signals for XAUUSD because the validated Optuna-tuned params
# (PF=7.16, WR=73.4%) live in src/forex_bot/config/strategies.yaml and were
# never loaded. This helper resolves an SRMRPlusConfig from the YAML so the
# validated config is used in production. Falls back to ``None`` so callers
# can keep their default-config behavior when no validated entry exists for
# the requested symbol/timeframe.
_STRATEGY_TYPE = "srmr_plus"
_VALID_FIELDS = {f.name for f in fields(SRMRPlusConfig)}


def _resolve_strategies_yaml_path(config_path: Path | str | None) -> Path:
    """Return an absolute path to ``strategies.yaml``.

    Tries ``config_path`` first (relative paths resolved against CWD), then
    walks upward from this module looking for the conventional
    ``src/forex_bot/config/strategies.yaml`` location so the helper works
    whether invoked from the repo root, from ``scripts/``, or from inside
    a test under ``tests/test_signal_engine/``.
    """
    if config_path is not None:
        candidate = Path(config_path)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if candidate.exists():
            return candidate

    module_path = Path(__file__).resolve()
    for parent in module_path.parents:
        candidate = parent / "config" / "strategies.yaml"
        if candidate.exists():
            return candidate
    return Path("src/forex_bot/config/strategies.yaml")


def _find_strategy_entry(
    raw: dict[str, Any],
    *,
    config_id: str,
) -> dict[str, Any] | None:
    """Locate the entry in ``forward_test.strategies`` matching ``config_id``.

    Returns the raw entry dict (with ``params`` and friends) or ``None``.
    Only entries with ``type: srmr_plus`` are considered so a future
    non-SRMR strategy that reuses the id pattern doesn't get returned.
    """
    forward_test = raw.get("forward_test", {}) if isinstance(raw, dict) else {}
    strategies = forward_test.get("strategies", []) or []
    for entry in strategies:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != _STRATEGY_TYPE:
            continue
        if entry.get("id") != config_id:
            continue
        return entry
    return None


def load_srmr_config_from_yaml(
    symbol: str,
    *,
    timeframe: str = "M15",
    config_path: Path | str | None = None,
) -> SRMRPlusConfig | None:
    """Load an ``SRMRPlusConfig`` from ``strategies.yaml`` for the given symbol.

    Looks up the entry ``forward_test.strategies[*]`` whose ``id`` matches
    ``srmr_{symbol.lower()}_{timeframe.lower()}`` and ``type`` is
    ``srmr_plus``. Returns an ``SRMRPlusConfig`` populated with the entry's
    ``params`` dict (filtered to known dataclass fields, with ``symbol``
    forced to the requested value so ``pip_value_for_symbol`` keeps working)
    or ``None`` if no enabled, validated entry exists — the caller is then
    expected to fall back to defaults.

    Parameters
    ----------
    symbol : str
        Trading symbol, case-insensitive (e.g. ``"XAUUSD"``).
    timeframe : str
        Strategy timeframe, case-insensitive (e.g. ``"M15"``).
    config_path : Path | str | None
        Optional explicit path to ``strategies.yaml``. When ``None``, the
        helper searches upward from this module for the conventional
        location.

    Returns
    -------
    SRMRPlusConfig | None
        Configured dataclass instance or ``None`` when no matching entry
        is present (the caller must fall back to defaults).
    """
    config_id = f"srmr_{symbol.lower()}_{timeframe.lower()}"
    yaml_path = _resolve_strategies_yaml_path(config_path)

    try:
        with open(yaml_path) as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.warning(
            "strategies.yaml not found at %s — falling back to SRMR+ defaults",
            yaml_path,
        )
        return None
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "Failed to load strategies.yaml (%s): %s — falling back to defaults",
            yaml_path,
            exc,
        )
        return None

    entry = _find_strategy_entry(raw, config_id=config_id)
    if entry is None:
        logger.info(
            "No SRMR+ config entry '%s' in %s — falling back to defaults",
            config_id,
            yaml_path,
        )
        return None

    if not entry.get("enabled", True):
        logger.info(
            "SRMR+ config entry '%s' is disabled in %s — falling back to defaults",
            config_id,
            yaml_path,
        )
        return None

    params = dict(entry.get("params", {}) or {})
    # Drop keys that aren't SRMRPlusConfig fields so the constructor can't
    # raise ``TypeError: unexpected keyword argument`` on schema drift.
    unknown = sorted(set(params) - _VALID_FIELDS)
    if unknown:
        logger.debug(
            "Ignoring unknown SRMR+ config keys for '%s': %s",
            config_id,
            unknown,
        )
        for key in unknown:
            params.pop(key, None)

    # Force ``symbol`` so symbol-aware pip-size lookup resolves correctly
    # regardless of whether the YAML entry carried it.
    params["symbol"] = symbol

    logger.info(
        "Loaded SRMR+ config '%s' for %s %s from %s",
        config_id,
        symbol,
        timeframe,
        yaml_path,
    )
    return SRMRPlusConfig(**params)


def _resolve_pip_size(symbol: str | None, price: float) -> float:
    """Resolve pip size for a bar using symbol when available.

    Prefers the symbol-name lookup (``utils.pip_value.pip_value_for_symbol``),
    which is the correct, bug-free path (the old price heuristic mis-classified
    XAUUSD as a JPY pair).

    Fallback (when ``symbol`` is None/empty):
    - Forex-range prices (<50): use the legacy price-based heuristic, which is
      correct for non-JPY (<10) and JPY (50-300) pairs.
    - Gold/silver-range prices (>=50 and <10000): raise. These are ambiguous
      without a symbol — could be XAUUSD (pip=0.1), XAGUSD (pip=0.001),
      BTCUSD (pip=1.0), or JPY (pip=0.01). The old heuristic returned the
      JPY value, which corrupted every XAUUSD backtest result.
    """
    if symbol:
        return pip_value_for_symbol(symbol)
    if price >= 50:
        # The price is in gold/silver/BTC territory — we cannot pick a safe
        # default without a symbol. This is the bug we are fixing: callers
        # MUST configure SRMRPlusConfig.symbol for non-forex instruments.
        raise ValueError(
            f"SRMR+ cannot determine pip size for price={price} without a "
            f"symbol. Set SRMRPlusConfig.symbol (e.g. 'XAUUSD') and retry. "
            f"See https://... for migration steps."
        )
    logger.debug(
        "SRMR+ legacy price heuristic (no symbol configured, price=%.5f)",
        price,
    )
    return DEFAULT_PIP


# ---------------------------------------------------------------------------
# Deprecated: backward-compat shim for tests/callers that imported the old
# price-based helper. Emits a DeprecationWarning so we can find stragglers and
# remove this in a follow-up cleanup pass.
# ---------------------------------------------------------------------------


def _pip_value_for_price(price: float) -> float:  # pragma: no cover - shim
    """DEPRECATED: use ``utils.pip_value.pip_value_for_symbol`` instead.

    The price-based heuristic mis-classifies XAUUSD as JPY because gold's
    ~1900-2200 price range triggers the ``price >= 50`` branch. Retained
    only for backward-compat with existing tests; new code must set
    ``SRMRPlusConfig.symbol`` and rely on ``_resolve_pip_size``.
    """
    warnings.warn(
        "_pip_value_for_price is deprecated and mis-classifies XAUUSD; "
        "use utils.pip_value.pip_value_for_symbol instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    if price >= 50:
        return JPY_PIP
    return DEFAULT_PIP


def _is_trading_session(bar_time: datetime) -> bool:
    utc_hour = bar_time.hour
    return (
        _LONDON_START.hour <= utc_hour < _LONDON_END.hour
        or _NY_OPEN_START.hour <= utc_hour < _NY_OPEN_END.hour
        or _LONDON_NY_OVERLAP_START.hour <= utc_hour < _LONDON_NY_OVERLAP_END.hour
    )


def _get_bar_session_type(bar_time: datetime) -> SessionType:
    utc_hour = bar_time.hour
    if _LONDON_START.hour <= utc_hour < _LONDON_END.hour:
        return SessionType.LONDON
    if _NY_OPEN_START.hour <= utc_hour < _NY_OPEN_END.hour:
        return SessionType.NY_AM
    if _LONDON_NY_OVERLAP_START.hour <= utc_hour < _LONDON_NY_OVERLAP_END.hour:
        return SessionType.NY_AM
    if _NY_CLOSE_START.hour <= utc_hour < _NY_CLOSE_END.hour:
        return SessionType.NY_PM
    return SessionType.OUTSIDE


def _calculate_atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0001
    tr_sum = 0.0
    count = 0
    for i in range(len(bars) - period, len(bars)):
        if i > 0:
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            tr_sum += tr
            count += 1
    return tr_sum / count if count > 0 else 0.0001


def _calculate_ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def _calculate_rsi(bars: list[Bar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(len(bars) - period, len(bars)):
        change = bars[i].close - bars[i - 1].close
        gains.append(change if change > 0 else 0.0)
        losses.append(abs(change) if change < 0 else 0.0)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calculate_adx(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period * 2 + 1:
        return 0.0

    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]

    plus_dm_list: list[float] = []
    minus_dm_list: list[float] = []
    tr_list: list[float] = []

    for i in range(1, len(bars)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

        high_diff = highs[i] - highs[i - 1]
        low_diff = lows[i - 1] - lows[i]

        plus_dm = high_diff if (high_diff > low_diff and high_diff > 0) else 0.0
        minus_dm = low_diff if (low_diff > high_diff and low_diff > 0) else 0.0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    if len(tr_list) < period:
        return 0.0

    tr_sum = sum(tr_list[:period])
    plus_dm_sum = sum(plus_dm_list[:period])
    minus_dm_sum = sum(minus_dm_list[:period])

    if tr_sum == 0:
        return 0.0

    plus_di = (plus_dm_sum / tr_sum) * 100
    minus_di = (minus_dm_sum / tr_sum) * 100

    if plus_di + minus_di == 0:
        dx = 0.0
    else:
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

    dx_list: list[float] = [dx]
    for i in range(period, len(tr_list)):
        tr_sum = tr_sum - tr_sum / period + tr_list[i]
        plus_dm_sum = plus_dm_sum - plus_dm_sum / period + plus_dm_list[i]
        minus_dm_sum = minus_dm_sum - minus_dm_sum / period + minus_dm_list[i]

        if tr_sum == 0:
            dx_list.append(0.0)
            continue

        plus_di = (plus_dm_sum / tr_sum) * 100
        minus_di = (minus_dm_sum / tr_sum) * 100
        if plus_di + minus_di == 0:
            dx_list.append(0.0)
        else:
            dx_list.append(100.0 * (abs(plus_di - minus_di) / (plus_di + minus_di)))

    if len(dx_list) < period:
        return 0.0

    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period

    return adx


def _calculate_session_range(
    bars: list[Bar], session_type: SessionType, reference_day: date
) -> tuple[float, float, float]:
    if not bars:
        return 0.0, 0.0, 0.0

    session_bars: list[Bar] = []
    for b in bars:
        if b.time.date() != reference_day:
            continue
        if _get_bar_session_type(b.time) == session_type:
            session_bars.append(b)

    if not session_bars:
        return 0.0, 0.0, 0.0

    high = max(b.high for b in session_bars)
    low = min(b.low for b in session_bars)
    mean = sum(b.close for b in session_bars) / len(session_bars)
    return high, low, mean


def _get_previous_session_range(
    bars: list[Bar], current_day: date, current_session: SessionType
) -> tuple[float, float, float]:
    if current_session == SessionType.LONDON:
        prev_day = _find_previous_trading_day(bars, current_day)
        if prev_day is None:
            return 0.0, 0.0, 0.0
        high, low, mean = _calculate_session_range(bars, SessionType.LONDON, prev_day)
        if high == 0:
            high, low, mean = _calculate_session_range(bars, SessionType.NY_AM, prev_day)
        return high, low, mean

    if current_session == SessionType.NY_AM:
        high, low, mean = _calculate_session_range(bars, SessionType.LONDON, current_day)
        if high == 0:
            prev_day = _find_previous_trading_day(bars, current_day)
            if prev_day is None:
                return 0.0, 0.0, 0.0
            high, low, mean = _calculate_session_range(bars, SessionType.LONDON, prev_day)
        return high, low, mean

    prev_day = _find_previous_trading_day(bars, current_day)
    if prev_day is None:
        return 0.0, 0.0, 0.0
    high, low, mean = _calculate_session_range(bars, SessionType.LONDON, prev_day)
    if high == 0:
        high, low, mean = _calculate_session_range(bars, SessionType.NY_AM, prev_day)
    return high, low, mean


def _find_previous_trading_day(bars: list[Bar], current_day: date) -> date | None:
    seen_days: set[date] = set()
    for b in bars:
        d = b.time.date()
        if d < current_day:
            seen_days.add(d)
    if not seen_days:
        return None
    return max(seen_days)


def _build_signal(
    direction: TradeDirection,
    entry: float,
    atr: float,
    config: SRMRPlusConfig,
    session_range_price: float,
    adx: float,
    rsi: float,
    rationale: str,
    pip: float,
    spread_price: float = 0.0,
) -> StrategySignal | None:
    """Build a StrategySignal with TP anchored to the broker fill price.

    The strategy's ``entry`` is the mid-price (Bar.close). For BUY orders the
    broker fills at ASK = mid + half-spread; for SELL at BID = mid - half-spread.
    TP/SL are computed relative to the fill price so the broker accepts the
    order (TP > entry for BUY, TP < entry for SELL) — prevents
    TRADING_BAD_STOPS rejections on wide-spread symbols like XAUUSD.

    Args:
        spread_price: full bid/ask spread in price units. Default 0.0 keeps
            legacy behavior (TP/SL relative to mid) for callers that don't
            supply bar-level spread info.
    """
    if atr <= 0:
        logger.debug("SRMR+ _build_signal: ATR is zero or negative")
        return None

    sl_distance = min(
        session_range_price * 0.6,
        config.hard_cap_sl_pips * pip,
    )

    # Enforce minimum SL distance (5 pips) to prevent tiny stops
    min_sl = _MIN_SL_PIPS * pip
    if sl_distance < min_sl:
        sl_distance = min_sl

    if sl_distance <= 0:
        logger.debug("SRMR+ _build_signal: SL distance is zero or negative")
        return None

    sl = entry - sl_distance if direction == TradeDirection.LONG else entry + sl_distance

    # Anchor TP baseline to the broker fill price (ASK for BUY, BID for SELL)
    # so the broker always sees TP > fill for BUY / TP < fill for SELL.
    half_spread = spread_price / 2.0
    if direction == TradeDirection.LONG:
        tp_baseline = entry + half_spread  # ASK
    else:
        tp_baseline = entry - half_spread  # BID

    risk = sl_distance
    tp1 = tp_baseline + risk * config.tp1_rr if direction == TradeDirection.LONG else tp_baseline - risk * config.tp1_rr
    tp2 = tp_baseline + risk * config.tp2_rr if direction == TradeDirection.LONG else tp_baseline - risk * config.tp2_rr

    # Guard clause: ensure TP direction is consistent with trade direction
    # relative to the strategy's mid-price entry. Defense-in-depth for edge
    # cases (zero/negative spread, NaN, sl_distance exactly at half-spread).
    if direction == TradeDirection.LONG and tp1 <= entry:
        logger.warning(
            "SRMR+ _build_signal: LONG tp1=%.5f <= entry=%.5f after spread anchoring "
            "(spread=%.5f, sl_distance=%.5f) — dropping signal",
            tp1,
            entry,
            spread_price,
            sl_distance,
        )
        return None
    if direction == TradeDirection.SHORT and tp1 >= entry:
        logger.warning(
            "SRMR+ _build_signal: SHORT tp1=%.5f >= entry=%.5f after spread anchoring "
            "(spread=%.5f, sl_distance=%.5f) — dropping signal",
            tp1,
            entry,
            spread_price,
            sl_distance,
        )
        return None

    confidence = 0.55 + (0.15 * (1.0 - adx / config.adx_max_threshold))
    confidence = min(0.80, max(0.40, confidence))

    return StrategySignal(
        direction=direction,
        confidence=confidence,
        entry_price=entry,
        stop_loss=sl,
        take_profit_1=tp1,
        take_profit_2=tp2,
        take_profit_3=tp2,
        rationale=rationale,
    )


class SRMRPlusStrategy(ISignalStrategy):
    def __init__(
        self,
        config: SRMRPlusConfig | None = None,
        name: str = "SRMR+",
    ) -> None:
        super().__init__()
        self.config = config or SRMRPlusConfig()
        # `name` allows multiple per-symbol/timeframe instances to coexist
        # in the engine's strategy registry (keyed by ``s.name``).  The
        # default "SRMR+" preserves backward compatibility for callers
        # that don't pass an explicit name.
        self._name = name
        self._dxy_overlay: DxyRegimeOverlay | None = None
        if self.config.dxy_overlay and DxyRegimeOverlay is not None:
            self._dxy_overlay = DxyRegimeOverlay()
            logger.info("SRMR+ initialized with DXY regime overlay")

    @property
    def name(self) -> str:
        return self._name

    def initialize(self, config: dict | None = None) -> None:
        """Initialize the SRMR+ strategy."""
        super().initialize(config)
        logger.info(
            "SRMRPlusStrategy initialized: rsi_long=%.1f rsi_short=%.1f adx_max=%.1f",
            self.config.rsi_long_level,
            self.config.rsi_short_level,
            self.config.adx_max_threshold,
        )

    def shutdown(self) -> None:
        """Clean up after SRMR+ run."""
        logger.info(
            "SRMRPlusStrategy shutdown (processed %d bars)",
            self._bars_processed,
        )
        super().shutdown()

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        min_required = max(
            self.config.atr_period + self.config.rsi_period + 2,
            self.config.adx_period * 2 + 1,
            self.config.ema_trend_period + 1,
        )
        if len(state.bars) < min_required:
            logger.info(
                "SRMR+ %s: insufficient bars (have=%d, need=%d)",
                getattr(state.bars[-1], "symbol", "?") if state.bars else "?",
                len(state.bars),
                min_required,
            )
            return None

        latest = state.latest_bar
        if not _is_trading_session(latest.time):
            logger.info(
                "SRMR+ %s: outside trading hours (hour=%d)",
                getattr(latest, "symbol", "?"),
                latest.time.hour,
            )
            return None

        current_session = _get_bar_session_type(latest.time)
        current_day = latest.time.date()

        session_high, session_low, session_mean = _get_previous_session_range(state.bars, current_day, current_session)
        if session_high == 0:
            logger.debug(
                "SRMR+ %s: no previous session range (day=%s session=%s)",
                getattr(latest, "symbol", "?"),
                current_day,
                current_session,
            )
            return None

        session_range_price = session_high - session_low
        pip = (
            self.config.pip_value
            if self.config.pip_value is not None
            else _resolve_pip_size(self.config.symbol, latest.close)
        )
        session_range_width = session_range_price / pip
        if session_range_width < self.config.session_range_min_pips:
            logger.debug(
                "SRMR+ %s: session range too narrow (%.1f pips < %.1f min)",
                getattr(latest, "symbol", "?"),
                session_range_width,
                self.config.session_range_min_pips,
            )
            return None

        adx = _calculate_adx(state.bars, self.config.adx_period)
        if adx > self.config.adx_max_threshold:
            logger.debug(
                "SRMR+ %s: ADX too high (%.1f > %.1f)",
                getattr(latest, "symbol", "?"),
                adx,
                self.config.adx_max_threshold,
            )
            return None

        atr = _calculate_atr(state.bars, self.config.atr_period)
        if atr <= 0:
            logger.debug(
                "SRMR+ %s: ATR is zero or negative (%.6f)",
                getattr(latest, "symbol", "?"),
                atr,
            )
            return None

        rsi = _calculate_rsi(state.bars, self.config.rsi_period)
        if rsi is None:
            logger.debug(
                "SRMR+ %s: RSI calculation returned None (bars=%d, period=%d)",
                getattr(latest, "symbol", "?"),
                len(state.bars),
                self.config.rsi_period,
            )
            return None

        price = latest.close
        entry_near_extreme_pips = self.config.entry_near_extreme_pips * pip

        def _bars_since_extreme_touch(extreme: float, side: str) -> int:
            """Walk back from current bar to find most recent bar that
            touched the session-range extreme. Returns bar distance
            (0 = current bar touched). Returns ``len(state.bars)`` if no
            recent touch found (no constraint).
            """
            threshold = extreme + entry_near_extreme_pips
            if side == "low":
                # Touched the low = bar.low <= session_low + tolerance
                for i in range(len(state.bars) - 1, -1, -1):
                    if state.bars[i].low <= threshold:
                        return len(state.bars) - 1 - i
            else:
                # Touched the high = bar.high >= session_high - tolerance
                threshold_high = extreme - entry_near_extreme_pips
                for i in range(len(state.bars) - 1, -1, -1):
                    if state.bars[i].high >= threshold_high:
                        return len(state.bars) - 1 - i
            return len(state.bars)  # no touch found

        # Trend exhaustion filter (research §A.5):
        # Block mean reversion entries in trending conditions.
        # LONG only when RSI < 50 (genuine oversold); SHORT only when RSI > 50.
        if (
            price <= session_low + entry_near_extreme_pips
            and rsi < self.config.rsi_long_level
            and rsi < 50.0  # trend exhaustion gate
        ):
            # Optional multi-bar reversal confirmation (research §A.5 NEW).
            if self.config.min_bars_since_extreme_touch > 0:
                bars_since = _bars_since_extreme_touch(session_low, "low")
                if bars_since <= self.config.min_bars_since_extreme_touch:
                    logger.debug(
                        "SRMR+ %s: long blocked — only %d bars since low touch (need >%d)",
                        getattr(latest, "symbol", "?"),
                        bars_since,
                        self.config.min_bars_since_extreme_touch,
                    )
                    return None
            direction = TradeDirection.LONG
            rationale = (
                f"SRMR+ long: price={price:.5f} near range low={session_low:.5f}, "
                f"RSI={rsi:.1f}, ADX={adx:.1f}, range={session_range_width:.1f} pips"
            )
            spread_price = latest.spread_pips * pip
            return _build_signal(
                direction,
                price,
                atr,
                self.config,
                session_range_price,
                adx,
                rsi,
                rationale,
                pip,
                spread_price=spread_price,
            )

        if (
            price >= session_high - entry_near_extreme_pips
            and rsi > self.config.rsi_short_level
            and rsi > 50.0  # trend exhaustion gate
        ):
            # Optional multi-bar reversal confirmation (research §A.5 NEW).
            if self.config.min_bars_since_extreme_touch > 0:
                bars_since = _bars_since_extreme_touch(session_high, "high")
                if bars_since <= self.config.min_bars_since_extreme_touch:
                    logger.debug(
                        "SRMR+ %s: short blocked — only %d bars since high touch (need >%d)",
                        getattr(latest, "symbol", "?"),
                        bars_since,
                        self.config.min_bars_since_extreme_touch,
                    )
                    return None
            direction = TradeDirection.SHORT
            rationale = (
                f"SRMR+ short: price={price:.5f} near range high={session_high:.5f}, "
                f"RSI={rsi:.1f}, ADX={adx:.1f}, range={session_range_width:.1f} pips"
            )
            spread_price = latest.spread_pips * pip
            return _build_signal(
                direction,
                price,
                atr,
                self.config,
                session_range_price,
                adx,
                rsi,
                rationale,
                pip,
                spread_price=spread_price,
            )

        logger.debug(
            "SRMR+ %s: no signal condition met (price=%.5f session_low=%.5f session_high=%.5f rsi=%.1f)",
            getattr(latest, "symbol", "?"),
            price,
            session_low,
            session_high,
            rsi,
        )
        return None

    def apply_dxy_overlay(
        self,
        signal: StrategySignal | None,
        dxy_bars: list | None = None,
    ) -> StrategySignal | None:
        """Apply DXY regime confidence adjustment to a generated signal.

        Returns the original signal unchanged if overlay is disabled,
        no DXY bars provided, or signal is None.
        """
        if signal is None or self._dxy_overlay is None or not dxy_bars:
            return signal
        if DxyBar is None:
            return signal

        try:
            dxy_data = [
                DxyBar(
                    time_ms=int(b["time_ms"]) if isinstance(b, dict) else int(b.time_ms),
                    open=b["open"] if isinstance(b, dict) else b.open,
                    high=b["high"] if isinstance(b, dict) else b.high,
                    low=b["low"] if isinstance(b, dict) else b.low,
                    close=b["close"] if isinstance(b, dict) else b.close,
                )
                for b in dxy_bars
            ]
            dir_name = signal.direction.name if hasattr(signal.direction, "name") else str(signal.direction)
            adjusted = self._dxy_overlay.adjust_confidence(signal.confidence, dxy_data, dir_name)
            return StrategySignal(
                direction=signal.direction,
                confidence=adjusted,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit_1=signal.take_profit_1,
                take_profit_2=signal.take_profit_2,
                take_profit_3=signal.take_profit_3,
                rationale=signal.rationale + f" | DXY adj: {signal.confidence:.2f}\u2192{adjusted:.2f}",
            )
        except Exception as exc:
            logger.warning("DXY overlay application failed: %s", exc)
            return signal
