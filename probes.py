"""FORGE M0 probes: read-only detection and measurement checks.

Every probe returns a dict with a 'status' ('ok' | 'unavailable' | 'error'),
optional 'value', 'method', 'note', 'error'. Nothing here changes system state.
POSIX branches exist only so the harness can be self-tested off Windows; they are
not part of the Windows feasibility evidence.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import winapi
from winapi import IS_WINDOWS


# --------------------------------------------------------------- pure helpers
def cpu_quantities(cpu_seconds: float, wall_seconds: float, logical_cpus: int) -> dict:
    """Three DIFFERENT CPU quantities. They are never interchangeable.

    cpu_seconds       - total processor time consumed (an amount of work/time).
    core_equivalents  - cpu_seconds / wall_seconds: average number of cores kept busy.
    normalized_pct    - core_equivalents / logical_cpus * 100: share of total machine
                        capacity. With SMT, a logical CPU is not a full core, so 100%
                        does not mean twice the work of 50%.
    """
    if wall_seconds <= 0 or logical_cpus <= 0:
        raise ValueError("wall_seconds and logical_cpus must be positive")
    core_eq = cpu_seconds / wall_seconds
    return {
        "cpu_seconds": cpu_seconds,
        "wall_seconds": wall_seconds,
        "core_equivalents": core_eq,
        "normalized_pct": core_eq / logical_cpus * 100.0,
        "logical_cpus": logical_cpus,
    }


def parse_powercfg_guid(text: str) -> Optional[str]:
    """Extract the scheme GUID from `powercfg /getactivescheme` output (text is localized)."""
    m = re.search(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", text)
    return m.group(1).lower() if m else None


def run_probe(fn: Callable[[], dict]) -> dict:
    try:
        return fn()
    except winapi.WindowsOnlyError as exc:
        return {"status": "unavailable", "note": str(exc)}
    except Exception as exc:  # a probe must never abort the whole run
        return {"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}


def _ok(value, method: str, note: Optional[str] = None) -> dict:
    out = {"status": "ok", "value": value, "method": method}
    if note:
        out["note"] = note
    return out


def logical_cpu_count() -> int:
    if IS_WINDOWS:
        try:
            return winapi.logical_processors()["active_logical"]
        except Exception:
            pass
    return os.cpu_count() or 1


# ---------------------------------------------------------------------- probes
def probe_environment() -> dict:
    value = {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "pointer_bits": 64 if sys.maxsize > 2**32 else 32,
        "cwd": str(Path.cwd()),
    }
    if IS_WINDOWS:
        try:
            value["python_process_in_job"] = winapi.is_current_process_in_job()
        except Exception as exc:
            value["python_process_in_job"] = "error: %r" % exc
    return _ok(value, "platform/sys; IsProcessInJob on Windows",
               "If the interpreter already runs inside a job, job-assignment tests may fail.")


def _process_time_granularity(max_s: float = 0.5, want: int = 20) -> dict:
    deltas, last = [], time.process_time()
    end = time.perf_counter() + max_s
    while time.perf_counter() < end and len(deltas) < want:
        now = time.process_time()
        if now != last:
            deltas.append(now - last)
            last = now
    if not deltas:
        return {"observed_changes": 0}
    return {"observed_changes": len(deltas), "min_s": min(deltas),
            "median_s": statistics.median(deltas), "max_s": max(deltas)}


def probe_clocks() -> dict:
    infos = {}
    for name in ("perf_counter", "process_time", "monotonic", "time"):
        i = time.get_clock_info(name)
        infos[name] = {"implementation": i.implementation, "resolution_s": i.resolution,
                       "monotonic": i.monotonic, "adjustable": i.adjustable}
    infos["process_time_observed_granularity"] = _process_time_granularity()
    return _ok(infos, "time.get_clock_info + empirical process_time step sizes",
               "Reported resolution is nominal; the observed step size is what limits CPU-seconds accuracy.")


def probe_cpu_topology() -> dict:
    value = {"os_cpu_count": os.cpu_count(), "platform_processor": platform.processor()}
    if IS_WINDOWS:
        value.update(winapi.logical_processors())
        try:
            import winreg  # stdlib, Windows only

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            try:
                value["registry_name"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
                value["registry_mhz_nominal"] = winreg.QueryValueEx(key, "~MHz")[0]
            finally:
                winreg.CloseKey(key)
        except Exception as exc:
            value["registry_error"] = repr(exc)
        method = "GetActiveProcessorCount, GetLogicalProcessorInformationEx, winreg"
    else:
        method = "os.cpu_count (POSIX self-test path)"
    return _ok(value, method,
               "Registry MHz is nominal, not current frequency. Hybrid/efficiency-core data is not collected.")


def probe_system_memory() -> dict:
    if IS_WINDOWS:
        return _ok(winapi.global_memory_status(), "GlobalMemoryStatusEx",
                   "Page-file fields are kept raw: their system-vs-process semantics must be verified.")
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(("MemTotal:", "MemAvailable:")):
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
    return _ok(info, "/proc/meminfo (POSIX self-test path)")


def probe_self_memory() -> dict:
    if IS_WINDOWS:
        return _ok(winapi.process_memory(), "GetProcessMemoryInfo (current process)",
                   "Working set and commit are different quantities and both are recorded.")
    info = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
    return _ok(info, "/proc/self/status (POSIX self-test path)")


def probe_power() -> dict:
    if not IS_WINDOWS:
        return {"status": "unavailable", "note": "power probes are Windows-only in M0"}
    value = {"system_power_status": winapi.power_status()}
    try:
        value["overlay"] = winapi.power_overlay()
    except Exception as exc:
        value["overlay"] = {"available": False, "error": repr(exc)}
    try:
        cp = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True, timeout=10)
        value["powercfg_active_scheme"] = {
            "returncode": cp.returncode,
            "raw_output": cp.stdout.strip(),
            "guid": parse_powercfg_guid(cp.stdout),
        }
    except Exception as exc:
        value["powercfg_active_scheme"] = {"error": repr(exc)}
    return _ok(value, "GetSystemPowerStatus, PowerGetEffectiveOverlayScheme, powercfg (external tool)",
               "Gives AC/battery state and power mode as CONTEXT. None of this measures energy.")


def probe_storage() -> dict:
    anchor = Path.cwd().anchor or "."
    u = shutil.disk_usage(anchor)
    return _ok({"path": anchor, "total": u.total, "used": u.used, "free": u.free}, "shutil.disk_usage",
               "Capacity only. Throughput/latency are not measured in M0.")


def probe_cpu_quantities_demo(spin_s: float = 0.4) -> dict:
    """Burn ~spin_s of single-thread CPU and report all three CPU quantities side by side."""
    logical = logical_cpu_count()
    sys0 = winapi.system_times() if IS_WINDOWS else None
    p0 = winapi.process_cpu_seconds() if IS_WINDOWS else None
    c0, w0 = time.process_time(), time.perf_counter()
    end = w0 + spin_s
    while time.perf_counter() < end:
        pass
    c1, w1 = time.process_time(), time.perf_counter()
    value = {"stdlib_process_time": cpu_quantities(c1 - c0, w1 - w0, logical)}
    if IS_WINDOWS:
        p1 = winapi.process_cpu_seconds()
        sys1 = winapi.system_times()
        api_cpu = (p1["user_s"] + p1["kernel_s"]) - (p0["user_s"] + p0["kernel_s"])
        value["getprocesstimes"] = cpu_quantities(api_cpu, w1 - w0, logical)
        total = sys1["kernel_incl_idle_s"] - sys0["kernel_incl_idle_s"] + sys1["user_s"] - sys0["user_s"]
        idle = sys1["idle_s"] - sys0["idle_s"]
        if total > 0:
            value["system_wide_busy_pct"] = (total - idle) / total * 100.0
    return _ok(value, "time.process_time; GetProcessTimes; GetSystemTimes",
               "A single-threaded spin should show about 1 core-equivalent regardless of logical CPU count.")


def collect_probes() -> dict:
    fns = {
        "environment": probe_environment,
        "clocks": probe_clocks,
        "cpu_topology": probe_cpu_topology,
        "system_memory": probe_system_memory,
        "self_memory": probe_self_memory,
        "power": probe_power,
        "storage": probe_storage,
        "cpu_quantities_demo": probe_cpu_quantities_demo,
    }
    return {name: run_probe(fn) for name, fn in fns.items()}
