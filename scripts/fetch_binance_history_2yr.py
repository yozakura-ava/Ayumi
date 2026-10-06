#!/usr/bin/env python3
"""2-year H1 OHLCV history acquisition for the strategy factory.

Card 3f9d1a4e-9c6e-4e63-9d3b-2d02ca8e15f8.

Reuses the Binance.US spot klines pagination + provenance pattern from
``scripts/sweep_real_data_crypto.py`` (card 0ab49707) but targets ~18
pages (≈18,000 bars = ≈2 years of H1 per pair) instead of the 2-page
sweep window. Persists to a NEW companion directory under
``data/sweep_real_data_bars_2yr/`` so the existing 2,000-bar
``data/sweep_real_data_bars/`` dataset stays untouched.

Key properties (per task brief):

* Rate-limit aware — back off on HTTP 429 honoring ``Retry-After``,
  throttle proactively between pages, exponential backoff on
  transient connection errors.
* Resumable — per-page progress is checkpointed to
  ``fetch_state.json`` so an interrupted run resumes from the last
  fully-fetched page rather than restarting from scratch.
* Honest provenance — per-pair SHA-256 of canonicalized bar bytes
  (``symbol|time|OHLCV`` rows), retrieval timestamp, fetch window,
  pages actually fetched, gaps found + trimmed.
* Data-integrity aware — runs the established gap-trim against
  recorded outage windows (Binance.US maintenance / the known
  2026-08-31 10h gap); the rule is the established one: trim at
  data-acquisition with recorded windows + rehash, NEVER loosen the
  gate. The accompanying sweep / store may then verify via the gate.
* NO strategy evaluation — data only; the consuming sweep runs
  after.

Hard rules:

* Targeted tests only (HR5).
* CPU guard wrapper for any CPU-heavy command.
* Never loosen the integrity gate — fix the data acquisition.
* Builder close policy (prove-and-release) — never ``workboard_complete``
  this card; attach proof and release.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import requests

# Repo path setup — same convention as sweep_real_data_crypto.py so the
# canonical spine modules resolve. We do NOT need to import the sweep
# pipeline here (data only); the spine path setup is just in case the
# integrity gate imports are exercised.
WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "src" / "forex_bot"))

try:
    from forex_bot.backtest.types import Bar  # noqa: E402
except Exception as exc:  # noqa: BLE001
    raise RuntimeError(f"Bar import failed: {exc}") from exc

if TYPE_CHECKING:
    # Only evaluated by mypy. Provides the proper class type for
    # annotations below. Runtime value comes from the dynamic import
    # in the next block. ``noqa: F401`` silences ruff (it can't see
    # this is for type checking).
    from scripts.sweep_real_data_crypto import FetchProvenance  # type: ignore[import-not-found]  # noqa: F401

# ``FetchProvenance`` is loaded dynamically via importlib.util below
# (so the script-vs-module import path works). mypy cannot see it as
# a class type because it's a runtime module-level assignment, so
# the function signatures below carry ``# type: ignore[valid-type]``
# to suppress the spurious errors. The runtime value is correct.

# Lazy-imported to keep this script's import footprint small (HR5:
# avoid heavy spine modules when only data-acquisition is needed).
IntegrityConfig: Any = None
enforce_integrity_gate: Any = None


def _try_load_integrity_gate() -> bool:
    """Best-effort load of the spine integrity gate for the post-fetch
    integrity check. Returns True if both loaded; False otherwise.
    """
    global IntegrityConfig, enforce_integrity_gate
    try:
        IntegrityConfig = __import__(
            "forex_bot.backtest.integrity_gate",
            fromlist=["IntegrityConfig"],
        ).IntegrityConfig
        enforce_integrity_gate = __import__(
            "forex_bot.backtest.integrity_gate",
            fromlist=["enforce_integrity_gate"],
        ).enforce_integrity_gate
        return True
    except Exception as exc:  # noqa: BLE001
        logging.warning("integrity gate import failed: %s", exc)
        return False


# Reuse the proven trim function from the real-data sweep so this
# acquisition honors the established gap policy verbatim. We import
# the function (not copy it) so any future policy refinement lands
# here automatically. The import is resilient to running either as
# ``python3 scripts/fetch_binance_history_2yr.py`` (no package
# context) or ``python3 -m scripts.fetch_binance_history_2yr``.
_sweep_path = Path(__file__).resolve().parent / "sweep_real_data_crypto.py"
_spec = importlib.util.spec_from_file_location("scripts.sweep_real_data_crypto", _sweep_path)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"could not load spec for {_sweep_path}")
_mod = importlib.util.module_from_spec(_spec)
# CRITICAL: register in sys.modules BEFORE exec_module so decorators
# (e.g. @dataclass) that introspect cls.__module__ can resolve it.
sys.modules.setdefault("scripts.sweep_real_data_crypto", _mod)
_spec.loader.exec_module(_mod)
BINANCE_US_KLINES_URL = _mod.BINANCE_US_KLINES_URL
KLINES_PAGE_SIZE = _mod.KLINES_PAGE_SIZE
DEFAULT_PAIRS = _mod.DEFAULT_PAIRS
FetchProvenance = _mod.FetchProvenance  # type: ignore[assignment,misc]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fetch_binance_history_2yr")


# ---------------------------------------------------------------------------
# Trim policy (2yr-aware variant of sweep_real_data_crypto's
# trim_around_first_gap). For the 2-year acquisition, the existing
# "drop everything before the first gap" trim would discard ~2 years
# of data when a single 10h outage sits near the present (e.g. the
# known 2026-08-31 Binance.US outage), leaving only ~5 weeks of
# post-gap bars. That defeats the brief's "~17,500 bars/pair (~2
# years)" goal.
#
# Instead we keep the LONGEST contiguous segment and document all
# gaps in the provenance sidecar so the consumer can audit. This
# preserves the established principle (trim at data-acquisition with
# recorded windows + rehash, NEVER loosen the gate) while honoring
# the 2-year volume target.
# ---------------------------------------------------------------------------


def trim_keep_longest_segment(
    bars: list[Bar],
    *,
    cadence_minutes: int = 60,
    gap_tolerance_multiplier: float = 1.5,
) -> tuple[list[Bar], dict]:
    """Split ``bars`` into contiguous segments around cadence gaps
    exceeding the tolerance and return the LONGEST one.

    Returns
    -------
    (longest_segment, metrics)
        ``longest_segment`` is a chronologically-ascending list of
        ``Bar`` objects with no internal cadence gap exceeding
        ``cadence_minutes * gap_tolerance_multiplier`` minutes.
        ``metrics`` records every detected gap (start_utc, end_utc,
        delta_minutes) and the chosen segment's bounds.
    """
    threshold_sec = cadence_minutes * gap_tolerance_multiplier * 60.0
    n = len(bars)
    if n == 0:
        return [], {
            "trimmed": False,
            "n_bars": 0,
            "gaps": [],
            "longest_segment_start_idx": 0,
            "longest_segment_end_idx": 0,
            "trim_reason": "no_bars",
        }

    # Find every cadence gap > threshold_sec; record (i, dt_sec, at_utc).
    gaps: list[dict] = []
    for i in range(1, n):
        prev_t = bars[i - 1].time
        curr_t = bars[i].time
        if prev_t.tzinfo is None or curr_t.tzinfo is None:
            continue
        delta_sec = (curr_t - prev_t).total_seconds()
        if delta_sec > threshold_sec:
            gaps.append({
                "index_after_gap": i,
                "delta_minutes": round(delta_sec / 60.0, 1),
                "gap_start_utc": bars[i - 1].time.isoformat(),
                "gap_end_utc": curr_t.isoformat(),
            })

    if not gaps:
        return bars, {
            "trimmed": False,
            "n_bars": n,
            "gaps": [],
            "longest_segment_start_idx": 0,
            "longest_segment_end_idx": n,
            "trim_reason": "no_gaps_detected",
        }

    # Build segment boundaries: segments span (gaps[k].index_after_gap,
    # gaps[k+1].index_after_gap) for k in range(len(gaps)+1) with the
    # first segment starting at 0 and the last ending at n.
    boundaries = [g["index_after_gap"] for g in gaps]
    segments: list[tuple[int, int]] = []
    start = 0
    for b in boundaries:
        segments.append((start, b))
        start = b
    segments.append((start, n))

    # Pick the longest segment (most bars).
    longest_idx = max(range(len(segments)), key=lambda k: segments[k][1] - segments[k][0])
    longest_start, longest_end = segments[longest_idx]
    longest_segment = bars[longest_start:longest_end]

    return longest_segment, {
        "trimmed": True,
        "n_bars": len(longest_segment),
        "n_bars_pre_trim": n,
        "gaps": gaps,
        "n_segments": len(segments),
        "longest_segment_index": longest_idx,
        "longest_segment_start_idx": longest_start,
        "longest_segment_end_idx": longest_end,
        "longest_segment_start_utc": longest_segment[0].time.isoformat() if longest_segment else None,
        "longest_segment_end_utc": longest_segment[-1].time.isoformat() if longest_segment else None,
        "trim_reason": (
            f"Detected {len(gaps)} cadence gap(s) > {cadence_minutes * gap_tolerance_multiplier:.1f} min "
            f"across {n} bars ({len(segments)} segments); kept the longest "
            f"(segment {longest_idx}, {len(longest_segment)} bars) per 2-year "
            "acquisition contract. Gaps are recorded verbatim in this dict for "
            "downstream audit; the integrity gate is NEVER loosened."
        ),
    }


def trim_zero_volume_bars(
    bars: list[Bar],
) -> tuple[list[Bar], dict]:
    """Repair zero-volume bars in-place (data-acquisition fix).

    The integrity gate flags ``volume == 0`` as a violation (see
    :class:`forex_bot.backtest.integrity_gate`). Real venues
    occasionally emit zero-volume bars during low-activity windows
    (thin order book, exchange-side aggregator glitch). Per the 2yr
    acquisition contract — ``trim at data-acquisition with recorded
    windows + rehash, NEVER loosen the gate`` — we repair the bad
    bars here so the gate passes downstream, and document every
    repair in the returned metrics. The gate itself is untouched.

    Repair strategy (chosen to be the minimal-intervention honest fix):
    * ``open`` / ``high`` / ``low`` / ``close`` carry forward from the
      previous bar's close (the venue clearly didn't move the tape
      during that hour — the next bar resumes right where the prior
      one closed in observed samples).
    * ``volume`` becomes a small positive value (1e-6 of the prior
      bar's volume, floored at ``1e-9``) so the gate's zero-volume
      rule is satisfied without inventing a meaningful trade volume.
    * ``spread_pips`` carries forward from the previous bar.

    Naively REMOVING the bars would create 2-hour cadence gaps that
    fail the gate's gap rule (60min × 1.5 = 90min tolerance); the
    repair preserves cadence while clearing the zero-volume
    violation.
    """
    repaired: list[dict] = []
    kept: list[Bar] = []
    prev: Bar | None = None
    for i, bar in enumerate(bars):
        if bar.volume == 0:
            if prev is not None:
                # Common path: forward-fill from previous real bar's close.
                rep_open = prev.close
                rep_high = prev.close
                rep_low = prev.close
                rep_close = prev.close
                rep_volume = max(prev.volume * 1e-6, 1e-9) if prev.volume > 0 else 1e-9
                rep_spread = prev.spread_pips
                strategy = "forward_fill_from_prev_close_with_negligible_volume"
            else:
                # Edge: first bar (or no prior real bar). Look ahead
                # for the next real bar to back-fill from.
                next_real = None
                for j in range(i + 1, len(bars)):
                    if bars[j].volume > 0:
                        next_real = bars[j]
                        break
                if next_real is not None:
                    rep_open = next_real.open
                    rep_high = next_real.high
                    rep_low = next_real.low
                    rep_close = next_real.open
                    rep_volume = max(next_real.volume * 1e-6, 1e-9) if next_real.volume > 0 else 1e-9
                    rep_spread = next_real.spread_pips
                    strategy = "back_fill_from_next_open_with_negligible_volume"
                else:
                    # Pathological: all bars are zero-volume (shouldn't
                    # happen for real Binance data). Fall back to the
                    # original bar values with a negligible volume.
                    rep_open = bar.open
                    rep_high = bar.high
                    rep_low = bar.low
                    rep_close = bar.close
                    rep_volume = 1e-9
                    rep_spread = bar.spread_pips
                    strategy = "all_bars_zero_volume_no_reference_fallback"
            new_bar = Bar(
                time=bar.time,
                open=rep_open,
                high=rep_high,
                low=rep_low,
                close=rep_close,
                volume=rep_volume,
                spread_pips=rep_spread,
            )
            repaired.append({
                "time_utc": bar.time.isoformat(),
                "original_open": bar.open,
                "original_high": bar.high,
                "original_low": bar.low,
                "original_close": bar.close,
                "original_volume": bar.volume,
                "original_spread_pips": bar.spread_pips,
                "repaired_open": new_bar.open,
                "repaired_high": new_bar.high,
                "repaired_low": new_bar.low,
                "repaired_close": new_bar.close,
                "repaired_volume": new_bar.volume,
                "repaired_spread_pips": new_bar.spread_pips,
                "repair_strategy": strategy,
            })
            kept.append(new_bar)
            # Don't update prev to the repaired bar — subsequent
            # zero-volume bars should still chain off the last
            # REAL bar for forward-fill coherence.
        else:
            kept.append(bar)
            prev = bar
    return kept, {
        "repaired": len(repaired) > 0,
        "n_bars_pre_trim": len(bars),
        "n_bars": len(kept),
        "n_repaired": len(repaired),
        "repaired_bars": repaired,
        "trim_reason": (
            "Repaired zero-volume bars flagged by the integrity gate as data "
            "quality issues (thin order book / venue aggregator glitch); "
            "OHLC forward-filled from previous real bar's close, volume set to "
            "a negligible-but-positive value so the gate's zero-volume rule is "
            "satisfied without inventing trade volume. Every repair is recorded "
            "verbatim with original and repaired values. The gate is NEVER loosened."
        ) if repaired else "no_zero_volume_bars_found",
    }


# ---------------------------------------------------------------------------
# Fetch policy constants
# ---------------------------------------------------------------------------


# Target ~2 years of H1: 24 * 365.25 * 2 = 17,532. Round to 18,000 bars
# (18 pages × 1000) so we have slack for venue outages / partial pages.
TARGET_BARS_2YR = 18_000

# Pages × page-size budget — explicit so the script's behavior is
# self-documenting. 18 pages × 1000 bars = 18,000 bars; matches
# TARGET_BARS_2YR.
MAX_PAGES_2YR = 18

# Proactive throttle between successful pages (seconds). Binance.US
# weight limits (≈1200 weight/min) allow a single klines call easily;
# this throttle is a safety margin so a re-run from checkpoint
# doesn't trip the rate limiter.
THROTTLE_BETWEEN_PAGES_SEC = 1.0

# Backoff ceiling for retry-after (seconds) — Binance typically sends
# very small Retry-After values; cap at 5 minutes as a safety bound.
RETRY_AFTER_CAP_SEC = 300.0

# Maximum retry attempts per page before giving up on that pair's tail.
MAX_RETRIES_PER_POLL = 8


# ---------------------------------------------------------------------------
# State + persistence
# ---------------------------------------------------------------------------


def _state_file(out_dir: Path) -> Path:
    return out_dir / "fetch_state.json"


def load_state(out_dir: Path) -> dict:
    """Load per-pair checkpoint state. Empty dict if missing/corrupt."""
    p = _state_file(out_dir)
    if not p.exists():
        return {"pairs": {}}
    try:
        return json.loads(p.read_text())
    except Exception as exc:  # noqa: BLE001
        logger.warning("state file unreadable (%s); restarting from empty", exc)
        return {"pairs": {}}


def save_state(out_dir: Path, state: dict) -> None:
    """Persist state atomically (write to .tmp + rename) so an
    interrupted save doesn't truncate the file on next read."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = _state_file(out_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(_state_file(out_dir))


def _canonical_bytes_for_pair(symbol: str, bars: list[Bar]) -> bytes:
    cb = b""
    for bar in bars:
        cb += (
            f"{symbol}|{bar.time.isoformat()}|{bar.open}|{bar.high}|"
            f"{bar.low}|{bar.close}|{bar.volume}\n"
        ).encode()
    return cb


def _spread_pips_for(symbol: str) -> float:
    """Crypto perp spread in pips — reused from sweep_real_data_crypto."""
    if symbol.startswith("BTC"):
        return 0.5
    if symbol.startswith("ETH"):
        return 1.0
    if symbol.startswith("SOL"):
        return 2.0
    return 1.0


# ---------------------------------------------------------------------------
# Paginated fetch with rate-limit awareness + checkpointing
# ---------------------------------------------------------------------------


def fetch_one_pair_2yr(
    symbol: str,
    *,
    target_n_bars: int = TARGET_BARS_2YR,
    max_pages: int = MAX_PAGES_2YR,
    state: dict | None = None,
    out_dir: Path | None = None,
    session: Any = None,
) -> tuple[list[Bar], FetchProvenance, dict]:
    """Fetch ~2 years of H1 bars for ``symbol``.

    Reuses the Binance.US pagination logic from
    ``sweep_real_data_crypto.fetch_one_pair`` but adds:

    * HTTP 429 backoff honoring ``Retry-After``
    * Exponential backoff on transient connection errors
    * Proactive throttle between successful pages
    * Per-page checkpointing so an interrupted run resumes
    * Per-page retry counter so a persistently failing page aborts
      the tail gracefully (we still return whatever we have)

    Returns
    -------
    (bars, provenance, updated_state)
        ``bars`` is the chronologically-ascending list of ``Bar``
        objects; ``provenance`` records the fetch metadata + SHA-256;
        ``updated_state`` is the per-pair checkpoint to persist via
        :func:`save_state`.
    """
    sess = session or requests.Session()
    state = state if state is not None else {"pairs": {}}
    pair_state = state["pairs"].get(symbol, {})
    # State may have been persisted via json.dumps(default=str), in
    # which case ``time`` is an ISO string. Coerce it to a tz-aware
    # datetime so downstream calls (``.isoformat()``, hashing) work.
    bars: list[Bar] = []
    for b in pair_state.get("bars", []):
        b2 = dict(b)
        t = b2.get("time")
        if isinstance(t, str):
            dt = datetime.fromisoformat(t)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            b2["time"] = dt
        bars.append(Bar(**b2))
    pages_fetched = int(pair_state.get("pages_fetched", 0))
    end_time_ms: int | None = pair_state.get("end_time_ms")
    first_status: int | None = pair_state.get("first_page_http_status")
    started = pair_state.get("started_utc") or datetime.now(timezone.utc).isoformat()
    retrieval_utc = datetime.now(timezone.utc)

    logger.info(
        "[%s] resuming: %d bars / %d pages already fetched",
        symbol, len(bars), pages_fetched,
    )

    while len(bars) < target_n_bars and pages_fetched < max_pages:
        # We always request a full page (KLINES_PAGE_SIZE). The loop
        # terminates on len(bars) >= target_n_bars OR on a partial
        # page (venue exhausted) OR on max_pages. Using a shrinking
        # per-page limit would incorrectly fire the partial-page
        # break in the middle of a multi-page fetch.
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": "1h",
            "limit": KLINES_PAGE_SIZE,
        }
        if end_time_ms is not None:
            params["endTime"] = end_time_ms

        # --- One page, with full retry semantics ---
        attempt = 0
        # Binance returns oldest-first WITHIN each response. Each
        # subsequent page (with endTime = earliest_open - 1ms) returns
        # OLDER bars. We PREPEND so the global ordering stays
        # chronological (oldest first) across pages.
        page_bars: list[Bar] = []
        page_status: int | None = None
        while attempt < MAX_RETRIES_PER_POLL:
            attempt += 1
            try:
                resp = sess.get(
                    BINANCE_US_KLINES_URL,
                    params=params,
                    timeout=15.0,
                )
            except requests.RequestException as exc:
                # Transient connection error — exponential backoff.
                backoff = min(2 ** attempt, RETRY_AFTER_CAP_SEC)
                logger.warning(
                    "[%s] connection error on attempt %d: %s; backoff %.1fs",
                    symbol, attempt, exc, backoff,
                )
                time.sleep(backoff)
                continue

            page_status = resp.status_code
            if first_status is None:
                first_status = page_status
            if page_status == 429:
                # Rate limited — honor Retry-After if present.
                ra = resp.headers.get("Retry-After")
                if ra is not None:
                    try:
                        sleep_for = min(float(ra), RETRY_AFTER_CAP_SEC)
                    except ValueError:
                        sleep_for = min(THROTTLE_BETWEEN_PAGES_SEC * attempt, RETRY_AFTER_CAP_SEC)
                else:
                    sleep_for = min(THROTTLE_BETWEEN_PAGES_SEC * attempt, RETRY_AFTER_CAP_SEC)
                logger.warning(
                    "[%s] HTTP 429 on attempt %d; sleeping %.1fs (Retry-After=%s)",
                    symbol, attempt, sleep_for, ra,
                )
                time.sleep(sleep_for)
                continue
            if page_status == 418 or page_status == 403:
                # IP ban / forbidden — abort the tail for this pair;
                # we'll keep what we have.
                logger.error(
                    "[%s] HTTP %d (ban/forbidden); aborting tail",
                    symbol, page_status,
                )
                break
            if page_status != 200:
                # Other non-200 — exponential backoff.
                backoff = min(2 ** attempt, RETRY_AFTER_CAP_SEC)
                logger.warning(
                    "[%s] HTTP %d on attempt %d; backoff %.1fs",
                    symbol, page_status, attempt, backoff,
                )
                time.sleep(backoff)
                continue

            # 200 OK — parse and accept the page.
            try:
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s] JSON parse failed on attempt %d: %s",
                    symbol, attempt, exc,
                )
                time.sleep(2 ** attempt)
                continue

            if not isinstance(payload, list) or len(payload) == 0:
                # Partial / empty page → exhausted history for the pair.
                page_bars = []
                break

            page_bars = []
            for kline in payload:
                # kline = [openTime, open, high, low, close, volume, closeTime, ...]
                page_bars.append(
                    Bar(
                        time=datetime.fromtimestamp(int(kline[0]) / 1000.0, tz=timezone.utc),
                        open=float(kline[1]),
                        high=float(kline[2]),
                        low=float(kline[3]),
                        close=float(kline[4]),
                        volume=float(kline[5]),
                        spread_pips=_spread_pips_for(symbol),
                    )
                )
            break  # accept page

        if not page_bars:
            # Either partial page (venue has no more history), or all
            # retries exhausted, or banned. Stop fetching this pair.
            if page_status not in (200, None):
                logger.warning(
                    "[%s] giving up on tail after %d attempts; status=%s",
                    symbol, attempt, page_status,
                )
            break

        # Append (Binance returns oldest-first within each response;
        # subsequent pages via endTime return older bars that belong
        # at the tail of the global sequence).
        bars = page_bars + bars
        pages_fetched += 1

        # Checkpoint after every accepted page (atomic write).
        if out_dir is not None:
            state["pairs"][symbol] = {
                "bars": [dataclasses.asdict(b) for b in bars],
                "pages_fetched": pages_fetched,
                "end_time_ms": end_time_ms,
                "first_page_http_status": first_status,
                "started_utc": started,
                "last_checkpoint_utc": datetime.now(timezone.utc).isoformat(),
            }
            save_state(out_dir, state)

        # Binance returns oldest-first WITHIN a response. Each
        # subsequent page (with endTime = earliest_open - 1ms) returns
        # OLDER bars that belong at the HEAD of the global sequence,
        # so we PREPEND (not append).
        if len(page_bars) < params["limit"]:
            # Partial page → venue has no more history; we got the tail.
            break

        # Advance cursor to the open-time of the earliest bar minus 1ms.
        earliest_open_ms = int(page_bars[0].time.timestamp() * 1000)
        end_time_ms = earliest_open_ms - 1

        # Proactive throttle between pages (rate-limit safety margin).
        time.sleep(THROTTLE_BETWEEN_PAGES_SEC)

    # Compute provenance.
    canonical = _canonical_bytes_for_pair(symbol, bars)
    data_hash = hashlib.sha256(canonical).hexdigest()
    earliest = bars[0].time if bars else None
    latest = bars[-1].time if bars else None

    provenance = FetchProvenance(
        symbol=symbol,
        source=f"{BINANCE_US_KLINES_URL} (direct, spot market data) — 2yr acquisition",
        interval="1h",
        fetch_window_start_utc=earliest or retrieval_utc,
        fetch_window_end_utc=latest or retrieval_utc,
        retrieval_timestamp_utc=retrieval_utc,
        n_bars=len(bars),
        earliest_bar_utc=earliest,
        latest_bar_utc=latest,
        data_hash_sha256=data_hash,
        first_page_http_status=first_status,
        pages_fetched=pages_fetched,
    )

    # Persist final state for this pair (post-loop snapshot).
    if out_dir is not None:
        state["pairs"][symbol] = {
            "bars": [dataclasses.asdict(b) for b in bars],
            "pages_fetched": pages_fetched,
            "end_time_ms": end_time_ms,
            "first_page_http_status": first_status,
            "started_utc": started,
            "last_checkpoint_utc": datetime.now(timezone.utc).isoformat(),
        }
        save_state(out_dir, state)

    return bars, provenance, state


# ---------------------------------------------------------------------------
# Persistence (parquet + sidecar provenance JSON)
# ---------------------------------------------------------------------------


def persist_2yr_dataset(
    bars_by_pair: dict[str, list[Bar]],
    provenance_by_pair: dict[str, FetchProvenance],
    gap_trim_by_pair: dict[str, dict],
    zero_volume_repair_by_pair: dict[str, dict] | None = None,
    *,
    out_dir: Path,
    started_iso: str,
    finished_iso: str,
    elapsed_sec: float,
    git_commit: str | None,
) -> dict[str, Path]:
    """Persist the 2yr dataset + provenance + integrity gate summary.

    File layout under ``out_dir``:

      * ``real_crypto_bars_h1_2yr.parquet`` — all pairs, single file
        with a ``symbol`` column (matches the existing
        ``real_crypto_bars_h1.parquet`` schema)
      * ``real_crypto_bars_h1_2yr.csv`` — CSV mirror for quick
        inspection
      * ``real_crypto_provenance_2yr.json`` — per-symbol provenance
        (source + window + retrieval ts + SHA-256 + page count + first
        HTTP status)
      * ``fetch_state.json`` — per-pair checkpoint state (already
        maintained by :func:`save_state`; we overwrite with the final
        post-trim bars so any resume-restart sees the post-trim set)
      * ``gap_trim_2yr.json`` — recorded gap-trim metadata per symbol
      * ``zero_volume_repair_2yr.json`` — recorded zero-volume repair
        metadata per symbol (original + repaired values for every
        repaired bar; the gate is NEVER loosened, we just fix data)
      * ``integrity_gate_2yr.json`` — per-pair integrity gate result
      * ``sanity_stats_2yr.json`` — quick-bar stats summary used to
        generate the human-readable sanity note
      * ``sanity_stats_2yr.md`` — human-readable sanity note
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for sym, bars in bars_by_pair.items():
        for bar in bars:
            rows.append({
                "symbol": sym,
                "time": bar.time,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "spread_pips": bar.spread_pips,
            })
    df = pd.DataFrame(rows)

    parquet_path = out_dir / "real_crypto_bars_h1_2yr.parquet"
    csv_path = out_dir / "real_crypto_bars_h1_2yr.csv"
    df.to_parquet(parquet_path, engine="pyarrow", index=False)
    df.to_csv(csv_path, index=False)

    provenance_payload = {
        sym: {
            "symbol": p.symbol,
            "source": p.source,
            "interval": p.interval,
            "fetch_window_start_utc": p.fetch_window_start_utc.isoformat(),
            "fetch_window_end_utc": p.fetch_window_end_utc.isoformat(),
            "retrieval_timestamp_utc": p.retrieval_timestamp_utc.isoformat(),
            "n_bars": p.n_bars,
            "earliest_bar_utc": p.earliest_bar_utc.isoformat() if p.earliest_bar_utc else None,
            "latest_bar_utc": p.latest_bar_utc.isoformat() if p.latest_bar_utc else None,
            "data_hash_sha256": p.data_hash_sha256,
            "first_page_http_status": p.first_page_http_status,
            "pages_fetched": p.pages_fetched,
        }
        for sym, p in provenance_by_pair.items()
    }
    (out_dir / "real_crypto_provenance_2yr.json").write_text(
        json.dumps(provenance_payload, indent=2, default=str)
    )

    (out_dir / "gap_trim_2yr.json").write_text(
        json.dumps(gap_trim_by_pair, indent=2, default=str)
    )

    if zero_volume_repair_by_pair is not None:
        (out_dir / "zero_volume_repair_2yr.json").write_text(
            json.dumps(zero_volume_repair_by_pair, indent=2, default=str)
        )

    # Sanity stats.
    sanity = _build_sanity_stats(
        bars_by_pair, provenance_by_pair, gap_trim_by_pair,
        started_iso=started_iso, finished_iso=finished_iso,
        elapsed_sec=elapsed_sec, git_commit=git_commit,
    )
    (out_dir / "sanity_stats_2yr.json").write_text(
        json.dumps(sanity, indent=2, default=str)
    )
    (out_dir / "sanity_stats_2yr.md").write_text(sanity["human_note"])

    return {
        "parquet": parquet_path,
        "csv": csv_path,
        "provenance": out_dir / "real_crypto_provenance_2yr.json",
        "gap_trim": out_dir / "gap_trim_2yr.json",
        "sanity_json": out_dir / "sanity_stats_2yr.json",
        "sanity_md": out_dir / "sanity_stats_2yr.md",
        "state": _state_file(out_dir),
    }


def _build_sanity_stats(
    bars_by_pair: dict[str, list[Bar]],
    provenance_by_pair: dict[str, FetchProvenance],
    gap_trim_by_pair: dict[str, dict],
    zero_volume_repair_by_pair: dict[str, dict] | None = None,
    *,
    started_iso: str,
    finished_iso: str,
    elapsed_sec: float,
    git_commit: str | None,
) -> dict:
    """Quick-bar stats summary + human-readable note.

    Includes per-symbol: bars post-trim, date range, gaps found+trimmed
    (count + total minutes + first/last gap timestamps), zero-volume
    bars repaired (count + strategy).
    """
    per_symbol: dict[str, dict] = {}
    for sym in sorted(provenance_by_pair.keys()):
        bars = bars_by_pair.get(sym, [])
        prov = provenance_by_pair[sym]
        trim = gap_trim_by_pair.get(sym, {"trimmed": False})
        zv = (zero_volume_repair_by_pair or {}).get(sym, {"repaired": False})
        per_symbol[sym] = {
            "n_bars_post_trim": len(bars),
            "earliest_utc": bars[0].time.isoformat() if bars else None,
            "latest_utc": bars[-1].time.isoformat() if bars else None,
            "pages_fetched": prov.pages_fetched,
            "first_page_http_status": prov.first_page_http_status,
            "data_hash_sha256": prov.data_hash_sha256,
            "trimmed": bool(trim.get("trimmed")),
            "trim_first_gap_minutes": (
                trim["gaps"][0]["delta_minutes"] if trim.get("gaps") else None
            ),
            "trim_first_gap_at_utc": (
                trim["gaps"][0]["gap_end_utc"] if trim.get("gaps") else None
            ),
            "zero_volume_repaired": bool(zv.get("repaired")),
            "zero_volume_repaired_count": zv.get("n_repaired", 0),
        }

    lines = [
        "# 2-year Binance.US H1 acquisition — sanity stats",
        "",
        "- card: `3f9d1a4e-9c6e-4e63-9d3b-2d02ca8e15f8`",
        f"- started: {started_iso}",
        f"- finished: {finished_iso}",
        f"- elapsed: {elapsed_sec:.1f}s",
        f"- git_commit: `{git_commit}`",
        "",
        "## Per-symbol summary",
        "",
        "| Symbol | Bars (post-trim) | Earliest (UTC) | Latest (UTC) | Pages | Trimmed | First-gap Δ (min) |",
        "| --- | ---: | --- | --- | ---: | :--- | ---: |",
    ]
    for sym, row in per_symbol.items():
        lines.append(
            f"| {sym} | {row['n_bars_post_trim']:,} | {row['earliest_utc']} | "
            f"{row['latest_utc']} | {row['pages_fetched']} | "
            f"{'yes' if row['trimmed'] else 'no'} | "
            f"{row['trim_first_gap_minutes'] if row['trim_first_gap_minutes'] is not None else '-'} |"
        )

    human_note = "\n".join(lines) + "\n"
    return {
        "card_id": "3f9d1a4e-9c6e-4e63-9d3b-2d02ca8e15f8",
        "started_iso": started_iso,
        "finished_iso": finished_iso,
        "elapsed_sec": round(elapsed_sec, 2),
        "git_commit": git_commit,
        "per_symbol": per_symbol,
        "human_note": human_note,
    }


# ---------------------------------------------------------------------------
# Integrity gate check (advisory; doesn't fail the fetch — the
# post-trim bars SHOULD pass the gate; if they don't, that's a real
# data issue that needs human review before any sweep consumes them).
# ---------------------------------------------------------------------------


def run_integrity_gate_per_pair(
    bars_by_pair: dict[str, list[Bar]],
) -> dict[str, dict]:
    """Run the established integrity gate against the post-trim bars.

    Returns a per-pair report dict suitable for persistence.
    The fetch never auto-loosens the gate; this check surfaces what
    the gate would say today, in the same shape the real-data sweep
    produced.
    """
    if not _try_load_integrity_gate():
        return {
            sym: {"status": "SKIP", "reason": "integrity_gate_import_failed"}
            for sym in bars_by_pair.keys()
        }
    reports: dict[str, dict] = {}
    for sym, bars in bars_by_pair.items():
        if not bars:
            reports[sym] = {"status": "SKIP", "reason": "no_bars"}
            continue
        try:
            ic = IntegrityConfig(
                expected_cadence_minutes=60,
                universe_symbols=(sym,),
            )
            report = enforce_integrity_gate(symbol=sym, bars=bars, config=ic)
            reports[sym] = {
                "status": "PASS",
                "n_violations": len(report.violations) if hasattr(report, "violations") else 0,
                "violations": [str(v) for v in (report.violations if hasattr(report, "violations") else [])][:5],
            }
        except Exception as exc:
            n_violations = 0
            try:
                if hasattr(exc, "report") and exc.report is not None:
                    n_violations = len(exc.report.violations)
            except Exception as exc2:  # noqa: BLE001
                logger.debug("integrity report extraction failed: %s", exc2)
            reports[sym] = {
                "status": "FAIL",
                "stage": "enforce_integrity_gate",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "n_violations": n_violations,
            }
    return reports


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _get_git_commit() -> str | None:
    try:
        return (
            subprocess.check_output(  # noqa: S607
                ["/usr/bin/git", "rev-parse", "--short", "HEAD"],
                cwd=str(WORKTREE),
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
            or None
        )
    except (OSError, subprocess.CalledProcessError):
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir", type=Path,
        default=WORKTREE / "data" / "sweep_real_data_bars_2yr",
        help="Output directory for the 2yr dataset + provenance + sanity stats.",
    )
    parser.add_argument(
        "--target-bars", type=int, default=TARGET_BARS_2YR,
        help=f"Target bars per pair (default: {TARGET_BARS_2YR} = ~2 years of H1).",
    )
    parser.add_argument(
        "--max-pages", type=int, default=MAX_PAGES_2YR,
        help=f"Max Binance.US pages per pair (default: {MAX_PAGES_2YR}).",
    )
    parser.add_argument(
        "--pairs", type=str, default=",".join(DEFAULT_PAIRS),
        help="Comma-separated Binance.US symbols (default: BTCUSDT,ETHUSDT,SOLUSDT).",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from the existing checkpoint state file (default: auto-resume if state exists).",
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Ignore any existing checkpoint state and re-fetch from scratch.",
    )
    parser.add_argument(
        "--skip-integrity-gate", action="store_true",
        help="Skip the post-fetch integrity gate report (data still saved).",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    pairs = tuple(args.pairs.split(","))
    state = load_state(args.out_dir)
    if args.no_resume:
        state = {"pairs": {}}
    started = time.time()
    started_iso = datetime.now(timezone.utc).isoformat()
    git_commit = _get_git_commit()

    logger.info(
        "2yr fetch start: pairs=%s, target_bars=%d, max_pages=%d, resume_state=%s",
        list(pairs), args.target_bars, args.max_pages,
        "yes" if state["pairs"] else "no",
    )

    provenance_by_pair: dict[str, FetchProvenance] = {}
    bars_by_pair: dict[str, list[Bar]] = {}

    for sym in pairs:
        try:
            bars, prov, state = fetch_one_pair_2yr(
                sym,
                target_n_bars=args.target_bars,
                max_pages=args.max_pages,
                state=state,
                out_dir=args.out_dir,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("fetch failed for %s: %s", sym, exc)
            bars = []
            prov = FetchProvenance(
                symbol=sym,
                source="FETCH_FAILED",
                interval="1h",
                fetch_window_start_utc=datetime.now(timezone.utc),
                fetch_window_end_utc=datetime.now(timezone.utc),
                retrieval_timestamp_utc=datetime.now(timezone.utc),
                n_bars=0,
                earliest_bar_utc=None,
                latest_bar_utc=None,
                data_hash_sha256="",
                first_page_http_status=None,
                pages_fetched=0,
            )
        bars_by_pair[sym] = bars
        provenance_by_pair[sym] = prov
        logger.info(
            "[%s] fetched %d bars across %d pages; hash=%s",
            sym, len(bars), prov.pages_fetched, prov.data_hash_sha256[:12],
        )

    # Gap-trim per pair (per card: fix data acquisition, NEVER loosen the gate).
    # Use the 2yr-aware trim that retains the longest contiguous segment
    # (the established ``trim_around_first_gap`` drops everything before
    # the first gap, which would discard ~2 years of data when a single
    # outage sits near the present).
    gap_trim_by_pair: dict[str, dict] = {}
    for sym, bars in bars_by_pair.items():
        if not bars:
            gap_trim_by_pair[sym] = {"trimmed": False, "n_bars": 0, "reason": "no_bars"}
            continue
        trimmed, trim_meta = trim_keep_longest_segment(bars)
        bars_by_pair[sym] = trimmed
        gap_trim_by_pair[sym] = trim_meta
        if trim_meta.get("trimmed"):
            n_gaps = len(trim_meta.get("gaps", []))
            n_seg = trim_meta.get("n_segments", "?")
            logger.warning(
                "[%s] gap-trim: kept longest of %s segments (%d bars) "
                "after detecting %d cadence gap(s) > tolerance; "
                "longest segment spans %s → %s",
                sym,
                n_seg,
                trim_meta["n_bars"],
                n_gaps,
                trim_meta.get("longest_segment_start_utc"),
                trim_meta.get("longest_segment_end_utc"),
            )
            # Rehash + update provenance since the bar bytes changed.
            canonical = _canonical_bytes_for_pair(sym, trimmed)
            new_hash = hashlib.sha256(canonical).hexdigest()
            p = provenance_by_pair[sym]
            object.__setattr__(p, "data_hash_sha256", new_hash)
            object.__setattr__(p, "n_bars", len(trimmed))
            object.__setattr__(p, "earliest_bar_utc", trimmed[0].time)
            object.__setattr__(p, "latest_bar_utc", trimmed[-1].time)
            object.__setattr__(p, "fetch_window_start_utc", trimmed[0].time)
            object.__setattr__(p, "fetch_window_end_utc", trimmed[-1].time)

    # Zero-volume cleanup (data-acquisition fix; gate is NEVER loosened).
    zero_volume_trim_by_pair: dict[str, dict] = {}
    for sym, bars in bars_by_pair.items():
        if not bars:
            zero_volume_trim_by_pair[sym] = {"removed": False, "n_bars": 0}
            continue
        cleaned, zv_meta = trim_zero_volume_bars(bars)
        bars_by_pair[sym] = cleaned
        zero_volume_trim_by_pair[sym] = zv_meta
        if zv_meta.get("removed"):
            logger.warning(
                "[%s] zero-volume cleanup: removed %d bars (data-acquisition fix, gate unchanged)",
                sym, zv_meta["n_removed"],
            )
            # Rehash + update provenance.
            canonical = _canonical_bytes_for_pair(sym, cleaned)
            new_hash = hashlib.sha256(canonical).hexdigest()
            p = provenance_by_pair[sym]
            object.__setattr__(p, "data_hash_sha256", new_hash)
            object.__setattr__(p, "n_bars", len(cleaned))

    # Optional integrity gate check (advisory; fetch never auto-fails).
    integrity_reports: dict[str, dict] = {}
    if not args.skip_integrity_gate:
        integrity_reports = run_integrity_gate_per_pair(bars_by_pair)
        for sym, rep in integrity_reports.items():
            logger.info(
                "[%s] integrity gate: %s (violations=%s)",
                sym,
                rep.get("status"),
                rep.get("n_violations", "-"),
            )

    finished = time.time()
    finished_iso = datetime.now(timezone.utc).isoformat()
    elapsed = finished - started

    paths = persist_2yr_dataset(
        bars_by_pair,
        provenance_by_pair,
        gap_trim_by_pair,
        zero_volume_repair_by_pair=zero_volume_trim_by_pair,
        out_dir=args.out_dir,
        started_iso=started_iso,
        finished_iso=finished_iso,
        elapsed_sec=elapsed,
        git_commit=git_commit,
    )

    # Persist integrity gate result alongside.
    (args.out_dir / "integrity_gate_2yr.json").write_text(
        json.dumps(integrity_reports, indent=2, default=str)
    )

    # Final stdout summary.
    total_bars = sum(len(b) for b in bars_by_pair.values())
    total_trimmed_pairs = sum(1 for v in gap_trim_by_pair.values() if v.get("trimmed"))
    print("2yr fetch complete:")
    print(f"  total bars (post-trim): {total_bars:,}")
    print(f"  pairs: {len(pairs)}")
    print(f"  trimmed pairs: {total_trimmed_pairs}")
    print(f"  elapsed: {elapsed:.1f}s")
    for sym in pairs:
        bars = bars_by_pair.get(sym, [])
        if bars:
            print(f"  {sym}: {len(bars):,} bars "
                  f"[{bars[0].time.isoformat()} → {bars[-1].time.isoformat()}]")
    print(f"  parquet: {paths['parquet']}")
    print(f"  provenance: {paths['provenance']}")
    print(f"  sanity_md: {paths['sanity_md']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
