"""FORGE M0 experiments. Run only through run_m0.py --run-experiments.

Safety rules baked into this module:
  * children never allocate more than m0_child.HARD_CAP_MB (512 MB);
  * Job Object tests refuse to run if available RAM is below MIN_AVAIL_MB;
  * every Job uses KILL_ON_JOB_CLOSE and is terminated in a finally block;
  * every wait has a timeout.

polling_miss() is portable (it has a /proc reader) so the harness logic can be
self-tested off Windows. A non-Windows result is NOT evidence about Windows.

Revision 2 changes (after the first Windows run):
  * LineReader.lines() drains the queue; v1 read the history before draining and
    reported INCONCLUSIVE for the memory-limit test;
  * cpu_time_limit_test tries three variants and reads the limits back;
  * supervisor_cpu_limit_test: FORGE-side enforcement by polling job accounting;
  * cpu_rate_cap_test and polling_miss run multiple trials, and the cap test
    reports 100 ms and 500 ms windows (accounting ticks are 15.625 ms, so 100 ms
    windows are noisy).
"""
from __future__ import annotations

import queue
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

import winapi
from winapi import IS_WINDOWS

MB = 1024 * 1024
CHILD = str(Path(__file__).with_name("m0_child.py"))
MIN_AVAIL_MB = 2048


# ---------------------------------------------------------------- plumbing
class LineReader(threading.Thread):
    """Reads child stdout on a thread so the parent can poll memory meanwhile."""

    def __init__(self, stream) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self.history: list = []
        self.closed = False
        self.start()

    def run(self) -> None:
        try:
            for line in self._stream:
                self._q.put(line.rstrip("\r\n"))
        except (ValueError, OSError):
            pass  # stream closed during cleanup
        finally:
            self._q.put(None)

    def _pump(self, timeout: float) -> bool:
        try:
            item = self._q.get(timeout=timeout) if timeout > 0 else self._q.get_nowait()
        except queue.Empty:
            return False
        if item is None:
            self.closed = True
        else:
            self.history.append(item)
        return True

    def lines(self) -> list:
        """All lines received so far (drains the queue first)."""
        while self._pump(0):
            pass
        return list(self.history)

    def find(self, prefix: str) -> Optional[str]:
        for line in reversed(self.lines()):
            if line.startswith(prefix):
                return line
        return None

    def wait_for(self, prefix: str, timeout: float) -> Optional[str]:
        end = time.monotonic() + timeout
        while True:
            hit = self.find(prefix)
            if hit is not None:
                return hit
            if self.closed:
                return None
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            self._pump(min(0.05, remaining))


def _spawn(*args) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-u", CHILD, *[str(a) for a in args]],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)


def _send(p: subprocess.Popen, text: str) -> None:
    try:
        p.stdin.write(text + "\n")
        p.stdin.flush()
    except (BrokenPipeError, OSError):
        pass


def _reap(p: subprocess.Popen) -> None:
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    finally:
        for stream in (p.stdin, p.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def window_stats(samples: Sequence, step: int) -> Optional[dict]:
    """Core-equivalents over windows of `step` samples.

    samples: list of (time_s, cumulative_cpu_s), first entry is the start (t0, 0.0).
    """
    vals = []
    for i in range(step, len(samples), step):
        t0, c0 = samples[i - step]
        t1, c1 = samples[i]
        if t1 > t0:
            vals.append((c1 - c0) / (t1 - t0))
    if not vals:
        return None
    return {"n": len(vals), "min": min(vals), "median": statistics.median(vals), "max": max(vals)}


class MemoryReader:
    """Reads (current, peak) bytes for another process.

    Windows: working set and kernel-maintained PeakWorkingSetSize.
    POSIX (self-test only): VmRSS and VmHWM from /proc/<pid>/status.
    """

    def __init__(self) -> None:
        self.method = ("GetProcessMemoryInfo(WorkingSetSize, PeakWorkingSetSize)" if IS_WINDOWS
                       else "/proc/<pid>/status (VmRSS, VmHWM) - POSIX self-test")

    def read(self, pid: int) -> dict:
        if IS_WINDOWS:
            m = winapi.process_memory(pid)
            return {"current": m["working_set"], "peak": m["peak_working_set"]}
        vals = {}
        for line in Path("/proc/%d/status" % pid).read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                k, v = line.split(":", 1)
                vals[k] = int(v.split()[0]) * 1024
        return {"current": vals["VmRSS"], "peak": vals["VmHWM"]}


# ------------------------------------------------- experiment 1: polling miss
def _poll_run(reader: MemoryReader, spike_mb: int, hold_ms: float, interval_ms: float) -> dict:
    p = _spawn("spike", spike_mb, hold_ms)
    lr = LineReader(p.stdout)
    try:
        if lr.wait_for("ready", 30) is None:
            raise RuntimeError("child not ready: %r" % lr.lines())
        base = reader.read(p.pid)["current"]
        _send(p, "go")
        polled_max, stamps = base, []
        end = time.perf_counter() + 60
        while time.perf_counter() < end:
            t = time.perf_counter()
            cur = reader.read(p.pid)["current"]
            stamps.append(t)
            polled_max = max(polled_max, cur)
            if lr.find("spiked") is not None:
                break
            time.sleep(interval_ms / 1000.0)
        spiked = lr.wait_for("spiked", 10)
        peak = reader.read(p.pid)["peak"]  # kernel-maintained, read AFTER the spike
        _send(p, "exit")
    finally:
        _reap(p)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    alloc_s = total_s = None
    if spiked:
        parts = spiked.split()
        alloc_s, total_s = float(parts[1]), float(parts[2])
    k_delta = (peak - base) / MB
    p_delta = (polled_max - base) / MB
    return {
        "spike_mb": spike_mb,
        "hold_ms": hold_ms,
        "interval_ms": interval_ms,
        "polls": len(stamps),
        "actual_gap_ms_mean": statistics.mean(gaps) * 1000 if gaps else None,
        "actual_gap_ms_max": max(gaps) * 1000 if gaps else None,
        "spike_alloc_s": alloc_s,
        "spike_total_s": total_s,
        "baseline_bytes": base,
        "polled_max_bytes": polled_max,
        "kernel_peak_bytes": peak,
        "polled_delta_mb": p_delta,
        "kernel_delta_mb": k_delta,
        "captured_fraction": (p_delta / k_delta) if k_delta > 0 else None,
    }


def polling_miss(spike_mb: int, holds_ms: Sequence[float], intervals_ms: Sequence[float],
                 trials: int = 3) -> dict:
    """Does polling see a short memory spike that the kernel's peak counter recorded?"""
    reader = MemoryReader()
    rows, summary = [], []
    for h in holds_ms:
        for i in intervals_ms:
            caps = []
            for t in range(trials):
                r = _poll_run(reader, spike_mb, h, i)
                r["trial"] = t
                rows.append(r)
                if r["captured_fraction"] is not None:
                    caps.append(r["captured_fraction"])
            summary.append({
                "hold_ms": h, "interval_ms": i, "trials": len(caps),
                "captured_min": min(caps) if caps else None,
                "captured_mean": statistics.mean(caps) if caps else None,
                "captured_max": max(caps) if caps else None,
                "full_captures": sum(1 for c in caps if c >= 0.99),
                "near_total_misses": sum(1 for c in caps if c < 0.05),
            })
    return {"status": "ok", "reader_method": reader.method, "is_windows_evidence": IS_WINDOWS,
            "trials_per_cell": trials, "summary": summary, "rows": rows}


# ------------------------------------------------ Windows-only limit experiments
def _precheck(name: str) -> Optional[dict]:
    if not IS_WINDOWS:
        return {"status": "skipped", "note": name + " requires Windows Job Objects"}
    avail = winapi.global_memory_status()["avail_phys"] / MB
    if avail < MIN_AVAIL_MB:
        return {"status": "skipped",
                "note": "available RAM %.0f MB is below the %d MB safety floor" % (avail, MIN_AVAIL_MB)}
    return None


def memory_limit_test(limit_mb: int = 256, try_mb: int = 512, step_mb: int = 16) -> dict:
    """Does a Job Object per-process memory limit stop a child, and how does it fail?"""
    skip = _precheck("memory_limit_test")
    if skip:
        return skip
    if not (0 < limit_mb < try_mb <= 512):
        raise ValueError("require 0 < limit_mb < try_mb <= 512")
    out: dict = {"status": "ok", "limit_mb": limit_mb, "try_mb": try_mb, "step_mb": step_mb}
    with winapi.Job() as job:
        job.set_process_memory_limit(limit_mb * MB)
        p = _spawn("memlimit", try_mb, step_mb)
        lr = LineReader(p.stdout)
        try:
            if lr.wait_for("ready", 30) is None:
                raise RuntimeError("child not ready: %r" % lr.lines())
            job.assign(p.pid)
            _send(p, "go")
            t0 = time.perf_counter()
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                out["timeout"] = True
                job.terminate()
            out["wall_s"] = time.perf_counter() - t0
            lr.join(timeout=5)
            out["exit_code"] = p.returncode
            out["child_lines"] = lr.lines()  # drained: v1 read an undrained history
            out["job_accounting"] = job.accounting()
            out["job_extended"] = job.extended()
        finally:
            _reap(p)
    lines = out.get("child_lines", [])
    if any(s.startswith("completed") for s in lines):
        outcome = "NOT_ENFORCED (child completed the full allocation)"
    elif any(s.startswith("memoryerror") for s in lines):
        outcome = "ENFORCED_AS_ALLOCATION_FAILURE (MemoryError inside the child)"
    elif out.get("exit_code") not in (0, None):
        outcome = "STOPPED_BY_OS_OR_CRASH (cause not distinguished; see exit code and accounting)"
    else:
        outcome = "INCONCLUSIVE"
    out["outcome"] = outcome
    return out


def _time_limit_variant(mode: str, limit_s: float, run_s: float) -> dict:
    """mode: 'before' (per-process limit set before assign), 'after' (set after assign),
    'job' (job-wide limit set before assign), 'job_after' (job-wide limit set after assign)."""
    out: dict = {"mode": mode, "limit_cpu_s": limit_s, "child_wall_budget_s": run_s}
    with winapi.Job() as job:
        if mode == "before":
            job.set_process_time_limit(limit_s)
        elif mode == "job":
            job.set_job_time_limit(limit_s)
        p = _spawn("spin", run_s)
        lr = LineReader(p.stdout)
        try:
            if lr.wait_for("ready", 30) is None:
                raise RuntimeError("child not ready")
            job.assign(p.pid)
            if mode == "after":
                job.set_process_time_limit(limit_s)
            elif mode == "job_after":
                job.set_job_time_limit(limit_s)
            out["limits_readback"] = job.extended()  # proves the limit was stored
            _send(p, "go")
            t0 = time.perf_counter()
            try:
                p.wait(timeout=run_s + 20)
            except subprocess.TimeoutExpired:
                out["timeout"] = True
                job.terminate()
            out["wall_s"] = time.perf_counter() - t0
            lr.join(timeout=5)
            out["exit_code"] = p.returncode
            out["child_finished_normally"] = lr.find("done") is not None
            acct = job.accounting()
            out["job_accounting"] = acct
            out["cpu_s_consumed"] = acct["user_s"] + acct["kernel_s"]
        finally:
            _reap(p)
    out["outcome"] = ("ENFORCED (stopped before finishing its spin)"
                      if not out["child_finished_normally"] else "NOT_ENFORCED (child ran to completion)")
    return out


def cpu_time_limit_test(limit_s: float = 1.0, run_s: float = 4.0, after_repeats: int = 5) -> dict:
    """Does a platform CPU-time limit stop a spinning child, and does ordering matter?

    Round 2 showed the per-process limit fired only when set AFTER assigning the process.
    This round repeats that variant to measure how far past the limit the child runs.
    """
    skip = _precheck("cpu_time_limit_test")
    if skip:
        return skip
    plan = [("per_process_limit_set_before_assign", "before"),
            ("job_wide_limit_set_before_assign", "job"),
            ("job_wide_limit_set_after_assign", "job_after")]
    plan += [("per_process_limit_set_after_assign_trial_%d" % k, "after") for k in range(after_repeats)]
    out: dict = {"status": "ok", "variants": {}}
    for name, mode in plan:
        try:
            out["variants"][name] = _time_limit_variant(mode, limit_s, run_s)
        except Exception as exc:
            out["variants"][name] = {"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
    after = [v for k, v in out["variants"].items() if "after_assign_trial" in k and "per_process" in k]
    stopped = [v["cpu_s_consumed"] for v in after if str(v.get("outcome", "")).startswith("ENFORCED")]
    out["per_process_after_assign_summary"] = {
        "trials": len(after),
        "enforced": len(stopped),
        "cpu_s_when_stopped": stopped,
        "limit_s": limit_s,
        "overshoot_s_min": (min(stopped) - limit_s) if stopped else None,
        "overshoot_s_max": (max(stopped) - limit_s) if stopped else None,
    }
    parts = []
    for name in ("per_process_limit_set_before_assign", "job_wide_limit_set_before_assign",
                 "job_wide_limit_set_after_assign"):
        v = out["variants"][name]
        parts.append("%s: %s" % (name, v.get("outcome", v.get("status"))))
    parts.append("per_process_after_assign: %d of %d enforced" % (len(stopped), len(after)))
    out["outcome"] = "; ".join(parts)
    return out


def supervisor_cpu_limit_test(limit_s: float = 1.0, run_s: float = 4.0, poll_ms: int = 20) -> dict:
    """FORGE-side enforcement: poll job CPU accounting and terminate the job at the limit.

    This is NOT platform enforcement; it is bounded by polling interval and by the
    15.625 ms accounting tick. It measures how far past the limit the child gets.
    """
    skip = _precheck("supervisor_cpu_limit_test")
    if skip:
        return skip
    out: dict = {"status": "ok", "limit_cpu_s": limit_s, "poll_ms": poll_ms}
    with winapi.Job() as job:
        p = _spawn("spin", run_s)
        lr = LineReader(p.stdout)
        try:
            if lr.wait_for("ready", 30) is None:
                raise RuntimeError("child not ready")
            job.assign(p.pid)
            _send(p, "go")
            t0 = time.perf_counter()
            polls, stopped = 0, False
            while p.poll() is None and time.perf_counter() - t0 < run_s + 20:
                a = job.accounting()
                polls += 1
                if a["user_s"] + a["kernel_s"] >= limit_s:
                    job.terminate(1)
                    stopped = True
                    break
                time.sleep(poll_ms / 1000.0)
            out["wall_s_at_stop"] = time.perf_counter() - t0
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                job.terminate(1)
            lr.join(timeout=5)
            a = job.accounting()
            out.update({"polls": polls, "stopped_by_supervisor": stopped, "exit_code": p.returncode,
                        "child_finished_normally": lr.find("done") is not None,
                        "cpu_s_final": a["user_s"] + a["kernel_s"],
                        "overshoot_s": a["user_s"] + a["kernel_s"] - limit_s,
                        "job_accounting": a})
        finally:
            _reap(p)
    out["outcome"] = ("SUPERVISOR_STOPPED (overshoot_s shows the cost of polling and tick granularity)"
                      if out["stopped_by_supervisor"] and not out["child_finished_normally"]
                      else "NOT_STOPPED")
    return out


def _spin_under_job(job, run_s: float, poll_ms: int = 100) -> dict:
    p = _spawn("spin", run_s)
    lr = LineReader(p.stdout)
    try:
        if lr.wait_for("ready", 30) is None:
            raise RuntimeError("child not ready")
        job.assign(p.pid)
        _send(p, "go")
        t0 = time.perf_counter()
        samples = [(t0, 0.0)]
        while p.poll() is None and time.perf_counter() - t0 < run_s + 20:
            time.sleep(poll_ms / 1000.0)
            a = job.accounting()
            samples.append((time.perf_counter(), a["user_s"] + a["kernel_s"]))
        wall = time.perf_counter() - t0
        a = job.accounting()
    finally:
        _reap(p)
    cpu = a["user_s"] + a["kernel_s"]
    step500 = max(1, int(round(500 / poll_ms)))
    return {
        "cpu_seconds": cpu,
        "wall_s": wall,
        "core_equivalents_overall": cpu / wall if wall > 0 else None,
        "poll_ms": poll_ms,
        "windows_100ms": window_stats(samples, 1),
        "windows_500ms": window_stats(samples, step500),
        "note": "wall includes up to one poll interval after exit; window stats are noisy at 100 ms "
                "because accounting advances in 15.625 ms ticks",
    }


def cpu_rate_cap_test(targets: Sequence[float] = (0.25, 0.5), trials: int = 3,
                      run_s: float = 5.0, poll_ms: int = 100) -> dict:
    """Does a Job Object CPU hard cap hold average CPU use near a target?

    The child is single-threaded, so only targets below 1.0 core-equivalent are testable.
    """
    skip = _precheck("cpu_rate_cap_test")
    if skip:
        return skip
    logical = winapi.logical_processors()["active_logical"]
    out: dict = {"status": "ok", "logical_cpus": logical, "run_s": run_s, "trials": trials}
    with winapi.Job() as job:
        out["control"] = _spin_under_job(job, run_s, poll_ms)
    out["targets"] = {}
    all_ok = True
    for target in targets:
        rows = []
        for _ in range(trials):
            try:
                with winapi.Job() as job:
                    rate = job.set_cpu_rate_hard_cap(target, logical)
                    r = _spin_under_job(job, run_s, poll_ms)
                    r["cpu_rate_value_set"] = rate
            except OSError as exc:
                r = {"status": "error", "error": "CPU rate control rejected: %r" % exc}
            rows.append(r)
        vals = [r["core_equivalents_overall"] for r in rows if r.get("core_equivalents_overall") is not None]
        mean = statistics.mean(vals) if vals else None
        out["targets"][str(target)] = {
            "trials": rows,
            "mean_core_equivalents": mean,
            "deviation_pct_from_target": (mean / target - 1) * 100 if mean is not None else None,
        }
        if mean is None or abs(mean / target - 1) > 0.25:
            all_ok = False
    out["outcome"] = ("CAP_HELD_ON_AVERAGE_WITHIN_25PCT (single-thread child; short-window behavior not established)"
                      if all_ok else "CAP_NOT_WITHIN_25PCT_OR_ERROR")
    return out


def poll_overhead_test(intervals_ms: Sequence[float] = (10, 50, 250), duration_s: float = 8.0) -> dict:
    """CPU cost of polling another process's memory with this implementation.

    Each read opens the process, reads counters and closes the handle. On Windows the
    parent's CPU time is quantized to 15.625 ms ticks, so the resolution of the
    core-equivalent figure is about 0.016 / duration_s.
    """
    reader = MemoryReader()
    rows = []
    for iv in intervals_ms:
        p = _spawn("spin", duration_s + 5)
        lr = LineReader(p.stdout)
        try:
            if lr.wait_for("ready", 30) is None:
                raise RuntimeError("child not ready")
            _send(p, "go")
            reads = 0
            c0, t0 = time.process_time(), time.perf_counter()
            while time.perf_counter() - t0 < duration_s:
                reader.read(p.pid)
                reads += 1
                time.sleep(iv / 1000.0)
            c1, t1 = time.process_time(), time.perf_counter()
            p.kill()
        finally:
            _reap(p)
        cpu, wall = c1 - c0, t1 - t0
        rows.append({
            "interval_ms": iv,
            "reads": reads,
            "parent_cpu_s": cpu,
            "wall_s": wall,
            "core_equivalents": cpu / wall,
            "cpu_us_per_read": cpu / reads * 1e6 if reads else None,
            "resolution_core_eq": 0.015625 / wall if IS_WINDOWS else None,
        })
    return {"status": "ok", "reader_method": reader.method, "is_windows_evidence": IS_WINDOWS,
            "duration_s": duration_s, "rows": rows,
            "outcome": "MEASURED (values below the stated resolution are indistinguishable from zero)"}
