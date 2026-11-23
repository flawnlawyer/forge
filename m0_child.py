"""FORGE M0 child process. Launched only by limits_lab.py.

Protocol (line based, stdin/stdout): the child prints 'ready', waits for a line
('go'), does its work, prints result lines, then (for modes that need it) waits
for a final line so the parent can read kernel peak counters before exit.

Safety: this process never allocates more than HARD_CAP_MB in total, whatever
arguments it is given.
"""
import sys
import time

MB = 1024 * 1024
HARD_CAP_MB = 512


def _touch(buf: bytearray) -> None:
    """Write one byte per 4 KiB page so pages are really committed to the working set."""
    buf[::4096] = b"\x01" * len(range(0, len(buf), 4096))


def _guard(mb: int) -> None:
    if mb > HARD_CAP_MB:
        print("refused: %d MB exceeds hard cap %d MB" % (mb, HARD_CAP_MB), flush=True)
        raise SystemExit(2)


def mode_spike(mb: int, hold_ms: float) -> None:
    """Allocate and touch mb MB, hold for hold_ms, then free. Used for polling-miss tests."""
    _guard(mb)
    print("ready", flush=True)
    sys.stdin.readline()
    t0 = time.perf_counter()
    buf = bytearray(mb * MB)
    _touch(buf)
    t1 = time.perf_counter()
    time.sleep(hold_ms / 1000.0)
    del buf
    t2 = time.perf_counter()
    print("spiked %.4f %.4f" % (t1 - t0, t2 - t0), flush=True)
    sys.stdin.readline()  # stay alive so the parent can read the kernel peak counter


def mode_memlimit(try_mb: int, step_mb: int) -> None:
    """Allocate in steps up to try_mb, reporting progress. Used under a Job Object memory limit."""
    _guard(try_mb)
    print("ready", flush=True)
    sys.stdin.readline()
    chunks = []
    total = 0
    try:
        while total < try_mb:
            c = bytearray(step_mb * MB)
            _touch(c)
            chunks.append(c)
            total += step_mb
            print("alloc %d" % total, flush=True)
        print("completed %d" % total, flush=True)
    except MemoryError:
        print("memoryerror %d" % total, flush=True)


def mode_spin(run_s: float) -> None:
    """Single-threaded busy loop for run_s wall seconds. Used for CPU limit tests."""
    print("ready", flush=True)
    sys.stdin.readline()
    end = time.perf_counter() + run_s
    while time.perf_counter() < end:
        pass
    print("done", flush=True)


def main(argv) -> None:
    mode = argv[1]
    if mode == "spike":
        mode_spike(int(argv[2]), float(argv[3]))
    elif mode == "memlimit":
        mode_memlimit(int(argv[2]), int(argv[3]))
    elif mode == "spin":
        mode_spin(float(argv[2]))
    else:
        raise SystemExit("unknown mode: " + mode)


if __name__ == "__main__":
    main(sys.argv)
