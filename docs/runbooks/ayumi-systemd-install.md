# Ayumi systemd User Unit — Install & Operations Runbook

**Sprint:** ayumi-reliability-2026-07-05
**Card:** `9043c09c` Task 5.2
**Unit:** `ayumi-forward-test.service`
**User:** `$USER` (systemd *user* instance, NOT root)
**Service type:** `simple` — wraps `scripts/launch_blend_forward_test.py --live`

---

## 1. Purpose

This runbook describes how to install, verify, and operate the systemd user
unit that manages the Ayumi multi-strategy forward test. The unit design is
derived from the **May–June 2026 crash root-cause analysis** in
[`docs/post-mortems/ayumi-may-june-crash-root-cause-2026-07-05.md`](../post-mortems/ayumi-may-june-crash-root-cause-2026-07-05.md)
(§5.2 — "Recommended Unit Topology"). It encodes the *evidence-driven* set of
directives that mitigates every observed crash pattern **except** application
memory leaks, which require code-side work.

---

## 2. Where the unit lives

| Path | Owner | Notes |
|---|---|---|
| `~/.config/systemd/user/ayumi-forward-test.service` | `$USER:$USER`, mode `0644` | The unit file. Lives in the **user's** systemd config dir, NOT `/etc/systemd/system/`. |
| Logs | `journalctl --user -u ayumi-forward-test.service` | Unit uses `StandardOutput=journal` / `StandardError=journal`. Do NOT also append to `logs/*.log` — that doubles writes and creates clock skew between sources. |
| PID lock | `$AYUMI_ROOT/data/forward_test.pid` | Held by the launcher (B1 single-instance guard), NOT by systemd. systemd tracks the Main PID only. |

---

## 3. Pre-flight checklist

Before installing or re-installing:

1. **Confirm user is $USER.** Running as root is forbidden — the launcher's
   `_refuse_root()` guard exits *before* any credential read to avoid leaving
   root-owned state behind. Verify with `whoami`.
2. **Verify no stray root-owned state files in `data/`** — particularly
   `data/state/risk_guard_state.json`, `data/risk_state_blend.json`,
   `data/forward_test_health.json`. If any are present, the launcher will log
   a graceful warning but the RiskGuard will reset to defaults. From a clean
   session, the safe pattern is:
   ```bash
   sudo chown -R $USER:$USER $AYUMI_ROOT/data
   ```
   Run this once after any prior root test runs.
3. **Verify the venv exists** at `$AYUMI_ROOT/.venv/`.
   The `ExecStart` path is hard-coded to that venv — if it's missing, the
   unit will fail with `code=exited, status=203/EXEC` (no such file).
4. **Verify cTrader credentials are populated in `.env`** —
   `CTRADER_OPENAPI_CLIENT_ID`, `CTRADER_OPENAPI_CLIENT_SECRET`,
   `CTRADER_OPENAPI_ACCESS_TOKEN`, `CTRADER_OPENAPI_REFRESH_TOKEN`,
   `CTRADER_OPENAPI_ACCOUNT_ID`, `CTRADER_OPENAPI_TRADER_LOGIN`.
   Missing tokens produce `1/FAILURE` exits with a clear "verify the env vars"
   log line; the unit will auto-restart up to 5× in 300 s before giving up.

---

## 4. Install procedure

The unit lives at `~/.config/systemd/user/ayumi-forward-test.service` — the
**user's** systemd directory, not `/etc/systemd/system/`. This means the
service runs as `$USER` (uid 1000) and uses the user's D-Bus session
(`XDG_RUNTIME_DIR=/run/user/1000/bus`).

```bash
# 1. Confirm the directory exists
mkdir -p ~/.config/systemd/user

# 2. Write the unit file (this repo's docs/runbooks/ contains the canonical
#    template at §10 below; the on-disk file should be byte-identical).
cat > ~/.config/systemd/user/ayumi-forward-test.service <<'EOF'
[Unit]
Description=Ayumi Forward Test (live multi-strategy paper trading)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$AYUMI_ROOT
ExecStart=$AYUMI_ROOT/.venv/bin/python scripts/launch_blend_forward_test.py --live
Restart=on-failure
RestartSec=30
StartLimitBurst=5
StartLimitIntervalSec=300
RestartPreventExitStatus=75
SuccessExitStatus=75
TimeoutStopSec=10
KillMode=control-group
MemoryMax=512M
MemoryHigh=384M
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

# 3. Ensure ownership and perms ($USER-owned, world-readable)
chmod 644 ~/.config/systemd/user/ayumi-forward-test.service

# 4. Reload the user systemd daemon
systemctl --user daemon-reload

# 5. Enable autostart at login (optional — service runs only while user session is alive)
systemctl --user enable ayumi-forward-test.service

# 6. Start it now
systemctl --user start ayumi-forward-test.service
```

> **Note on "lingering":** without `loginctl enable-linger $USER`, the
> user systemd instance dies when the user logs out and the service stops.
> For a true "boots on machine startup and keeps running" deployment, enable
> lingering and promote the unit to system-level — but that requires the unit
> to live at `/etc/systemd/system/`, not `~/.config/systemd/user/`. The
> current design **deliberately** uses user-level: the trading service
> *should* stop when the operator logs out, to avoid unattended trades on a
> locked screen.

---

## 5. Verify the install

Wait 5 seconds, then:

```bash
# Status: should show 'active (running)' and a Main PID
systemctl --user status ayumi-forward-test.service

# Process check: should return the same PID as status
pgrep -af launch_blend_forward_test

# Journal: should show the launch sequence with NO error lines
sudo loginctl enable-linger $USER   # only if you want boot-time start; not required
journalctl --user -u ayumi-forward-test.service --since "1 min ago"
```

A healthy startup looks like:

```
… ayumi.ctrader.environment    | [Environment] mode=demo endpoint=… account=…
… ayumi.openapi_spot_feed      | [Startup Diagnostics] environment=… account=…
… ayumi.connection_state       | [spot_feed] State transition: disconnected → connecting
… ayumi.ctrader_connection     | Connected to demo.ctraderapi.com:5035
… ayumi.connection_state       | [spot_feed] State transition: connecting → connected
… ayumi.connection_state       | [spot_feed] State transition: app_authenticating → acct_authenticating
… ayumi.connection_state       | [spot_feed] State transition: acct_authenticating → authenticated
… ayumi.openapi_spot_feed      | OpenApiSpotFeed started: account=… symbols=3
… ayumi.forward_test           | Forward test started: symbols=['GBPUSD','USDJPY','EURUSD']
```

Healthy memory should sit in the **150–250 MB** range, peaking around **210 MB**
under load. The unit's `MemoryHigh=384M` will throttle but not kill; the
`MemoryMax=512M` will SIGKILL and systemd will restart per policy.

---

## 6. Operating procedures

### 6.1 Check status

```bash
systemctl --user status ayumi-forward-test.service
```

Look for `Active: active (running)` and `Memory:` line — anything in the
green zone (≤ 384 MB) is healthy.

### 6.2 View live logs

```bash
# Tail everything for the last 5 minutes
journalctl --user -u ayumi-forward-test.service --since "5 min ago" -f

# Filter to errors and warnings only
journalctl --user -u ayumi-forward-test.service -p warning

# Look for the periodic B5 health line
journalctl --user -u ayumi-forward-test.service --since "5 min ago" | grep "B5 Health"
```

### 6.3 Restart cleanly

```bash
systemctl --user restart ayumi-forward-test.service
```

This stops the service (sends SIGTERM, waits up to `TimeoutStopSec=10`,
escalates to SIGKILL via `KillMode=control-group`), then starts it fresh.

### 6.4 Stop and disable

```bash
systemctl --user stop ayumi-forward-test.service        # stop now
systemctl --user disable ayumi-forward-test.service     # do not auto-start next login
```

### 6.5 Tail the on-disk equity / health JSON (optional)

These are written by the launcher itself, independent of systemd:

```bash
tail -f $AYUMI_ROOT/data/forward_test_health.json
tail -f $AYUMI_ROOT/logs/equity_$(date +%Y-%m-%d).json
```

---

## 7. Design rationale (why each directive is set)

This section answers "why isn't this set to X?" for every non-obvious choice.
Each line maps to a specific evidence point in the B6 root-cause analysis.

| Directive | Value | Why this, not the alternative |
|---|---|---|
| `Type=simple` | (not `notify` or `forking`) | The launcher is a single long-lived process; `simple` is correct. `notify` would require `sd_notify` calls in the launcher — not implemented. |
| `After=network-online.target` + `Wants=…` | (not just `After=network.target`) | `network-online.target` waits for an interface to have a routable address. The cTrader OpenAPI connection must hit a live network, not just a configured one. |
| `Restart=on-failure` | (not `always` or `no`) | Clean exits (`0`) and intentional `75/TEMPFAIL` (transient auth) should NOT trigger restart. Failures (`1/FAILURE`, signals, memory-kill) should. |
| `RestartSec=30` | (not 5, not 120) | **8.6× the observed 3.4 s average failure duration** (B6 §5.3). Long enough to avoid the May-05 storm pattern; short enough that real recovery is fast. |
| `StartLimitBurst=5` | (not 3, not 10) | Caps the May-05 storm (15 failures in 9 min) at a manageable rate. After 5 fails in 300 s, systemd *gives up* and a human must intervene. |
| `StartLimitIntervalSec=300` | (not 60, not 600) | 5 failures in 5 min is the threshold. Shorter would trigger false positives during known-bad window (e.g., during a deploy). Longer would hide real outages. |
| `RestartPreventExitStatus=75` | (not present, not `1`) | The application explicitly exits `75` (= `EX_TEMPFAIL`) when auth is in a known-transient state. Auto-restarting into the same fault caused the **June 02 14:34–14:35 storm** (3 × TEMPFAIL in 47 s). |
| `SuccessExitStatus=75` | (paired with the above) | Symmetric — `75` is *not a failure*; it's a deliberate "I'm backing off, try again on next market cycle" marker. systemd treats it as a clean exit. |
| `TimeoutStopSec=10` | (not 90 default, not 30) | The `72252c2` SIGTERM handler in the launcher completes in 1–3 s. 10 s gives ample headroom for connection draining without leaving systemd hanging. |
| `KillMode=control-group` | (not `process`, not `mixed`) | The launcher spawns worker threads (Twisted reactor). `control-group` ensures they're killed with the main process. `process` would orphan threads → port stays bound → next start fails. |
| `MemoryHigh=384M` | throttle-only | The 210.7 MB peak (May 24 OOM candidate) plus 80% headroom. Triggers throttling under pressure but doesn't kill — gives the app a chance to recover. |
| `MemoryMax=512M` | SIGKILL on hit | 2.4× the highest observed peak. If this trips, there is a real leak and we need to bisect. Bounded — better than a runaway process consuming gigabytes. |
| `Environment=PYTHONUNBUFFERED=1` | (only this env var) | Forces line-buffered stdout/stderr so journal logs are real-time. **Do NOT add `PYTHONPATH` or `PATH` overrides** — `WorkingDirectory` plus the absolute venv path is sufficient. Adding PATH overrides can mask broken venvs. |
| `StandardOutput=journal` / `StandardError=journal` | (not `append:…log`) | The post-mortem identified a 19-day log gap (`ayumi_2026-05-20.log` → `ayumi_2026-06-09.log`) — application-side file logging is unreliable. The journal is retained by systemd and queryable by date. See §8 for the logrotate follow-up. |
| `WantedBy=default.target` | (not `multi-user.target`) | This is a *user* unit — `default.target` (user's default) is correct. `multi-user.target` is a system-level target and would be ignored for user units. |

### 7.1 Why `WatchdogSec=` is intentionally absent

The May-22 ABRT was caused by systemd's `WatchdogSec=300` (5 min). The
in-app `ConnectionWatchdog` operates at **30 s DEGRADED / 90 s FAILED**
thresholds — strictly faster than any plausible systemd watchdog. Re-enabling
systemd's watchdog would:

1. Be redundant with the in-app check.
2. Add an extra SIGABRT pathway that bypasses `ConnectionWatchdog`'s
   graceful DEGRADED transition.
3. Re-introduce the May-22 kill mechanism.

If we ever need it (e.g., OS-launch-level liveness *before* `ConnectionWatchdog`
initializes), the minimum safe value is `WatchdogSec=600` with the launcher
calling `sd_notify(0, "WATCHDOG=1")` once per 5 minutes on a confirmed-healthy
state. **Not in scope for this card.** Documented as a follow-up.

---

## 8. Known gaps (not solved by this unit)

These are flagged in the B6 post-mortem as **residual vectors** or **out-of-scope
debt**. They are NOT blockers for the 5.2 deployment, but the next iteration
of the reliability sprint should track them.

| # | Gap | Where to track |
|---|---|---|
| 1 | Application-side memory leak — `MemoryMax=512M` bounds the *consequence* but doesn't fix the *cause*. | New card: "Forward-test memory profile under load." |
| 2 | 19-day log gap (May 21 → Jun 9) is a systemic logging problem, not a systemd unit issue. | New card: "Logrotate config for `logs/ayumi_*.log` and `forward_test-*.log`." |
| 3 | Pre-existing root-owned state files (e.g., `data/state/risk_guard_state.json` with mode `0600`) prevent the user-owned service from reading them. | Operator pre-flight: `sudo chown -R $USER:$USER $AYUMI_ROOT/data`. See §3. |
| 4 | ConnectionWatchdog vs systemd Watchdog overlap — not a bug today, but a future `sd_notify` integration needs a careful design pass. | New card: "Forward-test `sd_notify` integration + systemd WatchdogSec=600." |
| 5 | Single-connection architecture depends on `ab8e4c5` (connection extraction). If that refactor regresses, the dual-connection death-spiral returns. | Covered by existing `17e72b0` resilience xfail tests. |

---

## 9. Verification log (most recent install)

The install procedure was executed on **2026-07-05** during sprint
`ayumi-reliability-2026-07-05` (card `9043c09c` Task 5.2) and verified by:

```
● ayumi-forward-test.service - Ayumi Forward Test (live multi-strategy paper trading)
     Loaded: loaded (/home/$USER/.config/systemd/user/ayumi-forward-test.service; enabled; preset: enabled)
     Active: active (running) since Sun 2026-07-05 16:57:12 UTC; 39s ago
   Main PID: 567359 (python)
      Tasks: 21 (limit: 38368)
     Memory: 151.0M (high: 384.0M max: 512.0M available: 232.9M peak: 155.9M)
        CPU: 3.042s
```

```
Jul 05 16:57:12 systemd: Started ayumi-forward-test.service.
Jul 05 16:57:14 ayumi.ctrader.environment:    [Environment] mode=demo endpoint=demo.ctraderapi.com account=REDACTED_CTRADER_ACCOUNT
Jul 05 16:57:14 ayumi.connection_state:       [spot_feed] State transition: disconnected → connecting
Jul 05 16:57:14 ayumi.ctrader_connection:     Connected to demo.ctraderapi.com:5035
Jul 05 16:57:16 ayumi.connection_state:       [spot_feed] State transition: acct_authenticating → authenticated
Jul 05 16:57:20 ayumi.openapi_spot_feed:      OpenApiSpotFeed started: account=REDACTED_CTRADER_ACCOUNT symbols=3
Jul 05 16:57:23 ayumi.forward_test:           Preloaded 200 bars for USDJPY 60m
Jul 05 16:57:26 ayumi.forward_test:           Bar preloading complete
Jul 05 16:57:27 ayumi.forward_test:           Preflight: seeded 0 open cTrader positions totaling $0.00 risk
Jul 05 16:57:28 ayumi.forward_test:           [Balance Sync] RiskGuard synced: live=$9324.58 starting=$10000.00 dd=6.75%
Jul 05 16:57:28 ayumi.forward_test:           Forward test started: symbols=['GBPUSD','USDJPY','EURUSD'] strategies=10 mode=LIVE
```

Forward test fully operational at first attempt. No restart events in first 30 s
of operation — exit code `0/SUCCESS` from the prior June 30 run shows the
unit previously ran cleanly for **7 h 21 min** before the operator stopped it.

---

## 10. Canonical unit file template (copy-paste ready)

For operators who need to recreate the unit file from scratch (e.g., new
machine, fresh `~/.config/systemd/user/`):

```ini
[Unit]
Description=Ayumi Forward Test (live multi-strategy paper trading)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$AYUMI_ROOT
ExecStart=$AYUMI_ROOT/.venv/bin/python scripts/launch_blend_forward_test.py --live
Restart=on-failure
RestartSec=30
StartLimitBurst=5
StartLimitIntervalSec=300
RestartPreventExitStatus=75
SuccessExitStatus=75
TimeoutStopSec=10
KillMode=control-group
MemoryMax=512M
MemoryHigh=384M
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

---

## 11. References

- [`docs/post-mortems/ayumi-may-june-crash-root-cause-2026-07-05.md`](../post-mortems/ayumi-may-june-crash-root-cause-2026-07-05.md) — B6 root-cause analysis (the source of truth for §5.2 unit design)
- [`scripts/launch_blend_forward_test.py`](../../scripts/launch_blend_forward_test.py) — the launcher script; do NOT modify
- [`src/forex_bot/adapters/ctrader/connection_watchdog.py`](../../src/forex_bot/adapters/ctrader/connection_watchdog.py) — in-app liveness check (30 s / 90 s thresholds)
- [`docs/post-mortems/ctrader-openapi-connection-2026-06-11.md`](../post-mortems/ctrader-openapi-connection-2026-06-11.md) — ProtoMessage double-wrap fix (`7af7a73`)
- [`docs/plans/ayumi-reliability-sprint-2026-07-05.md`](../plans/ayumi-reliability-sprint-2026-07-05.md) — sprint plan this card belongs to