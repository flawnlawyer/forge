# FORGE M0: Windows feasibility spike

Status: written and self-tested on Linux only. **Nothing here is verified on Windows until you run the steps below and send back the JSON.**

Scope: isolated spike. Standard library only (no psutil). Not the FORGE package; do not import it from anywhere.

## Run on the Windows laptop (Python 3.11+)

Open PowerShell in this folder. Plug the laptop in and use your normal power mode for the first run. For a second run, unplug it (battery) so AC and battery state are both on record.

```
python --version
python -m unittest -v test_m0
python run_m0.py --out evidence\win_probes_ac.json
python run_m0.py --run-experiments --out evidence\win_experiments_ac.json
```

Then repeat the last two commands on battery, with `_battery` in the file names. Create the `evidence` folder first (`mkdir evidence`).

## Safety

- `--run-experiments` starts child Python processes that allocate at most 512 MB each.
- The Job Object tests refuse to run if available RAM is under 2 GB.
- Every child is placed in a job with kill-on-close, and every wait has a timeout.
- Close memory-heavy applications first, and do not run these during something you cannot afford to interrupt.
- The tests change no system settings. `powercfg /getactivescheme` is read-only.

## Files

| File | Purpose |
|---|---|
| `winapi.py` | ctypes bindings (memory, CPU times, power, job objects); import-safe off Windows |
| `probes.py` | read-only detection and measurement probes |
| `limits_lab.py` | polling-miss experiment and Job Object limit experiments |
| `m0_child.py` | child process with a hard 512 MB allocation cap |
| `run_m0.py` | runner; writes the JSON evidence file |
| `test_m0.py` | unit tests; Windows-only tests skip elsewhere |
| `evidence/linux_container_selftest.json` | harness self-test output from Linux (not Windows evidence) |

Send back: the unittest output and the four JSON files.

## Round 3 (after the second Windows results)

The file named `win_experiments_battery.json` was recorded on AC power (its power probe says `online (AC)`), so no battery evidence exists yet. The runner now has a gate: `--require-power battery` aborts before running anything unless Windows reports battery power.

1. Replace all files with the versions in this folder.
2. `python -m unittest -v test_m0`
3. Unplug the charger. Confirm the battery icon shows it is discharging. Then run:

```
python run_m0.py --require-power battery --run-experiments --out evidence\win_experiments_battery_real.json
```

If it prints `ABORTED`, Windows still reports AC power; fix that and run it again. A full run takes about three minutes. Plug the charger back in afterwards.

4. Plugged in again, run once more so we have a matching AC file from the same version:

```
python run_m0.py --require-power ac --run-experiments --out evidence\win_experiments_ac_r3.json
```

New in this round: the CPU-time test repeats the "limit set after assigning the process" variant five times and also tries the job-wide limit after assignment; a new test measures the CPU cost of polling at 10, 50 and 250 ms.

Send back the unittest output and both JSON files.
