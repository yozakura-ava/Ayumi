#!/usr/bin/env python3
"""Kill Switch CLI — command-line interface for the kill switch system.

Usage:
    python scripts/kill_switch.py status
    python scripts/kill_switch.py kill --reason "manual intervention"
    python scripts/kill_switch.py freeze --reason "monitoring spread"
    python scripts/kill_switch.py recover
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from adapters.ctrader.kill_switch import KillSwitchManager


def cmd_status(ksm: KillSwitchManager, args):
    """Print current kill switch status."""
    status = ksm.get_status()
    if not status["active"]:
        print("✅ Kill switch: INACTIVE (trading enabled)")
        return 0

    mode = status.get("mode", "unknown").upper()
    reason = status.get("reason", "unknown")
    triggered_by = status.get("triggered_by", "unknown")
    triggered_at = status.get("triggered_at", "unknown")

    if mode == "KILL":
        print("🔴 Kill switch: ACTIVE (KILL)")
    else:
        print("🟡 Kill switch: ACTIVE (FREEZE)")

    print(f"   Reason:        {reason}")
    print(f"   Triggered by:  {triggered_by}")
    print(f"   Triggered at:  {triggered_at}")
    print(f"   Level:         {status.get('level', 'global')}")
    print(f"   Positions closed: {status.get('positions_closed', False)} (count: {status.get('close_count', 0)})")

    if status.get("metadata"):
        print(f"   Metadata:      {json.dumps(status['metadata'])}")

    return 0


def cmd_kill(ksm: KillSwitchManager, args):
    """Activate global kill."""
    reason = args.reason or "manual_cli"
    triggered_by = args.triggered_by or "cli"

    if ksm.is_globally_killed():
        print("⚠️  Kill switch already ACTIVE (KILL) — re-activating with new reason")
    elif ksm.is_globally_frozen():
        print("⚠️  Kill switch was ACTIVE (FREEZE) — escalating to KILL")

    ksm.activate_global_kill(
        reason=reason,
        triggered_by=triggered_by,
        close_positions=not args.no_close_positions,
    )
    print(f"🔴 GLOBAL KILL activated: reason='{reason}', by='{triggered_by}'")
    return 0


def cmd_freeze(ksm: KillSwitchManager, args):
    """Activate global freeze."""
    reason = args.reason or "manual_cli"
    triggered_by = args.triggered_by or "cli"

    if ksm.is_globally_killed():
        print("⚠️  Kill switch was ACTIVE (KILL) — downgrading to FREEZE")
    elif ksm.is_globally_frozen():
        print("⚠️  Kill switch already ACTIVE (FREEZE) — re-activating with new reason")

    ksm.activate_global_freeze(
        reason=reason,
        triggered_by=triggered_by,
    )
    print(f"🟡 GLOBAL FREEZE activated: reason='{reason}', by='{triggered_by}'")
    return 0


def cmd_recover(ksm: KillSwitchManager, args):
    """Deactivate kill switch (manual recovery)."""
    if not ksm.is_active():
        print("✅ Kill switch is not active — nothing to recover from")
        return 0

    status = ksm.get_status()
    mode = status.get("mode", "unknown").upper()
    reason = status.get("reason", "unknown")

    print(f"Recovering from {mode} (reason was: '{reason}')...")

    ksm.deactivate(reason=args.reason or "manual_recovery")
    print("✅ Kill switch DEACTIVATED — trading enabled")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Ayumi Kill Switch CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--state-dir",
        default="data/kill_switches",
        help="State directory (default: data/kill_switches)",
    )

    subparsers = parser.add_subparsers(dest="command")

    # status
    sp_status = subparsers.add_parser("status", help="Show current kill switch status")
    sp_status.set_defaults(func=cmd_status)

    # kill
    sp_kill = subparsers.add_parser("kill", help="Activate global KILL (stop trading, close positions)")
    sp_kill.add_argument("--reason", default="manual_cli", help="Reason for the kill")
    sp_kill.add_argument("--triggered-by", default="cli", help="Who triggered the kill")
    sp_kill.add_argument(
        "--no-close-positions",
        action="store_true",
        help="Don't close existing positions (just block new trades)",
    )
    sp_kill.set_defaults(func=cmd_kill)

    # freeze
    sp_freeze = subparsers.add_parser("freeze", help="Activate global FREEZE (block new trades, hold positions)")
    sp_freeze.add_argument("--reason", default="manual_cli", help="Reason for the freeze")
    sp_freeze.add_argument("--triggered-by", default="cli", help="Who triggered the freeze")
    sp_freeze.set_defaults(func=cmd_freeze)

    # recover
    sp_recover = subparsers.add_parser("recover", help="Deactivate kill switch")
    sp_recover.add_argument("--reason", default="manual_recovery", help="Reason for recovery")
    sp_recover.set_defaults(func=cmd_recover)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Use project-relative state dir if not absolute
    state_dir = args.state_dir
    if not Path(state_dir).is_absolute():
        state_dir = str(PROJECT_ROOT / state_dir)

    ksm = KillSwitchManager(state_dir=state_dir)
    return args.func(ksm, args)


if __name__ == "__main__":
    sys.exit(main())
