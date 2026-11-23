"""FORGE M0 tests. Run from this directory:  python -m unittest -v test_m0

Portable tests run everywhere. Windows-only tests are SKIPPED elsewhere; a skipped
test is not evidence. Struct size/offset tests check internal consistency against
the documented x64 layouts - they cannot prove Windows agrees.
"""
import ctypes
import sys
import time
import unittest

import limits_lab
import probes
import run_m0
import winapi
from winapi import IS_WINDOWS

X64 = ctypes.sizeof(ctypes.c_void_p) == 8


@unittest.skipUnless(X64, "documented layouts below are for 64-bit")
class TestStructLayouts(unittest.TestCase):
    def test_sizes(self):
        expected = {
            winapi.MEMORYSTATUSEX: 64,
            winapi.PROCESS_MEMORY_COUNTERS_EX: 80,
            winapi.SYSTEM_POWER_STATUS: 12,
            winapi.FILETIME: 8,
            winapi.GUID: 16,
            winapi.JOBOBJECT_BASIC_LIMIT_INFORMATION: 64,
            winapi.IO_COUNTERS: 48,
            winapi.JOBOBJECT_EXTENDED_LIMIT_INFORMATION: 144,
            winapi.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: 48,
            winapi.JOBOBJECT_CPU_RATE_CONTROL_INFORMATION: 8,
        }
        for cls, size in expected.items():
            with self.subTest(struct=cls.__name__):
                self.assertEqual(ctypes.sizeof(cls), size)

    def test_offsets(self):
        E = winapi.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        B = winapi.JOBOBJECT_BASIC_LIMIT_INFORMATION
        expected = [
            (winapi.MEMORYSTATUSEX, "ullTotalPhys", 8),
            (winapi.PROCESS_MEMORY_COUNTERS_EX, "PeakWorkingSetSize", 8),
            (winapi.PROCESS_MEMORY_COUNTERS_EX, "PagefileUsage", 56),
            (winapi.PROCESS_MEMORY_COUNTERS_EX, "PrivateUsage", 72),
            (B, "LimitFlags", 16),
            (B, "MinimumWorkingSetSize", 24),
            (B, "Affinity", 48),
            (E, "IoInfo", 64),
            (E, "ProcessMemoryLimit", 112),
            (E, "JobMemoryLimit", 120),
            (E, "PeakProcessMemoryUsed", 128),
            (E, "PeakJobMemoryUsed", 136),
        ]
        for cls, field, off in expected:
            with self.subTest(struct=cls.__name__, field=field):
                self.assertEqual(getattr(cls, field).offset, off)


class TestCpuQuantities(unittest.TestCase):
    def test_three_quantities_are_distinct(self):
        q = probes.cpu_quantities(cpu_seconds=2.0, wall_seconds=4.0, logical_cpus=12)
        self.assertAlmostEqual(q["cpu_seconds"], 2.0)
        self.assertAlmostEqual(q["core_equivalents"], 0.5)
        self.assertAlmostEqual(q["normalized_pct"], 0.5 / 12 * 100)
        self.assertEqual(len({q["cpu_seconds"], q["core_equivalents"], q["normalized_pct"]}), 3)

    def test_parallel_work_exceeds_one_core_equivalent(self):
        q = probes.cpu_quantities(cpu_seconds=8.0, wall_seconds=2.0, logical_cpus=12)
        self.assertAlmostEqual(q["core_equivalents"], 4.0)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            probes.cpu_quantities(1.0, 0.0, 4)
        with self.assertRaises(ValueError):
            probes.cpu_quantities(1.0, 1.0, 0)


class TestParsing(unittest.TestCase):
    def test_powercfg_guid(self):
        text = "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)"
        self.assertEqual(probes.parse_powercfg_guid(text), "381b4222-f694-41f0-9685-ff5bb260df2e")
        self.assertIsNone(probes.parse_powercfg_guid("no guid here"))

    def test_guid_to_str(self):
        g = winapi.GUID(0x381B4222, 0xF694, 0x41F0,
                        (ctypes.c_uint8 * 8)(0x96, 0x85, 0xFF, 0x5B, 0xB2, 0x60, 0xDF, 0x2E))
        self.assertEqual(winapi.guid_to_str(g), "381b4222-f694-41f0-9685-ff5bb260df2e")


class TestWindowStats(unittest.TestCase):
    def test_windows_of_one_sample(self):
        samples = [(0.0, 0.0), (0.1, 0.1), (0.2, 0.15), (0.3, 0.35)]
        st = limits_lab.window_stats(samples, 1)
        self.assertEqual(st["n"], 3)
        self.assertAlmostEqual(st["min"], 0.5)
        self.assertAlmostEqual(st["max"], 2.0)

    def test_wider_windows(self):
        samples = [(0.0, 0.0), (0.1, 0.1), (0.2, 0.15), (0.3, 0.35)]
        st = limits_lab.window_stats(samples, 3)
        self.assertEqual(st["n"], 1)
        self.assertAlmostEqual(st["median"], 0.35 / 0.3)

    def test_too_few_samples(self):
        self.assertIsNone(limits_lab.window_stats([(0.0, 0.0)], 1))


class TestLineReaderDrain(unittest.TestCase):
    """Regression test: v1 read the history before draining the queue and lost child output."""

    def test_lines_visible_after_child_exit(self):
        p = limits_lab._spawn("memlimit", 32, 16)
        lr = limits_lab.LineReader(p.stdout)
        try:
            self.assertIsNotNone(lr.wait_for("ready", 30))
            limits_lab._send(p, "go")
            p.wait(timeout=60)
            lr.join(timeout=5)
            lines = lr.lines()
        finally:
            limits_lab._reap(p)
        self.assertIn("alloc 16", lines)
        self.assertTrue(any(s.startswith("completed") for s in lines), lines)


class TestPowerRequirement(unittest.TestCase):
    def _probe(self, raw):
        return {"status": "ok", "value": {"system_power_status": {"ac_line_raw": raw}}}

    def test_no_requirement(self):
        self.assertIsNone(run_m0.check_power_requirement(self._probe(1), None))

    def test_matching_state(self):
        self.assertIsNone(run_m0.check_power_requirement(self._probe(0), "battery"))
        self.assertIsNone(run_m0.check_power_requirement(self._probe(1), "ac"))

    def test_mismatch_is_reported(self):
        msg = run_m0.check_power_requirement(self._probe(1), "battery")
        self.assertIn("'ac'", msg)

    def test_unavailable_power_state_fails_closed(self):
        msg = run_m0.check_power_requirement({"status": "unavailable"}, "battery")
        self.assertIn("cannot be verified", msg)


class TestPollOverhead(unittest.TestCase):
    def test_measures_reads(self):
        r = limits_lab.poll_overhead_test(intervals_ms=(20,), duration_s=1.0)
        row = r["rows"][0]
        self.assertGreater(row["reads"], 10)
        self.assertGreaterEqual(row["parent_cpu_s"], 0.0)


class TestImportSafety(unittest.TestCase):
    @unittest.skipIf(IS_WINDOWS, "checks non-Windows behavior")
    def test_windows_api_raises_elsewhere(self):
        with self.assertRaises(winapi.WindowsOnlyError):
            winapi.global_memory_status()

    def test_probes_never_raise(self):
        results = probes.collect_probes()
        self.assertIn("environment", results)
        for name, r in results.items():
            with self.subTest(probe=name):
                self.assertIn(r["status"], ("ok", "unavailable", "error"))


class TestPollingHarness(unittest.TestCase):
    """Harness self-test. On non-Windows this is NOT Windows evidence."""

    def test_kernel_peak_sees_spike(self):
        r = limits_lab.polling_miss(spike_mb=20, holds_ms=[20], intervals_ms=[10], trials=1)
        row = r["rows"][0]
        self.assertGreater(row["polls"], 0)
        self.assertGreater(row["kernel_delta_mb"], 10)
        self.assertGreaterEqual(row["kernel_peak_bytes"] + 1024 * 1024, row["polled_max_bytes"])


@unittest.skipUnless(IS_WINDOWS, "Windows only")
class TestWindowsBindings(unittest.TestCase):
    def test_memory_status_plausible(self):
        m = winapi.global_memory_status()
        self.assertGreater(m["total_phys"], 0)
        self.assertGreater(m["avail_phys"], 0)
        self.assertLessEqual(m["avail_phys"], m["total_phys"])

    def test_process_memory_plausible(self):
        m = winapi.process_memory()
        self.assertGreater(m["working_set"], 0)
        self.assertGreaterEqual(m["peak_working_set"], m["working_set"])

    def test_cpu_times_agree_with_stdlib(self):
        end = time.perf_counter() + 0.3
        while time.perf_counter() < end:
            pass
        c = winapi.process_cpu_seconds()
        self.assertAlmostEqual(c["user_s"] + c["kernel_s"], time.process_time(), delta=0.1)

    def test_logical_processors(self):
        p = winapi.logical_processors()
        self.assertGreaterEqual(p["active_logical"], p["physical_cores"])
        self.assertGreaterEqual(p["physical_cores"], 1)

    def test_power_status_fields(self):
        s = winapi.power_status()
        self.assertIn("ac_line_status", s)

    def test_job_object_roundtrip(self):
        with winapi.Job() as job:
            job.set_process_memory_limit(256 * 1024 * 1024)
            ext = job.extended()
            self.assertEqual(ext["process_memory_limit"], 256 * 1024 * 1024)
            self.assertEqual(job.accounting()["total_processes"], 0)

    def test_job_time_limit_readback(self):
        with winapi.Job() as job:
            job.set_process_time_limit(1.0)
            ext = job.extended()
            self.assertTrue(int(ext["limit_flags"], 16) & winapi.JOB_OBJECT_LIMIT_PROCESS_TIME)
            self.assertAlmostEqual(ext["per_process_user_time_limit_s"], 1.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
