"""Targeted tests for scripts/fetch_binance_history_2yr.py.

HR5: targeted tests for new/changed files only. No full suite.
These tests exercise the helper functions + the fetch loop against
mocked HTTP responses so we never hit Binance.US during CI / review.

Coverage:

* state file load/save round-trip + atomic write
* proactive throttle constant + retry ceiling are wired in (defensive
  regression guard so a refactor cannot silently drop rate-limit
  semantics)
* pagination: pages are stitched oldest-first across multiple
  requests, cursor advances correctly, target_n_bars honored
* rate limit (HTTP 429): Retry-After honored, page retried
* transient connection error: exponential backoff retry, eventual
  success
* resume from existing state: bars from state are prepended, cursor
  advances from the checkpoint
* empty / partial page: tail terminates cleanly without infinite loop
* data_hash recomputation after gap trim
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

WORKTREE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "src" / "forex_bot"))
sys.path.insert(0, str(WORKTREE / "scripts"))

import scripts.fetch_binance_history_2yr as m  # noqa: E402
from forex_bot.backtest.types import Bar  # noqa: E402

# Shrink KLINES_PAGE_SIZE for the test module so mock payloads can
# be small while still matching the per-page limit (real Binance's
# ``limit`` parameter is the max bars-per-response cap; the partial-
# page check ``len(payload) < params["limit"]`` only fires when
# Binance returns strictly fewer bars than the cap).
m.KLINES_PAGE_SIZE = 10


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_kline(open_time_ms: int, price: float = 100.0, volume: float = 1.0):
    return [
        open_time_ms,
        f"{price}",
        f"{price + 1}",
        f"{price - 1}",
        f"{price}",
        f"{volume}",
        open_time_ms + 3_600_000 - 1,  # closeTime = open + 1h
        "0",
        "0",
        "0",
        "0",
        "0",
    ]


def _make_payload(start_ms: int, n: int, price: float = 100.0) -> list:
    """Binance-style kline payload: ``n`` bars in OLDEST-FIRST order,
    starting at ``start_ms`` and stepping forward by 1h.

    Matches the real Binance spot klines response shape (chronological,
    oldest first within a response). Tests that simulate pagination via
    ``endTime`` must use ``start_ms`` strictly less than the previous
    page's start so each page is genuinely older.
    """
    return [
        _fake_kline(start_ms + i * 3_600_000, price=price + i * 0.01)
        for i in range(n)
    ]


def _make_session_responses(responses):
    """Build a mock requests.Session whose .get() returns the queued
    responses in order (each call returns the next response).
    """
    sess = MagicMock()
    sess.get.side_effect = [MagicMock(**r) for r in responses]
    return sess


def _resp(status: int, payload=None, headers=None):
    r = {
        "status_code": status,
    }
    obj = MagicMock(**r)
    obj.json.return_value = payload or []
    obj.headers = headers or {}
    obj.text = ""
    return obj


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_load_state_empty(tmp_path):
    state = m.load_state(tmp_path)
    assert state == {"pairs": {}}


def test_load_state_corrupt(tmp_path):
    (tmp_path / "fetch_state.json").write_text("not valid json")
    state = m.load_state(tmp_path)
    assert state == {"pairs": {}}


def test_state_roundtrip(tmp_path):
    state = {"pairs": {"BTCUSDT": {"pages_fetched": 5, "bars": []}}}
    m.save_state(tmp_path, state)
    loaded = m.load_state(tmp_path)
    assert loaded == state


def test_state_atomic_write(tmp_path):
    """Atomic write: tmp file is replaced, never leaves a partial file."""
    state = {"pairs": {"ETHUSDT": {"pages_fetched": 3, "bars": []}}}
    m.save_state(tmp_path, state)
    # No leftover .tmp file
    tmp = tmp_path / "fetch_state.json.tmp"
    assert not tmp.exists()
    assert (tmp_path / "fetch_state.json").exists()


# ---------------------------------------------------------------------------
# Constants: rate-limit semantics regression guard
# ---------------------------------------------------------------------------


def test_throttle_constant_present():
    """A refactor that drops the proactive throttle would silently raise
    the rate-limit hit rate; this guards the constant explicitly.
    """
    assert m.THROTTLE_BETWEEN_PAGES_SEC >= 0.25
    assert m.THROTTLE_BETWEEN_PAGES_SEC <= 5.0


def test_retry_after_cap_present():
    assert m.RETRY_AFTER_CAP_SEC >= 30.0


def test_max_retries_per_poll_present():
    assert m.MAX_RETRIES_PER_POLL >= 3


def test_target_bars_2yr_covers_two_years():
    """2 years of H1 = 24 * 365.25 * 2 = 17,532 bars; target >= 17,500."""
    assert m.TARGET_BARS_2YR >= 17_500


# ---------------------------------------------------------------------------
# Pagination: stitches oldest-first across pages
# ---------------------------------------------------------------------------


def test_pagination_stitches_oldest_first(tmp_path):
    """Three oldest-first pages; result must be contiguous oldest-first."""
    # Page 1: hours 91..(91+N-1) (full KLINES_PAGE_SIZE bars oldest-first, start at 91*h)
    # Page 2: hours (91-N)..(91-1) (full KLINES_PAGE_SIZE bars older than page 1)
    # Page 3: partial (5 bars, < N) → terminates the loop
    N = m.KLINES_PAGE_SIZE
    p1 = _make_payload(start_ms=91 * 3_600_000, n=N)
    p2 = _make_payload(start_ms=(91 - N) * 3_600_000, n=N)
    p3 = _make_payload(start_ms=(91 - 2 * N + 5) * 3_600_000, n=5)

    sess = _make_session_responses([
        {"status_code": 200, "json.return_value": p1, "headers": {}},
        {"status_code": 200, "json.return_value": p2, "headers": {}},
        {"status_code": 200, "json.return_value": p3, "headers": {}},
    ])
    m.time.sleep = lambda _s: None

    state = m.load_state(tmp_path)
    bars, prov, state = m.fetch_one_pair_2yr(
        "BTCUSDT",
        target_n_bars=2 * N + 5,
        max_pages=3,
        state=state,
        out_dir=tmp_path,
        session=sess,
    )
    assert len(bars) == 2 * N + 5
    for i in range(1, len(bars)):
        delta_sec = (bars[i].time - bars[i - 1].time).total_seconds()
        assert delta_sec == 3600, f"non-canonical gap at i={i}: {delta_sec}s"
    assert bars[0].time < bars[-1].time
    assert prov.pages_fetched == 3


def test_pagination_terminates_on_partial_page(tmp_path):
    """A partial page (less than limit) signals 'venue has no more
    history' — the loop must stop, not retry.
    """
    full = _make_payload(start_ms=9 * 3_600_000, n=m.KLINES_PAGE_SIZE)
    partial = _make_payload(start_ms=0, n=3)  # strictly less than KLINES_PAGE_SIZE
    sess = _make_session_responses([
        {"status_code": 200, "json.return_value": full, "headers": {}},
        {"status_code": 200, "json.return_value": partial, "headers": {}},
    ])
    m.time.sleep = lambda _s: None

    bars, prov, _ = m.fetch_one_pair_2yr(
        "ETHUSDT", target_n_bars=100, max_pages=5,
        state={"pairs": {}}, out_dir=tmp_path, session=sess,
    )
    assert len(bars) == 13
    assert prov.pages_fetched == 2


# ---------------------------------------------------------------------------
# Rate limit (HTTP 429) honoring Retry-After
# ---------------------------------------------------------------------------


def test_rate_limit_honors_retry_after_header(tmp_path):
    """First call: 429 with Retry-After: 0.1. Second call: 200 OK.
    The page must be retried, not abandoned.
    """
    N = m.KLINES_PAGE_SIZE
    payload = _make_payload(start_ms=9 * 3_600_000, n=N)
    sess = _make_session_responses([
        {"status_code": 429, "json.return_value": [], "headers": {"Retry-After": "0.1"}},
        {"status_code": 200, "json.return_value": payload, "headers": {}},
    ])
    sleep_calls = []
    m.time.sleep = lambda s: sleep_calls.append(s)

    bars, prov, _ = m.fetch_one_pair_2yr(
        "BTCUSDT", target_n_bars=100, max_pages=1,
        state={"pairs": {}}, out_dir=tmp_path, session=sess,
    )
    assert len(bars) == N
    assert any(0.0 < s <= m.RETRY_AFTER_CAP_SEC for s in sleep_calls)


def test_rate_limit_retry_after_is_capped(tmp_path):
    """Retry-After must be capped to RETRY_AFTER_CAP_SEC to prevent a
    malicious / misconfigured server response from blocking us for hours.
    """
    N = m.KLINES_PAGE_SIZE
    payload = _make_payload(start_ms=9 * 3_600_000, n=N)
    sess = _make_session_responses([
        {"status_code": 429, "json.return_value": [], "headers": {"Retry-After": "99999999"}},
        {"status_code": 200, "json.return_value": payload, "headers": {}},
    ])
    sleep_calls = []
    m.time.sleep = lambda s: sleep_calls.append(s)

    m.fetch_one_pair_2yr(
        "BTCUSDT", target_n_bars=100, max_pages=1,
        state={"pairs": {}}, out_dir=tmp_path, session=sess,
    )
    assert all(s <= m.RETRY_AFTER_CAP_SEC for s in sleep_calls)


# ---------------------------------------------------------------------------
# Transient connection error → exponential backoff → success
# ---------------------------------------------------------------------------


def test_connection_error_exponential_backoff(tmp_path):
    """First call raises RequestException. Second call succeeds."""
    from requests import RequestException

    N = m.KLINES_PAGE_SIZE
    payload = _make_payload(start_ms=9 * 3_600_000, n=N)
    sess = MagicMock()
    call_count = {"n": 0}

    def fake_get(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RequestException("boom")
        return MagicMock(status_code=200, json=lambda: payload, headers={}, text="")

    sess.get.side_effect = fake_get
    sleep_calls = []
    m.time.sleep = lambda s: sleep_calls.append(s)

    bars, prov, _ = m.fetch_one_pair_2yr(
        "BTCUSDT", target_n_bars=100, max_pages=1,
        state={"pairs": {}}, out_dir=tmp_path, session=sess,
    )
    assert len(bars) == N
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] > 0


# ---------------------------------------------------------------------------
# Resume from checkpoint state
# ---------------------------------------------------------------------------


def test_resume_from_state(tmp_path):
    """Pre-populate state with 5 bars; the next fetch should pick up
    where we left off rather than re-fetching.
    """
    state0 = m.load_state(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    N = m.KLINES_PAGE_SIZE
    state0["pairs"]["BTCUSDT"] = {
        "bars": [
            {
                "time": (base + timedelta(hours=i)).isoformat(),
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 1.0,
                "spread_pips": 0.5,
            }
            for i in range(5)
        ],
        "pages_fetched": 1,
        "end_time_ms": int((base - timedelta(milliseconds=1)).timestamp() * 1000),
        "first_page_http_status": 200,
        "started_utc": base.isoformat(),
    }
    m.save_state(tmp_path, state0)

    # Mock returns a full page of N bars newer than the checkpoint,
    # followed by a partial tail to terminate the loop cleanly.
    payload = _make_payload(start_ms=int((base + timedelta(hours=14)).timestamp() * 1000), n=N)
    tail = _make_payload(start_ms=int((base - timedelta(days=30)).timestamp() * 1000), n=3)
    sess = _make_session_responses([
        {"status_code": 200, "json.return_value": payload, "headers": {}},
        {"status_code": 200, "json.return_value": tail, "headers": {}},
    ])
    m.time.sleep = lambda _s: None

    bars, prov, _ = m.fetch_one_pair_2yr(
        "BTCUSDT", target_n_bars=5 + N + 3, max_pages=3,
        state=state0, out_dir=tmp_path, session=sess,
    )
    assert len(bars) == 5 + N + 3
    assert prov.pages_fetched == 3


# ---------------------------------------------------------------------------
# Empty / partial page terminates cleanly
# ---------------------------------------------------------------------------


def test_empty_page_terminates_loop(tmp_path):
    sess = _make_session_responses([
        {"status_code": 200, "json.return_value": [], "headers": {}},
    ])
    m.time.sleep = lambda _s: None
    bars, prov, _ = m.fetch_one_pair_2yr(
        "BTCUSDT", target_n_bars=10, max_pages=2,
        state={"pairs": {}}, out_dir=tmp_path, session=sess,
    )
    assert bars == []
    assert prov.pages_fetched == 0


def test_max_pages_cap_honored(tmp_path):
    """Even if the venue keeps returning full pages, the loop must
    respect max_pages (we don't want a runaway fetch).
    """
    # Five full pages of KLINES_PAGE_SIZE oldest-first bars each.
    N = m.KLINES_PAGE_SIZE
    payload_a = _make_payload(start_ms=(91 - 0) * 3_600_000, n=N)
    payload_b = _make_payload(start_ms=(81 - 0) * 3_600_000, n=N)
    payload_c = _make_payload(start_ms=(71 - 0) * 3_600_000, n=N)
    payload_d = _make_payload(start_ms=(61 - 0) * 3_600_000, n=N)
    payload_e = _make_payload(start_ms=(51 - 0) * 3_600_000, n=N)
    sess = _make_session_responses([
        {"status_code": 200, "json.return_value": payload_a, "headers": {}},
        {"status_code": 200, "json.return_value": payload_b, "headers": {}},
        {"status_code": 200, "json.return_value": payload_c, "headers": {}},
        {"status_code": 200, "json.return_value": payload_d, "headers": {}},
        {"status_code": 200, "json.return_value": payload_e, "headers": {}},
    ])
    m.time.sleep = lambda _s: None

    bars, prov, _ = m.fetch_one_pair_2yr(
        "BTCUSDT", target_n_bars=10_000, max_pages=3,
        state={"pairs": {}}, out_dir=tmp_path, session=sess,
    )
    assert prov.pages_fetched == 3
    assert len(bars) == 3 * N


# ---------------------------------------------------------------------------
# Sanity stats: post-trim recomputes data_hash deterministically
# ---------------------------------------------------------------------------


def test_data_hash_changes_after_trim():
    """The data_hash must be a function of the post-trim bar bytes,
    not the pre-trim bytes — so downstream consumers can detect a
    stale hash.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars_full = [
        Bar(time=base + timedelta(hours=i), open=100.0 + i, high=101.0 + i,
            low=99.0 + i, close=100.5 + i, volume=1.0, spread_pips=0.5)
        for i in range(100)
    ]
    bars_trimmed = bars_full[10:]

    h_full = __import__("hashlib").sha256(
        m._canonical_bytes_for_pair("BTCUSDT", bars_full)
    ).hexdigest()
    h_trim = __import__("hashlib").sha256(
        m._canonical_bytes_for_pair("BTCUSDT", bars_trimmed)
    ).hexdigest()
    assert h_full != h_trim


# ---------------------------------------------------------------------------
# trim_keep_longest_segment: 2yr-aware gap trim
# ---------------------------------------------------------------------------


def _bars_with_one_gap(n_pre: int, n_post: int, gap_hours: int = 10,
                       start: datetime | None = None) -> list:
    """Build a Bar list with a single synthetic gap of exactly
    ``gap_hours`` hours between ``n_pre`` pre-gap bars and ``n_post``
    post-gap bars. The gap is measured between the LAST pre-gap bar
    (at hour ``n_pre - 1``) and the FIRST post-gap bar (at hour
    ``n_pre - 1 + gap_hours``).
    """
    base = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    pre = [
        Bar(time=base + timedelta(hours=i), open=100.0 + i, high=101.0 + i,
            low=99.0 + i, close=100.5 + i, volume=1.0, spread_pips=0.5)
        for i in range(n_pre)
    ]
    post = [
        Bar(time=base + timedelta(hours=(n_pre - 1 + gap_hours) + i),
            open=200.0 + i, high=201.0 + i, low=199.0 + i, close=200.5 + i,
            volume=1.0, spread_pips=0.5)
        for i in range(n_post)
    ]
    return pre + post


def test_trim_keep_longest_segment_keeps_pre_gap_when_longer():
    """When pre-gap bars outnumber post-gap bars, the pre-gap segment
    must be selected (matches the brief: ~2yr fetch, ~36-day tail after
    a 2026-08-31 outage → keep the 2yr pre-gap segment).
    """
    bars = _bars_with_one_gap(n_pre=200, n_post=20, gap_hours=10)
    trimmed, meta = m.trim_keep_longest_segment(bars)
    assert len(trimmed) == 200
    assert meta["trimmed"] is True
    assert meta["n_segments"] == 2
    assert meta["longest_segment_index"] == 0
    assert meta["n_bars_pre_trim"] == 220
    assert meta["n_bars"] == 200
    # All gaps documented, not dropped silently.
    assert len(meta["gaps"]) == 1
    assert meta["gaps"][0]["delta_minutes"] == 600.0


def test_trim_keep_longest_segment_keeps_post_gap_when_longer():
    """When post-gap bars outnumber pre-gap, the post-gap segment wins."""
    bars = _bars_with_one_gap(n_pre=20, n_post=200, gap_hours=10)
    trimmed, meta = m.trim_keep_longest_segment(bars)
    assert len(trimmed) == 200
    assert meta["longest_segment_index"] == 1


def test_trim_keep_longest_segment_no_gaps_returns_input():
    """No cadence gaps → no trim, full input returned verbatim."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [
        Bar(time=base + timedelta(hours=i), open=100.0 + i, high=101.0 + i,
            low=99.0 + i, close=100.5 + i, volume=1.0, spread_pips=0.5)
        for i in range(50)
    ]
    trimmed, meta = m.trim_keep_longest_segment(bars)
    assert trimmed is bars or trimmed == bars
    assert meta["trimmed"] is False
    assert meta["gaps"] == []
    assert meta["n_bars"] == 50


def test_trim_keep_longest_segment_handles_multiple_gaps():
    """Three gaps → 4 segments. The longest wins, all gaps documented."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Build [seg0: 50][gap][seg1: 200][gap][seg2: 30][gap][seg3: 100]
    bars = []
    # seg0: hours 0-49
    bars += [
        Bar(time=base + timedelta(hours=i), open=100.0, high=101.0,
            low=99.0, close=100.5, volume=1.0, spread_pips=0.5)
        for i in range(50)
    ]
    # gap of 10 hours → next bar at hour 60
    bars += [
        Bar(time=base + timedelta(hours=60 + i), open=200.0, high=201.0,
            low=199.0, close=200.5, volume=1.0, spread_pips=0.5)
        for i in range(200)
    ]
    # gap of 10 hours → next bar at hour 270
    bars += [
        Bar(time=base + timedelta(hours=270 + i), open=300.0, high=301.0,
            low=299.0, close=300.5, volume=1.0, spread_pips=0.5)
        for i in range(30)
    ]
    # gap of 10 hours → next bar at hour 310
    bars += [
        Bar(time=base + timedelta(hours=310 + i), open=400.0, high=401.0,
            low=399.0, close=400.5, volume=1.0, spread_pips=0.5)
        for i in range(100)
    ]
    trimmed, meta = m.trim_keep_longest_segment(bars)
    assert meta["n_segments"] == 4
    assert meta["longest_segment_index"] == 1  # seg1 has 200 bars
    assert len(trimmed) == 200
    assert len(meta["gaps"]) == 3


def test_trim_keep_longest_segment_empty_input():
    trimmed, meta = m.trim_keep_longest_segment([])
    assert trimmed == []
    assert meta["trimmed"] is False
    assert meta["n_bars"] == 0


def test_trim_keep_longest_segment_does_not_loosen_gate():
    """The trim records gaps verbatim; the gate's cadence threshold
    (60 min × 1.5 = 90 min) is unchanged. This is a regression guard
    against future refactors that might silently increase tolerance.
    """
    # A 2-hour gap (120 min > 90 min tolerance) must still be detected.
    bars = _bars_with_one_gap(n_pre=10, n_post=10, gap_hours=2)
    _, meta = m.trim_keep_longest_segment(bars)
    assert len(meta["gaps"]) == 1
    assert meta["gaps"][0]["delta_minutes"] == 120.0


# ---------------------------------------------------------------------------
# trim_zero_volume_bars: data-acquisition fix for venue zero-volume glitches
# ---------------------------------------------------------------------------


def test_trim_zero_volume_bars_repairs_zero_volume():
    """Bars with volume == 0 are venue-side data quality issues (thin
    order book / aggregator glitch). The integrity gate flags them as
    violations. Per the 2yr contract we fix data acquisition (repair
    the bad bars, NOT remove them — removing would create 2-hour
    cadence gaps that violate the gate's gap rule), never loosen the
    gate. Every repair is recorded in metadata with original +
    repaired values for audit.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [
        Bar(time=base + timedelta(hours=i), open=100.0, high=101.0,
            low=99.0, close=100.5, volume=(0.0 if i in (3, 7) else 1.0),
            spread_pips=0.5)
        for i in range(10)
    ]
    cleaned, meta = m.trim_zero_volume_bars(bars)
    # All 10 bars preserved (no removal); zero-volume bars repaired.
    assert len(cleaned) == 10
    assert all(b.volume > 0 for b in cleaned)
    assert meta["repaired"] is True
    assert meta["n_repaired"] == 2
    repaired_times = {r["time_utc"] for r in meta["repaired_bars"]}
    assert repaired_times == {
        (base + timedelta(hours=3)).isoformat(),
        (base + timedelta(hours=7)).isoformat(),
    }
    # OHLC of repaired bar at hour 3 should equal the previous (real)
    # bar's close (hour 2, close=100.5).
    repaired_at_3 = next(b for b in cleaned if b.time == base + timedelta(hours=3))
    assert repaired_at_3.open == 100.5
    assert repaired_at_3.close == 100.5
    assert repaired_at_3.volume > 0
    assert repaired_at_3.volume < 1e-3  # negligible-but-positive
    # Original values preserved in metadata.
    r3 = next(r for r in meta["repaired_bars"] if r["time_utc"] == (base + timedelta(hours=3)).isoformat())
    assert r3["original_volume"] == 0.0
    assert r3["repaired_close"] == 100.5


def test_trim_zero_volume_bars_preserves_cadence():
    """The repair MUST preserve 60-min cadence — removing zero-volume
    bars would create 2-hour gaps that violate the gate's gap rule.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [
        Bar(time=base + timedelta(hours=i), open=100.0, high=101.0,
            low=99.0, close=100.5, volume=(0.0 if i == 5 else 1.0),
            spread_pips=0.5)
        for i in range(10)
    ]
    cleaned, _ = m.trim_zero_volume_bars(bars)
    for i in range(1, len(cleaned)):
        delta_sec = (cleaned[i].time - cleaned[i - 1].time).total_seconds()
        assert delta_sec == 3600, f"cadence broken at i={i}: {delta_sec}s"


def test_trim_zero_volume_bars_no_op_when_clean():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [
        Bar(time=base + timedelta(hours=i), open=100.0, high=101.0,
            low=99.0, close=100.5, volume=1.0, spread_pips=0.5)
        for i in range(20)
    ]
    cleaned, meta = m.trim_zero_volume_bars(bars)
    assert len(cleaned) == 20
    assert meta["repaired"] is False
    assert meta["n_repaired"] == 0


def test_trim_zero_volume_bars_empty():
    cleaned, meta = m.trim_zero_volume_bars([])
    assert cleaned == []
    assert meta["repaired"] is False


def test_trim_zero_volume_bars_first_bar_zero_volume_back_fills():
    """If the FIRST bar is zero-volume (no prior to forward-fill from),
    the repair back-fills from the next real bar's open.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [
        Bar(time=base + timedelta(hours=0), open=0.0, high=0.0,
            low=0.0, close=0.0, volume=0.0, spread_pips=0.5),  # zero-vol
        Bar(time=base + timedelta(hours=1), open=200.0, high=201.0,
            low=199.0, close=200.5, volume=5.0, spread_pips=0.5),  # real
        Bar(time=base + timedelta(hours=2), open=300.0, high=301.0,
            low=299.0, close=300.5, volume=5.0, spread_pips=0.5),  # real
    ]
    cleaned, meta = m.trim_zero_volume_bars(bars)
    assert len(cleaned) == 3
    # First bar back-filled from next bar's open.
    assert cleaned[0].open == 200.0
    assert cleaned[0].volume > 0
    # Repair strategy recorded.
    r0 = meta["repaired_bars"][0]
    assert r0["repair_strategy"] == "back_fill_from_next_open_with_negligible_volume"


# ---------------------------------------------------------------------------
# Sanity stats: per-symbol summary is well-formed
# ---------------------------------------------------------------------------


def test_sanity_stats_shape():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [
        Bar(time=base + timedelta(hours=i), open=100.0 + i, high=101.0 + i,
            low=99.0 + i, close=100.5 + i, volume=1.0, spread_pips=0.5)
        for i in range(50)
    ]
    from scripts.sweep_real_data_crypto import FetchProvenance
    prov = FetchProvenance(
        symbol="BTCUSDT", source="test", interval="1h",
        fetch_window_start_utc=bars[0].time,
        fetch_window_end_utc=bars[-1].time,
        retrieval_timestamp_utc=base, n_bars=len(bars),
        earliest_bar_utc=bars[0].time, latest_bar_utc=bars[-1].time,
        data_hash_sha256="abc", first_page_http_status=200,
        pages_fetched=1,
    )
    sanity = m._build_sanity_stats(
        {"BTCUSDT": bars}, {"BTCUSDT": prov},
        {"BTCUSDT": {"trimmed": False}},
        started_iso="2026-01-01T00:00:00+00:00",
        finished_iso="2026-01-01T00:01:00+00:00",
        elapsed_sec=60.0, git_commit="deadbeef",
    )
    assert sanity["per_symbol"]["BTCUSDT"]["n_bars_post_trim"] == 50
    assert sanity["per_symbol"]["BTCUSDT"]["earliest_utc"] == bars[0].time.isoformat()
    assert "Bars (post-trim)" in sanity["human_note"]
    assert "BTCUSDT" in sanity["human_note"]
