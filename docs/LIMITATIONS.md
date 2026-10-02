# Known Limitations

## CPU limiter is advisory-only (BQ-1038)

`cpu_limited()` in `src/forex_bot/common/resource_limits.py` enforces CPU
throttling via `psutil.Process.cpu_affinity()` and `os.nice(19)`. This is
advisory; on an 8-core host a 60-second CPU-busy loop under `cpu_limited(20)`
measured 24.8–43.5% CPU usage, exceeding the 20% target. For hard CPU caps
a cgroup-based solution (e.g. `cpu.cfs_quota_us` / systemd slice) is required.
Tracked in follow-up BQ-1039.
