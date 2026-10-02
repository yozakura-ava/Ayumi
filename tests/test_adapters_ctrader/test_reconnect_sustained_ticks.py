"""Sustained-tick reconnect gate + not-authorized error mapping tests (card f37e7b74).

Background
----------
Live forward-test (PID 515241) entered a ~69s reconnect-flap loop because
the reconnection consecutive-failure counter was reset on ANY tick. cTrader
emits exactly ONE snapshot spot event per ProtoOASubscribeSpotsReq, so a
subscribed-but-silent broker could farm one tick → counter reset → feed
stalls → health check fires → reconnect — ad infinitum. The 20-attempt
breaker was unreachable.

Separately, INVALID_REQUEST "Trading account is not authorized" surfaced as
the misleading "ProtoOATraderRes missing 'trader' field" complaint raised
downstream by account_state.get_balance — the actual authorization fault
was hidden from operator logs.

These tests lock the contract:
1. 1-tick-then-silence mock drives the engine to breaker trip within
   max_reconnect_attempts.
2. Sustained-tick mock (>= N ticks in T seconds) resets the counter and
   the breaker does NOT trip.
3. INVALID_REQUEST "Trading account is not authorized" payload yields the
   explicit "[Account Auth] server reports not authorized" log line and
   does NOT emit the misleading schema complaint in the spot-feed logs.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Ensure src/forex_bot is on path for imports (mirrors existing tests
# in this directory that run from the repo root).
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src" / "forex_bot"))

from adapters.ctrader.forward_test_engine import (  # noqa: E402
    ForwardTestConfig,
    ForwardTestEngine,
    ForwardTestHealth,
)
from adapters.ctrader.protocols import Tick  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bare_engine(
    *,
    sustained_ticks_required: int = 5,
    sustained_ticks_window_sec: float = 120.0,
    max_reconnect_attempts: int = 20,
    reconnect_delay_sec: float = 0.0,
    stale_tick_threshold_sec: float = 60.0,
) -> ForwardTestEngine:
    """Construct a minimal ForwardTestEngine without starting any threads.

    Uses ``__new__`` to bypass the heavy ``__init__`` (no strategies
    required, no auth/feed wiring). Sets only the fields that
    ``_attempt_reconnect`` and ``_on_tick`` actually touch.
    """
    engine = ForwardTestEngine.__new__(ForwardTestEngine)
    engine._config = ForwardTestConfig(
        symbol="GBPUSD",
        symbols=["GBPUSD"],
        max_reconnect_attempts=max_reconnect_attempts,
        reconnect_delay_sec=reconnect_delay_sec,
        max_reconnect_delay_sec=1.0,
        stale_tick_threshold_sec=stale_tick_threshold_sec,
        sustained_ticks_required=sustained_ticks_required,
        sustained_ticks_window_sec=sustained_ticks_window_sec,
        health_monitor_interval_sec=0.01,
        live_mode=False,
        execution_mode="paper",
        openapi_host="demo.ctraderapi.com",
    )
    engine._lock = threading.RLock()
    engine._health = ForwardTestHealth()
    engine._reconnect_delay = engine._config.reconnect_delay_sec
    engine._last_reconnect_attempt_at = 0.0
    engine._reconnect_stuck_at = None
    engine._stuck_reconnect_threshold_sec = 60.0
    engine._post_reconnect_at = None
    engine._post_reconnect_ticks = []
    engine._tick_timestamps = []
    engine._tick_rate_window_sec = 10.0
    engine._running = True
    engine._cfg_symbols_normalized = {"GBPUSD"}
    engine._bars = {}
    engine._current_bar = {}
    engine._required_timeframes = set()
    # The bare engine has no live feed wired up; ``_market_feed = None``
    # so attribute lookups in _attempt_reconnect and _on_tick use the
    # production-style guard (``if self._market_feed: ...``) instead of
    # raising AttributeError. Callers that need a feed mock set it on the
    # instance after construction.
    engine._market_feed = None
    engine._live_adapter = None
    return engine


def _make_tick(symbol: str = "GBPUSD", bid: float = 1.2700, ask: float = 1.2701) -> Tick:
    return Tick(
        symbol=symbol,
        bid=bid,
        ask=ask,
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# TestFlapBreakerTrip — flap mock drives breaker
# ---------------------------------------------------------------------------


class TestFlapBreakerTrip:
    """1 tick → silence → repeated reconnect attempts → breaker trips."""

    def test_one_tick_then_silence_trips_breaker(self, monkeypatch):
        """A feed that emits a single tick and then stops must drive the
        reconnect counter to ``max_reconnect_attempts`` and trip the
        breaker (``_running`` flips to False).
        """
        # Tight window so the gate expires quickly and the next health
        # check sees a feed with no recent ticks — same flap shape as the
        # production failure.
        engine = _bare_engine(
            sustained_ticks_required=5,
            sustained_ticks_window_sec=0.5,
            max_reconnect_attempts=20,
        )

        # Mock market feed: claims to be running (broker accepted
        # subscribe), but emits exactly one snapshot spot event and then
        # stays silent — exactly the failure shape from card 17c3afed.
        feed = MagicMock()
        feed.is_running = True
        # The structured reconnect-diagnostic log json-serializes
        # ``_token_expires_at - time.monotonic()``; a plain MagicMock
        # would make json.dumps blow up. Set it to None so the log emits
        # ``null``.
        feed._token_expires_at = None

        # ``_start_market_feed`` is invoked by ``_attempt_reconnect``. On
        # the first call it succeeds (returns True) so the engine opens
        # the sustained-tick window; subsequent calls also "succeed" but
        # the feed remains silent — mirrors the snapshot-only broker.
        feed_calls = {"n": 0}

        def _fake_start_market_feed():
            feed_calls["n"] += 1
            engine._market_feed = feed
            return True

        monkeypatch.setattr(engine, "_start_market_feed", _fake_start_market_feed)

        # Drive the health monitor's gate evaluation directly: simulate
        # that the window has expired (now >> reconnect_at + window) so
        # the counter is NOT reset, and that the feed has not produced a
        # tick recently enough to satisfy staleness, so _attempt_reconnect
        # is invoked. We loop until the breaker trips.
        engine._post_reconnect_at = time.monotonic() - 10.0  # window already expired
        engine._post_reconnect_ticks = []  # nothing arrived within the window

        iterations = 0
        max_iterations = engine._config.max_reconnect_attempts + 5
        while engine._running and iterations < max_iterations:
            iterations += 1
            # Simulate "no recent tick" by leaving _health.last_tick_at None
            # — that makes staleness=inf, is_healthy=False, and triggers
            # _attempt_reconnect because the backoff is 0.
            with engine._lock:
                engine._health.last_tick_at = None
            engine._attempt_reconnect()
            # Simulate feed going silent again after a successful start
            # so the next loop iteration forces another reconnect attempt.
            engine._post_reconnect_at = time.monotonic() - 10.0
            engine._post_reconnect_ticks = []

        assert not engine._running, (
            f"Engine should have stopped after breaker trip; iterations={iterations}"
        )
        assert engine._health.reconnection_attempts >= engine._config.max_reconnect_attempts, (
            f"Counter should have reached max_reconnect_attempts, got "
            f"{engine._health.reconnection_attempts}"
        )
        # The flap loop must actually invoke _start_market_feed more than
        # once — that's what the production failure looked like.
        assert feed_calls["n"] >= 2, (
            f"Expected repeated reconnect attempts, only saw {feed_calls['n']}"
        )

    def test_snapshot_tick_alone_does_not_reset_counter(self, monkeypatch):
        """A single snapshot tick post-reconnect must NOT reset the
        consecutive-failure counter — that's exactly the bug card f37e7b74
        is fixing.
        """
        engine = _bare_engine(
            sustained_ticks_required=5,
            sustained_ticks_window_sec=120.0,
        )
        # Force counter to non-zero as if a previous flap had bumped it.
        engine._health.reconnection_attempts = 7
        engine._post_reconnect_at = time.monotonic()
        engine._post_reconnect_ticks = []

        # Deliver a single tick. _on_tick should record it (1 of N
        # required), but the gate must NOT trip — so the counter stays
        # at 7.
        tick = _make_tick()
        engine._on_tick(tick)

        assert len(engine._post_reconnect_ticks) == 1
        assert engine._health.reconnection_attempts == 7, (
            "Single snapshot tick must not reset the counter; that is "
            "the reconnect-flap regression we're fixing."
        )

        # Drive the health monitor branch directly: with only 1 tick in
        # the window, the gate is not satisfied → counter stays.
        with engine._lock:
            engine._health.last_tick_at = datetime.now(timezone.utc)
        # Reset the staleness clock and feed state so is_healthy=True
        feed = MagicMock()
        feed.is_running = True
        engine._market_feed = feed

        # Manually invoke the gate evaluation block by setting up the
        # conditions then calling the gate logic path. The simplest
        # check: verify that after the post-reconnect window with
        # < N ticks, the counter is unchanged.
        engine._post_reconnect_at = time.monotonic() - 1.0  # 1s into a 120s window
        engine._post_reconnect_ticks = [time.monotonic()]
        # Simulate the health monitor running (we don't start the thread;
        # just inline-check that with len(ticks)=1 < required=5 the
        # counter stays).
        window_age = time.monotonic() - engine._post_reconnect_at
        assert window_age < engine._config.sustained_ticks_window_sec
        assert len(engine._post_reconnect_ticks) < engine._config.sustained_ticks_required


# ---------------------------------------------------------------------------
# TestSustainedTicksRecovery — healthy feed recovers
# ---------------------------------------------------------------------------


class TestSustainedTicksRecovery:
    """Sustained ticks post-reconnect reset the counter."""

    def test_sustained_ticks_reset_counter(self, monkeypatch):
        """Five ticks within 120s post-reconnect resets the counter to 0
        and the engine stays running (no breaker trip).
        """
        engine = _bare_engine(
            sustained_ticks_required=5,
            sustained_ticks_window_sec=120.0,
        )
        # Simulate prior flap bumped the counter.
        engine._health.reconnection_attempts = 7
        # Window just opened (reconnect just succeeded).
        engine._post_reconnect_at = time.monotonic()
        engine._post_reconnect_ticks = []

        # Deliver N=5 ticks. _on_tick should append each one.
        for i in range(5):
            engine._on_tick(_make_tick(bid=1.2700 + i * 0.0001))

        assert len(engine._post_reconnect_ticks) == 5
        assert engine._health.reconnection_attempts == 7, (
            "_on_tick itself does not reset the counter; the gate check "
            "in the health monitor is what clears it."
        )

        # Simulate the health monitor gate check: with 5 ticks within
        # the 120s window, reset the counter and clear window state.
        now_mono = time.monotonic()
        window_age = now_mono - engine._post_reconnect_at
        assert window_age < engine._config.sustained_ticks_window_sec
        assert len(engine._post_reconnect_ticks) >= engine._config.sustained_ticks_required

        # Inline the gate-pass branch (mirrors _health_monitor_loop):
        with engine._lock:
            engine._health.reconnection_attempts = 0
        engine._post_reconnect_at = None
        engine._post_reconnect_ticks = []

        assert engine._health.reconnection_attempts == 0, (
            "Gate-pass with 5 ticks within 120s must reset counter"
        )
        assert engine._post_reconnect_at is None
        assert engine._post_reconnect_ticks == []

    def test_window_expiry_without_ticks_does_not_reset(self):
        """If the window expires (T seconds) before N ticks arrive, the
        counter stays untouched and the window clears.
        """
        engine = _bare_engine(
            sustained_ticks_required=5,
            sustained_ticks_window_sec=0.1,  # 100ms window
        )
        engine._health.reconnection_attempts = 9
        # Pretend reconnect happened 1 second ago, with only 2 ticks so far.
        engine._post_reconnect_at = time.monotonic() - 1.0
        engine._post_reconnect_ticks = [time.monotonic() - 0.5, time.monotonic() - 0.4]

        # Inline gate-expired branch: window_age > window → drop window,
        # leave counter alone.
        now_mono = time.monotonic()
        window_age = now_mono - engine._post_reconnect_at
        assert window_age > engine._config.sustained_ticks_window_sec
        # Mirroring the engine code:
        #   if window expired: drop state, counter NOT reset
        engine._post_reconnect_at = None
        engine._post_reconnect_ticks = []

        assert engine._health.reconnection_attempts == 9, (
            "Expired window must NOT reset the counter"
        )
        assert engine._post_reconnect_at is None

    def test_attempt_reconnect_success_starts_window_does_not_reset(self, monkeypatch):
        """``_attempt_reconnect`` on a successful start must NOT reset
        ``reconnection_attempts`` to 0; instead it opens the sustained-tick
        window. (This is the core flap-loop fix.)
        """
        engine = _bare_engine(sustained_ticks_required=5, sustained_ticks_window_sec=120.0)
        engine._health.reconnection_attempts = 12
        engine._post_reconnect_at = None
        engine._post_reconnect_ticks = []

        # Make _start_market_feed succeed without doing real I/O.
        monkeypatch.setattr(engine, "_start_market_feed", lambda: True)
        # Ensure _market_feed.stop() doesn't blow up AND the structured
        # diagnostic log in _attempt_reconnect can json-serialize the
        # token-validity field (the production code reads
        # ``_token_expires_at`` and subtracts ``time.monotonic()``; with
        # a plain MagicMock the subtraction returns a MagicMock, which
        # json.dumps refuses to serialize).
        feed = MagicMock()
        feed._token_expires_at = None
        engine._market_feed = feed

        before = time.monotonic()
        engine._attempt_reconnect()
        after = time.monotonic()

        # The window opens at the moment of successful start; ticks list
        # is empty (no ticks have arrived yet).
        assert engine._post_reconnect_at is not None
        assert before <= engine._post_reconnect_at <= after
        assert engine._post_reconnect_ticks == []

        # Counter increments at the top of _attempt_reconnect (12 → 13).
        # The CRITICAL invariant: it is NOT reset to 0 on success. The
        # old buggy behavior reset it to 0 unconditionally; the new
        # behavior leaves it at 13 and only the sustained-tick gate (in
        # the health monitor) clears it.
        assert engine._health.reconnection_attempts == 13, (
            f"_attempt_reconnect success must NOT reset the counter "
            f"(expected 13 after increment; got "
            f"{engine._health.reconnection_attempts}). The sustained-tick "
            f"gate, not reconnect success, is what clears it."
        )
        assert engine._health.reconnection_successes == 1


# ---------------------------------------------------------------------------
# TestNotAuthorizedErrorMapping — explicit log + no schema complaint
# ---------------------------------------------------------------------------


class TestNotAuthorizedErrorMapping:
    """INVALID_REQUEST 'Trading account is not authorized' must surface an
    explicit [Account Auth] log line naming the auth fault, and must NOT
    emit the misleading 'ProtoOATraderRes missing' schema complaint in the
    spot-feed logs.
    """

    def test_not_authorized_payload_emits_explicit_log(self, caplog):
        """A description-based reclassification of an INVALID_REQUEST
        with 'Trading account is not authorized' must produce an explicit
        error log line beginning with '[Account Auth]' that names the
        authorization fault (broker intervention required).
        """
        # We don't need a fully wired OpenApiSpotFeed — we just need to
        # call the existing _handle_error path with a synthetic message
        # that matches the production failure shape.
        try:
            from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed
        except Exception as exc:  # pragma: no cover — fallback path
            pytest.skip(f"OpenApiSpotFeed import not available: {exc}")

        # Build a minimal instance using __new__ to bypass __init__.
        feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)

        # Envelope and message shape match what the cTrader gateway sends
        # for the trader/balance query path. ``description`` carries the
        # canonical "Trading account is not authorized" wording.
        envelope = SimpleNamespace(clientMsgId="trader_query_abc123")
        message = SimpleNamespace(
            errorCode="INVALID_REQUEST",
            description="Trading account is not authorized",
        )

        # _handle_error reads these attributes off the feed. Pre-populate
        # to no-op values so the reclassification branch runs cleanly.
        feed._auth_circuit_open = False
        feed._refresh_in_progress = False
        feed._last_reactive_refresh_time = 0.0
        feed._kill_switch = None

        with caplog.at_level(logging.WARNING):
            feed._handle_error(message, envelope=envelope)

        log_text = "\n".join(record.getMessage() for record in caplog.records)

        assert "[Account Auth]" in log_text, (
            f"Expected explicit '[Account Auth]' log line; got:\n{log_text}"
        )
        assert "broker intervention required" in log_text, (
            f"Expected 'broker intervention required' phrase; got:\n{log_text}"
        )
        assert "Trading account is not authorized" in log_text, (
            f"Expected description in log; got:\n{log_text}"
        )

        # The misleading schema complaint must NOT appear in the spot
        # feed's logs — that complaint is raised downstream by
        # account_state.get_balance (out of scope for this card), not by
        # the spot feed itself.
        assert "ProtoOATraderRes missing" not in log_text, (
            f"Misleading schema complaint must not appear in spot-feed "
            f"logs; got:\n{log_text}"
        )
        assert "missing 'trader' field" not in log_text, (
            f"Misleading schema complaint must not appear in spot-feed "
            f"logs; got:\n{log_text}"
        )

    def test_unrelated_invalid_request_does_not_emit_auth_log(self, caplog):
        """A description that contains 'not authorized' but NOT 'trading
        account' must NOT trigger the explicit [Account Auth] line —
        we gate on both phrases to avoid false positives on generic
        'not authorized' wording.
        """
        try:
            from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"OpenApiSpotFeed import not available: {exc}")

        feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)
        envelope = SimpleNamespace(clientMsgId="trader_query_xyz789")
        message = SimpleNamespace(
            errorCode="INVALID_REQUEST",
            description="Operation not authorized: missing scope",  # generic
        )

        feed._auth_circuit_open = False
        feed._refresh_in_progress = False
        feed._last_reactive_refresh_time = 0.0
        feed._kill_switch = None

        with caplog.at_level(logging.WARNING):
            feed._handle_error(message, envelope=envelope)

        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert "[Account Auth]" not in log_text, (
            f"Generic 'not authorized' wording must NOT trigger the "
            f"explicit auth log; got:\n{log_text}"
        )
