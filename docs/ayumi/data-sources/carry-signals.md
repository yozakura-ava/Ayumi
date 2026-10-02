# Carry Signals — FRED + ECB + MT5 Swap

> **Card:** `[AYUMI] Wire FRED policy rates + MT5 swap feeds into Cabal
> confidence layer` (e87be156-7fce-4ae7-b234-08311a86ee27)
>
> **Status:** complete — see *Builder Completion Report* below.

## Why carry

Forex carry is the structural, multi-week flow signal that arises from
holding a currency whose central-bank rate exceeds the rate of the
currency you're short. Three free public data sources together cover
all three legs of the carry trade that Ayumi needs:

| Source | Currency | Provider |
| ------ | -------- | -------- |
| FRED (`FEDFUNDS`) | USD | `fredapi` library + offline fallback |
| ECB SDMX (`MR/UST`) | EUR | `data-api.ecb.europa.eu` REST endpoint |
| MT5 terminal swap feed | All symbols | Daily CSV dump → `MT5SwapFileProvider` |

Other G7 rates (GBP, AUD, NZD, JPY, CHF, CAD) sit in a static lookup
table; refreshing those quarterly is on the Ava-facing roadmap.

## Design choices

### Carry is a regime feature, not a tactical entry signal

The card is explicit:

> "Forex carry = structural, multi-week flow signal. Wire as regime/bias
> feature with daily update cadence, NOT tactical entry feature."

We therefore model carry as a regime classifier (`CarryRegime.POSITIVE /
NEGATIVE / NEUTRAL`) rather than a numeric confidence score. The
confidence engine consumes only the POSITIVE regime as a confluence boost
(0.05 strategy agreement + 0.025 daily timeframe alignment) — NEGATIVE
and NEUTRAL regimes return `None`, so a "long USDJPY" signal gets *no*
carry boost when the rate differential has flipped to favouring JPY-funded
positions. That absence — relative to a carry-blind baseline — is the
"confidence adjustment" the regime shift is meant to trigger.

### Daily cadence, no real-time pressure

None of the three sources are polled more than once per day in production.
FRED and ECB SDMX are cached on disk; the MT5 swap feed is read from a
CSV that the daily MT5 script populates. There is no websocket, no real
MT5 server connection inside this module, and no rate-limiting code.

### Zero new dependencies

`fredapi` is optional. When the library or API key is missing — or the
sandbox has no network egress — `:class:`FredFetcher` falls back to a
hand-maintained annual-effective-rate table that approximates the
post-2010 FOMC path well enough for regime classification. The ECB SDMX
provider uses `urllib.request` from the standard library. The MT5 swap
provider reads CSV via the stdlib `csv` module. No new packages need to
be added to `requirements.txt`.

## Public surface

```python
from data.fred_fetcher import FredFetcher
from data.carry_signals import CarrySignalProvider, score_with_carry
from confidence.engine import ConfidenceEngine

provider = CarrySignalProvider()
engine = ConfidenceEngine()

result = score_with_carry(
    engine, provider,
    raw_confidence=0.55,        # what the strategy produced
    symbol="USDJPY",
    direction="long",
    spread=...,                 # typical orchestrator metadata
    atr=...,
)
```

`score_with_carry` is a thin wrapper that mutates nothing on the
engine. It builds the same `confluences` list the orchestrator would
have built and injects `{"strategy": "carry_regime", "direction": "long",
"timeframe": "D1", "metadata": {...}}` only when the regime is
POSITIVE. NEGATIVE and NEUTRAL regimes are no-ops from the engine's
perspective.

### Plumbing the orchestrator

The Ayumi signal orchestrator already calls `engine.score(...)` with a
`confluences` list built per-strategy. To activate carry without
modifying the engine, build a `CarrySignalProvider` once, then in the
caller assemble the confluence list:

```python
confluences = strategy_built_confluences + [
    provider.to_confluence(symbol, direction),  # may be None
]
```

If `to_confluence` returns `None` (NEUTRAL or NEGATIVE), `score_with_carry`
silently drops it, so orchestrator code can stay unconditional. Production
wiring is left to a follow-up card because the orchestrator file lives
outside this card's `allowed_files`.

## Regime taxonomy

| Regime | Condition | Engine effect |
| ------ | --------- | -------------- |
| POSITIVE | `rate_diff ≥ min_diff` and aligned with direction | Inject carry_regime confluence (boost ≈ +0.075) |
| NEGATIVE | `rate_diff ≥ min_diff` opposite to direction, **or** extreme negative swap | No boost — relative decrease vs carry-blind baseline |
| NEUTRAL | `rate_diff < min_diff` **or** missing rate data | No boost, no penalty |

`min_diff` defaults to 0.5 % but is configurable per
`CarrySignalProvider` instance. An MT5 swap override forces NEGATIVE
when the relevant swap is below −50 points/day (≈ 5 USD per lot per
night) — that prevents the rare "rates look positive but the broker
roll eats the carry" situation.

## Currency mapping

```
EURUSD → base EUR / quote USD
GBPUSD → base GBP / quote USD
AUDUSD → base AUD / quote USD
NZDUSD → base NZD / quote USD
USDJPY → base USD / quote JPY
USDCHF → base USD / quote CHF
USDCAD → base USD / quote CAD
EURJPY → base EUR / quote JPY
GBPJPY → base GBP / quote JPY
EURGBP → base EUR / quote GBP
```

Unknown symbols evaluate to NEUTRAL with `rate_diff = None`. Adding
new symbols is a one-line change to `_BASE_CCY` / `_QUOTE_CCY`.

## Acceptance-criterion evidence

`tests/unit/data/test_carry_signals.py` contains 29 tests across:

* `TestStaticFredRates` — fallback-table filtering, default scope.
* `TestFredFetcherOffline` — offline mode, disk cache round-trip,
  explicit source injection.
* `TestECBSDMXProvider` — static fallback, non-EUR rejection,
  injected fetcher, malformed JSON recovery.
* `TestMT5SwapFileProvider` — in-memory records, missing symbols,
  disk roundtrip, missing-file behaviour.
* `TestCarryClassification` — positive/negative/neutral regimes,
  direction inversion, swap override, missing data, bad input.
* `TestCarryCurrencyHelpers` — base/quote lookup, unknown symbol.
* `TestScoreWithCarryRegimeShift` — **the acceptance criterion**:
  USDJPY confidence reverses between regimes, neutral carries don't
  perturb, and disabled carries remain engine-equivalent.
* `TestRealisticInputs` — two sample inputs with full dict
  comparison.

Run with:

```bash
PATH=$AYUMI_ROOT/.venv/bin:$PATH \
    python3 -m pytest $AYUMI_ROOT/tests/unit/data/test_carry_signals.py -q
# → 29 passed in ~0.7s
```

## Out of scope

* Non-USD central bank APIs beyond ECB (BOE, BOJ, etc.) — explicit
  follow-up card.
* Real-time swap updates — daily cadence only.
* Confidence-engine modifications (`src/forex_bot/confidence/engine.py`
  is **not** in `allowed_files`).
* Orchestrator wiring — `src/forex_bot/orchestrator/signal_orchestrator.py`
  is **not** in `allowed_files`; left as a follow-up card.

## Source-debt finding

The card's `Source` field lists `docs/research/srb/SRB-AYUMI-004.md`
as the originating Satoshi research document. That path does not
exist in the repo (`docs/research/` contains other research notes but
no `srb/` directory; no `SRB-*` file is present anywhere). The
substantive spec is in the card body, so this is a stale reference and
worth noting to Ava; it does not block the implementation.

## Builder Completion Report

- **Files modified:**
  - `src/forex_bot/data/fred_fetcher.py` (new, 8.9 KB)
  - `src/forex_bot/data/carry_signals.py` (new, 20.5 KB)
  - `tests/unit/data/test_carry_signals.py` (new, 19 KB)
  - `docs/ayumi/data-sources/carry-signals.md` (this file)
- **Syntax validation:**
  - `python3 -m py_compile src/forex_bot/data/fred_fetcher.py` → exit 0
  - `python3 -m py_compile src/forex_bot/data/carry_signals.py` → exit 0
  - `python3 -m py_compile tests/unit/data/test_carry_signals.py` → exit 0
- **Tests run:** `pytest tests/unit/data/test_carry_signals.py -q` → 29 passed in 0.68s
- **Integration notes:** `score_with_carry` wraps `ConfidenceEngine.score`
  without mutating it. `CarrySignalProvider.to_confluence` returns
  `Optional[dict]` so callers can append-or-skip unconditionally. The
  three data sources can all be replaced independently via the
  `Protocol` interfaces (`CentralBankRateProvider`, `SwapRateProvider`,
  `FredSource`).
- **Acceptance criteria:**
  - [x] fred_fetcher.py fetches USD policy rate history via fredapi (with offline fallback)
  - [x] carry_signals.py integrates ECB SDMX + MT5 swap feeds
  - [x] Carry wired as regime feature (daily update) in confidence layer via `score_with_carry`
  - [x] `test_carry_signals.py`: regime shift triggers confidence adjustment on USDJPY (`TestScoreWithCarryRegimeShift`)
  - [x] `docs/ayumi/data-sources/carry-signals.md`
- **Realistic input tests:** two paired samples in `TestRealisticInputs`
  with full dict comparison. USDJPY high-carry vs EURUSD low-carry.
- **Discrepancies:** card's source doc reference (`SRB-AYUMI-004.md`)
  does not exist on disk — noted above; flagged for Ava.
- **Status:** COMPLETE
