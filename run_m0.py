"""FORGE M0 runner.

  python run_m0.py                                   # read-only probes only
  python run_m0.py --run-experiments                 # probes + all experiments
  python run_m0.py --run-experiments --only memory_limit cpu_time_limit

Writes a JSON evidence file (default m0_results.json) and prints a short summary.
The experiments start child Python processes that allocate at most 512 MB; the
Job Object tests refuse to run if available RAM is under 2 GB. A full run takes
roughly 3 minutes with the default trial counts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import limits_lab
import probes
from winapi import IS_WINDOWS

VERSION = "forge-m0-0.3"
EXPERIMENTS = ("polling_miss", "memory_limit", "cpu_time_limit", "supervisor_cpu_limit",
               "cpu_rate_cap", "poll_overhead")


def check_power_requirement(power_probe: dict, required):
    """Return an error string if the machine is not in the required power state, else None."""
    if required is None:
        return None
    try:
        raw = power_probe["value"]["system_power_status"]["ac_line_raw"]
    except (KeyError, TypeError):
        return "power state unavailable, so --require-power %s cannot be verified" % required
    state = {1: "ac", 0: "battery"}.get(raw, "unknown")
    if state != required:
        return "power state is '%s' but --require-power %s was requested" % (state, required)
    return None


def _guarded(fn) -> dict:
    try:
        return fn()
    except Exception as exc:
        return {"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="m0_results.json")
    ap.add_argument("--run-experiments", action="store_true",
                    help="run the polling-miss test and (on Windows) the Job Object limit tests")
    ap.add_argument("--only", nargs="+", choices=EXPERIMENTS, default=list(EXPERIMENTS),
                    help="run only these experiments (implies nothing unless --run-experiments)")
    ap.add_argument("--require-power", choices=["ac", "battery"], default=None,
                    help="abort before running anything unless the machine is in this power state")
    ap.add_argument("--trials", type=int, default=3, help="trials per cell for polling and cap tests")
    ap.add_argument("--spike-mb", type=int, default=200, help="size of the memory spike (max 512)")
    ap.add_argument("--holds-ms", type=float, nargs="+", default=[5, 20, 100, 500])
    ap.add_argument("--intervals-ms", type=float, nargs="+", default=[10, 50, 250])
    args = ap.parse_args(argv)
    if not 1 <= args.spike_mb <= 512:
        ap.error("--spike-mb must be between 1 and 512")
    if not 1 <= args.trials <= 10:
        ap.error("--trials must be between 1 and 10")

    # Create the output folder BEFORE running anything, so a bad path can never
    # throw away the results of a long experiment run.
    out_dir = os.path.dirname(os.path.abspath(args.out))
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(args.out, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        ap.error("cannot write to %s: %s" % (args.out, exc))

    results = {
        "meta": {
            "version": VERSION,
            "utc": datetime.now(timezone.utc).isoformat(),
            "argv": sys.argv,
            "is_windows": IS_WINDOWS,
            "experiments_requested": args.run_experiments,
            "experiments_selected": args.only if args.run_experiments else [],
            "trials": args.trials,
        },
        "probes": probes.collect_probes(),
        "experiments": {},
    }
    problem = check_power_requirement(results["probes"]["power"], args.require_power)
    if problem:
        print("ABORTED:", problem)
        print("Nothing was run. Fix the power state and run again.")
        return 3

    if args.run_experiments:
        runners = {
            "polling_miss": lambda: limits_lab.polling_miss(
                args.spike_mb, args.holds_ms, args.intervals_ms, trials=args.trials),
            "memory_limit": limits_lab.memory_limit_test,
            "cpu_time_limit": limits_lab.cpu_time_limit_test,
            "supervisor_cpu_limit": limits_lab.supervisor_cpu_limit_test,
            "cpu_rate_cap": lambda: limits_lab.cpu_rate_cap_test(trials=args.trials),
            "poll_overhead": limits_lab.poll_overhead_test,
        }
        for name in EXPERIMENTS:
            if name in args.only:
                print("running", name, "...", flush=True)
                results["experiments"][name] = _guarded(runners[name])

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)

    print("FORGE M0 results written to", args.out)
    pw = results["probes"]["power"].get("value", {}).get("system_power_status")
    if pw:
        print("  power state at start: %s, battery %s%%" % (pw["ac_line_status"], pw["battery_percent"]))
    for name, r in results["probes"].items():
        print("  probe %-22s %s" % (name, r.get("status")), ("- " + r["error"]) if "error" in r else "")
    for name, r in results["experiments"].items():
        print("  experiment %-20s %s  %s" % (name, r.get("status"), r.get("outcome", "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
