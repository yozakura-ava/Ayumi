# BQ-1038 Build Report: CPU + Memory Protection Layer — Finish & Verify

**Date:** 2026-07-01  
**Host:** xeon (Linux 6.8.0-111-generic, x64)  
**Repo:** $AYUMI_ROOT  
**Venv:** $AYUMI_ROOT/.venv (Python 3.12)  
**Cores:** 8 logical CPUs

---

## 1. pytest-memray Installation

Command:

```bash
cd $AYUMI_ROOT
source .venv/bin/activate
pip install "pytest-memray>=1.6"
```

Result:

```text
Requirement already satisfied: pytest-memray>=1.6 in ./.venv/lib/python3.12/site-packages (1.8.0)
Requirement already satisfied: memray>=1.12 in ./.venv/lib/python3.12/site-packages (1.19.3)
```

pytest-memray **was already installed** in the shared venv. The `requirements.txt` constraint (`pytest-memray>=1.6`) is satisfied.

---

## 2. pytest --memray Run on `tests/test_resource_limits.py`

Command:

```bash
pytest --memray tests/test_resource_limits.py -q
```

Result:

```text
14 passed in 0.26s
```

All 14 existing resource-limit tests pass under memray profiling. The `[memray]` block in `pytest.ini` (`limit_memory = 2GB`) is respected; no tests exceeded the memory limit.

---

## 3. 60s CPU E2E Test

New file: `tests/test_cpu_limit_e2e.py`  
The test is marked `@pytest.mark.skip(reason="60s test; run manually")` so it does not run in normal CI.

### Measurement Methodology

- Ran a 60-second CPU-busy loop inside `cpu_limited(percent=20)`.
- Used `psutil.Process().cpu_percent()` averaged over the window.
- Loop body: `_ = sum(i * i for i in range(1000))` with a 0.1 ms `time.sleep()` slice so the OS scheduler and nice value have a chance to throttle the process.
- Compared against a 60-second baseline **without** `cpu_limited()`.

### Results

| Run | Configuration | Elapsed | Measured CPU% |
|-----|---------------|---------|---------------|
| 1   | with `cpu_limited(20)` | 60.0 s | 24.80% |
| 2   | with `cpu_limited(20)` | 60.0 s | 33.10% |
| 3   | with `cpu_limited(20)` | 60.0 s | 43.50% |
| —   | baseline (no limiter)  | 60.0 s | 36.70% |

Observations:

- The limiter does **not** reliably hold CPU usage at or below 20%.
- On a quiet host, run 1 was close (24.8%), but subsequent runs drifted higher.
- The current implementation relies on `cpu_affinity()` plus `os.nice(19)`, which is advisory-only and ineffective when other cores are idle or when the scheduler ignores nice.

Conclusion: **measured CPU% > 20%**. Advisory enforcement is insufficient for hard guarantees.

---

## 4. Files Modified

- `tests/test_cpu_limit_e2e.py` — new E2E measurement test (skipped by default)
- `docs/builds/BQ-1038-build-report.md` — this report
- `docs/LIMITATIONS.md` — addendum added (see section 5)
- `src/forex_bot/common/resource_limits.py` — unchanged (no bugs surfaced)
- `tests/test_resource_limits.py` — unchanged
- `requirements.txt` — unchanged (already declares `pytest-memray>=1.6`)

---

## 5. LIMITATIONS.md Addendum

Added to `docs/LIMITATIONS.md`:

> ### CPU limiter is advisory-only (BQ-1038)
>
> `cpu_limited()` in `src/forex_bot/common/resource_limits.py` enforces CPU
> throttling via `psutil.Process.cpu_affinity()` and `os.nice(19)`. This is
> advisory; on an 8-core host a 60-second CPU-busy loop under `cpu_limited(20)`
> measured 24.8–43.5% CPU usage, exceeding the 20% target. For hard CPU caps
> a cgroup-based solution (e.g. `cpu.cfs_quota_us` / systemd slice) is
> required. Tracked in follow-up BQ-1039.

---

## 6. Follow-up BQ

- **BQ-1039**: "cgroup-based hard CPU enforcement for `cpu_limited()`" — filed via workboard.

---

## 7. Git

Commit: `BQ-1038: finish CPU protection layer verification (install + measure)`

---

## Acceptance Criteria

- [x] pytest-memray installed successfully (already present, v1.8.0)
- [x] `pytest --memray tests/test_resource_limits.py -q` ran — 14 passed
- [x] 60s CPU test created and measured CPU% documented
- [x] Build report captures all measurements
- [x] CPU% > 20% → LIMITATIONS.md addendum written + follow-up BQ-1039 filed
