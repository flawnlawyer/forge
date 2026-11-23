"""FORGE M0 spike - minimal ctypes bindings for Windows.

STATUS: Experimental. Struct layouts follow the documented x64 Windows ABI and are
checked for size/offset consistency by test_m0.py, but nothing here counts as
verified on Windows until run_m0.py and test_m0.py have been executed there.

This module is import-safe on every platform. Calling a Windows-only function
elsewhere raises WindowsOnlyError.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_int,
    c_int32,
    c_int64,
    c_size_t,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
    c_wchar_p,
    sizeof,
)
from typing import Optional

IS_WINDOWS = sys.platform == "win32"

# Fixed-width aliases (avoids ctypes.wintypes so this imports everywhere).
DWORD = c_uint32
WORD = c_uint16
BYTE = c_uint8
BOOL = c_int32
ULONGLONG = c_uint64
LARGE_INTEGER = c_int64
SIZE_T = c_size_t
ULONG_PTR = c_size_t
HANDLE = c_void_p

# Access rights
PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Job object limit flags
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# Job object information classes
JobObjectBasicAccountingInformation = 1
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15

# CPU rate control flags
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4

RelationProcessorCore = 0
ERROR_INSUFFICIENT_BUFFER = 122

# Effective power overlay GUIDs. MAPPING IS FROM MEMORY AND UNVERIFIED: the raw GUID
# is always recorded; the label is only a hint.
OVERLAY_LABELS_BELIEVED = {
    "961cc777-2547-4f9d-8174-7d86181b8a7a": "best power efficiency (believed)",
    "00000000-0000-0000-0000-000000000000": "balanced / recommended (believed)",
    "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "better performance (believed)",
    "ded574b5-45a0-4f42-8737-46345c09c238": "best performance (believed)",
}


class WindowsOnlyError(RuntimeError):
    """Raised when a Windows-only API is called on another platform."""


# ---------------------------------------------------------------- structures
class MEMORYSTATUSEX(Structure):
    _fields_ = [
        ("dwLength", DWORD),
        ("dwMemoryLoad", DWORD),
        ("ullTotalPhys", ULONGLONG),
        ("ullAvailPhys", ULONGLONG),
        ("ullTotalPageFile", ULONGLONG),
        ("ullAvailPageFile", ULONGLONG),
        ("ullTotalVirtual", ULONGLONG),
        ("ullAvailVirtual", ULONGLONG),
        ("ullAvailExtendedVirtual", ULONGLONG),
    ]


class PROCESS_MEMORY_COUNTERS_EX(Structure):
    _fields_ = [
        ("cb", DWORD),
        ("PageFaultCount", DWORD),
        ("PeakWorkingSetSize", SIZE_T),
        ("WorkingSetSize", SIZE_T),
        ("QuotaPeakPagedPoolUsage", SIZE_T),
        ("QuotaPagedPoolUsage", SIZE_T),
        ("QuotaPeakNonPagedPoolUsage", SIZE_T),
        ("QuotaNonPagedPoolUsage", SIZE_T),
        ("PagefileUsage", SIZE_T),
        ("PeakPagefileUsage", SIZE_T),
        ("PrivateUsage", SIZE_T),
    ]


class SYSTEM_POWER_STATUS(Structure):
    _fields_ = [
        ("ACLineStatus", BYTE),
        ("BatteryFlag", BYTE),
        ("BatteryLifePercent", BYTE),
        ("SystemStatusFlag", BYTE),
        ("BatteryLifeTime", DWORD),
        ("BatteryFullLifeTime", DWORD),
    ]


class FILETIME(Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class GUID(Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", c_uint16),
        ("Data3", c_uint16),
        ("Data4", c_uint8 * 8),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class IO_COUNTERS(Structure):
    _fields_ = [
        ("ReadOperationCount", ULONGLONG),
        ("WriteOperationCount", ULONGLONG),
        ("OtherOperationCount", ULONGLONG),
        ("ReadTransferCount", ULONGLONG),
        ("WriteTransferCount", ULONGLONG),
        ("OtherTransferCount", ULONGLONG),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(Structure):
    _fields_ = [
        ("TotalUserTime", LARGE_INTEGER),
        ("TotalKernelTime", LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", LARGE_INTEGER),
        ("TotalPageFaultCount", DWORD),
        ("TotalProcesses", DWORD),
        ("ActiveProcesses", DWORD),
        ("TotalTerminatedProcesses", DWORD),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(Structure):
    # The native layout is { DWORD ControlFlags; union { DWORD CpuRate; DWORD Weight;
    # struct { WORD MinRate; WORD MaxRate; }; }; } - 8 bytes. We use the CpuRate view.
    _fields_ = [("ControlFlags", DWORD), ("CpuRate", DWORD)]


# ------------------------------------------------------------------ helpers
def guid_to_str(g: GUID) -> str:
    d = bytes(g.Data4)
    return "%08x-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x" % (
        g.Data1, g.Data2, g.Data3, d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7])


def _ft(f: FILETIME) -> int:
    return (int(f.dwHighDateTime) << 32) | int(f.dwLowDateTime)


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise WindowsOnlyError("Windows-only API called on " + sys.platform)


def _check(ok) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]


class _Api:
    pass


_api: Optional[_Api] = None


def _load() -> _Api:
    global _api
    if _api is not None:
        return _api
    _require_windows()
    api = _Api()
    k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    ps = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]

    def sig(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = list(argtypes)

    sig(k.GlobalMemoryStatusEx, BOOL, POINTER(MEMORYSTATUSEX))
    sig(k.GetSystemPowerStatus, BOOL, POINTER(SYSTEM_POWER_STATUS))
    sig(k.GetCurrentProcess, HANDLE)
    sig(k.OpenProcess, HANDLE, DWORD, BOOL, DWORD)
    sig(k.CloseHandle, BOOL, HANDLE)
    sig(k.GetProcessTimes, BOOL, HANDLE, POINTER(FILETIME), POINTER(FILETIME),
        POINTER(FILETIME), POINTER(FILETIME))
    sig(k.GetSystemTimes, BOOL, POINTER(FILETIME), POINTER(FILETIME), POINTER(FILETIME))
    sig(k.IsProcessInJob, BOOL, HANDLE, HANDLE, POINTER(BOOL))
    sig(k.CreateJobObjectW, HANDLE, c_void_p, c_wchar_p)
    sig(k.SetInformationJobObject, BOOL, HANDLE, c_int, c_void_p, DWORD)
    sig(k.QueryInformationJobObject, BOOL, HANDLE, c_int, c_void_p, DWORD, POINTER(DWORD))
    sig(k.AssignProcessToJobObject, BOOL, HANDLE, HANDLE)
    sig(k.TerminateJobObject, BOOL, HANDLE, c_uint32)
    sig(k.GetActiveProcessorCount, DWORD, WORD)
    sig(k.GetLogicalProcessorInformationEx, BOOL, c_int, c_void_p, POINTER(DWORD))
    sig(ps.GetProcessMemoryInfo, BOOL, HANDLE, POINTER(PROCESS_MEMORY_COUNTERS_EX), DWORD)
    api.k, api.ps = k, ps
    _api = api
    return api


def _open_process(pid: int, access_options):
    api = _load()
    for access in access_options:
        h = api.k.OpenProcess(access, False, pid)
        if h:
            return h, access
    raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]


# ------------------------------------------------------------ system queries
def global_memory_status() -> dict:
    api = _load()
    s = MEMORYSTATUSEX()
    s.dwLength = sizeof(s)
    _check(api.k.GlobalMemoryStatusEx(byref(s)))
    return {
        "memory_load_pct": int(s.dwMemoryLoad),
        "total_phys": int(s.ullTotalPhys),
        "avail_phys": int(s.ullAvailPhys),
        # Semantics of the two page-file fields are documented as "system or current
        # process, whichever is smaller" - kept raw until verified in M0.
        "total_pagefile_raw": int(s.ullTotalPageFile),
        "avail_pagefile_raw": int(s.ullAvailPageFile),
        "total_virtual_process": int(s.ullTotalVirtual),
        "avail_virtual_process": int(s.ullAvailVirtual),
    }


def process_memory(pid: Optional[int] = None) -> dict:
    """Working set, peak working set, commit (PagefileUsage), peak commit, private bytes."""
    api = _load()
    owned = False
    access = None
    if pid is None:
        h = api.k.GetCurrentProcess()
    else:
        h, access = _open_process(pid, [PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                                        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ])
        owned = True
    try:
        c = PROCESS_MEMORY_COUNTERS_EX()
        c.cb = sizeof(c)
        _check(api.ps.GetProcessMemoryInfo(h, byref(c), sizeof(c)))
        return {
            "working_set": int(c.WorkingSetSize),
            "peak_working_set": int(c.PeakWorkingSetSize),
            "commit": int(c.PagefileUsage),
            "peak_commit": int(c.PeakPagefileUsage),
            "private_bytes": int(c.PrivateUsage),
            "page_fault_count": int(c.PageFaultCount),
            "open_access": hex(access) if access is not None else "current-process",
        }
    finally:
        if owned:
            api.k.CloseHandle(h)


def process_cpu_seconds(pid: Optional[int] = None) -> dict:
    """User and kernel CPU-seconds consumed by one process (GetProcessTimes)."""
    api = _load()
    owned = False
    if pid is None:
        h = api.k.GetCurrentProcess()
    else:
        h, _ = _open_process(pid, [PROCESS_QUERY_LIMITED_INFORMATION])
        owned = True
    try:
        c, e, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        _check(api.k.GetProcessTimes(h, byref(c), byref(e), byref(kt), byref(ut)))
        return {"user_s": _ft(ut) / 1e7, "kernel_s": _ft(kt) / 1e7}
    finally:
        if owned:
            api.k.CloseHandle(h)


def system_times() -> dict:
    """System-wide cumulative times in seconds. Kernel time INCLUDES idle time."""
    api = _load()
    i, k, u = FILETIME(), FILETIME(), FILETIME()
    _check(api.k.GetSystemTimes(byref(i), byref(k), byref(u)))
    return {"idle_s": _ft(i) / 1e7, "kernel_incl_idle_s": _ft(k) / 1e7, "user_s": _ft(u) / 1e7}


def power_status() -> dict:
    api = _load()
    s = SYSTEM_POWER_STATUS()
    _check(api.k.GetSystemPowerStatus(byref(s)))
    ac = {0: "offline (on battery)", 1: "online (AC)", 255: "unknown"}.get(int(s.ACLineStatus), "other")
    pct = None if s.BatteryLifePercent == 255 else int(s.BatteryLifePercent)
    life = None if s.BatteryLifeTime == 0xFFFFFFFF else int(s.BatteryLifeTime)
    return {
        "ac_line_status": ac,
        "ac_line_raw": int(s.ACLineStatus),
        "battery_flag_raw": int(s.BatteryFlag),
        "battery_percent": pct,
        "battery_saver_on": int(s.SystemStatusFlag) == 1,
        "battery_seconds_remaining": life,
    }


def power_overlay() -> dict:
    """Effective power mode overlay (Windows 10 1709+ / 11). May be unavailable."""
    _require_windows()
    try:
        pw = ctypes.WinDLL("powrprof")  # type: ignore[attr-defined]
        fn = pw.PowerGetEffectiveOverlayScheme
    except (OSError, AttributeError) as exc:
        return {"available": False, "error": repr(exc)}
    fn.restype = DWORD
    fn.argtypes = [POINTER(GUID)]
    g = GUID()
    rc = fn(byref(g))
    if rc != 0:
        return {"available": False, "error": "PowerGetEffectiveOverlayScheme rc=%d" % rc}
    s = guid_to_str(g)
    return {"available": True, "guid": s, "label_believed": OVERLAY_LABELS_BELIEVED.get(s.lower())}


def logical_processors() -> dict:
    """Active logical processors (all groups) and physical core count."""
    api = _load()
    active = int(api.k.GetActiveProcessorCount(0xFFFF))
    needed = DWORD(0)
    api.k.GetLogicalProcessorInformationEx(RelationProcessorCore, None, byref(needed))
    if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or needed.value == 0:  # type: ignore[attr-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(needed.value)
    _check(api.k.GetLogicalProcessorInformationEx(RelationProcessorCore, buf, byref(needed)))
    raw = buf.raw[: needed.value]
    offset, cores = 0, 0
    while offset + 8 <= len(raw):
        rel = int.from_bytes(raw[offset:offset + 4], "little")
        size = int.from_bytes(raw[offset + 4:offset + 8], "little")
        if size == 0:
            break
        if rel == RelationProcessorCore:
            cores += 1
        offset += size
    return {"active_logical": active, "physical_cores": cores}


def is_current_process_in_job() -> bool:
    api = _load()
    res = BOOL(0)
    _check(api.k.IsProcessInJob(api.k.GetCurrentProcess(), None, byref(res)))
    return bool(res.value)


# ---------------------------------------------------------------- job objects
class Job:
    """Windows Job Object wrapper for limit experiments.

    KILL_ON_JOB_CLOSE is set by default so a crashed experiment cannot leave
    orphaned child processes behind.
    """

    def __init__(self, kill_on_close: bool = True) -> None:
        api = _load()
        h = api.k.CreateJobObjectW(None, None)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        self.handle = h
        self._closed = False
        self._ext = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if kill_on_close:
            self._ext.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self._apply_ext()

    def _apply_ext(self) -> None:
        api = _load()
        _check(api.k.SetInformationJobObject(
            self.handle, JobObjectExtendedLimitInformation, byref(self._ext), sizeof(self._ext)))

    def set_process_memory_limit(self, limit_bytes: int) -> None:
        """Per-process committed-memory limit (JOB_OBJECT_LIMIT_PROCESS_MEMORY)."""
        self._ext.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
        self._ext.ProcessMemoryLimit = limit_bytes
        self._apply_ext()

    def set_process_time_limit(self, seconds: float) -> None:
        """Per-process USER-mode CPU-time limit (100 ns units)."""
        self._ext.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_TIME
        self._ext.BasicLimitInformation.PerProcessUserTimeLimit = int(seconds * 1e7)
        self._apply_ext()

    def set_job_time_limit(self, seconds: float) -> None:
        """Job-wide USER-mode CPU-time limit (100 ns units), a different flag from the per-process one."""
        self._ext.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_TIME
        self._ext.BasicLimitInformation.PerJobUserTimeLimit = int(seconds * 1e7)
        self._apply_ext()

    def set_cpu_rate_hard_cap(self, core_equivalents: float, logical_cpus: int) -> int:
        """Hard cap on CPU rate. CpuRate is in 1/100 of a percent of TOTAL machine capacity."""
        api = _load()
        rate = int(round(core_equivalents / float(logical_cpus) * 10000))
        rate = max(1, min(10000, rate))
        info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        info.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        info.CpuRate = rate
        _check(api.k.SetInformationJobObject(
            self.handle, JobObjectCpuRateControlInformation, byref(info), sizeof(info)))
        return rate

    def assign(self, pid: int) -> None:
        api = _load()
        h, _ = _open_process(pid, [PROCESS_SET_QUOTA | PROCESS_TERMINATE])
        try:
            _check(api.k.AssignProcessToJobObject(self.handle, h))
        finally:
            api.k.CloseHandle(h)

    def accounting(self) -> dict:
        api = _load()
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        _check(api.k.QueryInformationJobObject(
            self.handle, JobObjectBasicAccountingInformation, byref(info), sizeof(info), None))
        return {
            "user_s": info.TotalUserTime / 1e7,
            "kernel_s": info.TotalKernelTime / 1e7,
            "total_processes": int(info.TotalProcesses),
            "active_processes": int(info.ActiveProcesses),
            "terminated_processes": int(info.TotalTerminatedProcesses),
            "page_faults": int(info.TotalPageFaultCount),
        }

    def extended(self) -> dict:
        api = _load()
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        _check(api.k.QueryInformationJobObject(
            self.handle, JobObjectExtendedLimitInformation, byref(info), sizeof(info), None))
        return {
            "limit_flags": hex(info.BasicLimitInformation.LimitFlags),
            "per_process_user_time_limit_s": info.BasicLimitInformation.PerProcessUserTimeLimit / 1e7,
            "per_job_user_time_limit_s": info.BasicLimitInformation.PerJobUserTimeLimit / 1e7,
            "process_memory_limit": int(info.ProcessMemoryLimit),
            "peak_process_memory_used": int(info.PeakProcessMemoryUsed),
            "peak_job_memory_used": int(info.PeakJobMemoryUsed),
        }

    def terminate(self, exit_code: int = 1) -> None:
        api = _load()
        api.k.TerminateJobObject(self.handle, exit_code)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _load().k.CloseHandle(self.handle)

    def __enter__(self) -> "Job":
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.terminate()
        finally:
            self.close()
