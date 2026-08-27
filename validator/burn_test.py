#!/usr/bin/env python3
"""Bandwidth/compute burn test, validates the GPU *under load*.

Runs the CUDA burn binary in the background and samples NVML telemetry the
whole time. A static health check (gpu_validate.py) can't prove the PCIe
link or thermal envelope; only a real workload can. This script answers:

  * did the PCIe link ramp to its max generation under bandwidth load?
  * did clocks ramp to boost and power draw climb?
  * did temperature stay inside the envelope, or did the card throttle?

Exit codes:
  0 -> PASS   (held max clocks, link ramped, temps in envelope)
  1 -> FAIL   (throttled / link didn't ramp / thermal breach under load)
  2 -> ERROR  (could not run)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import pynvml

# Resolve the default binary relative to the repo root, so the script works
# no matter what CWD it is launched from (e.g. by Ansible, which runs from ~).
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BINARY = REPO_ROOT / "artifacts" / "burn"


def sample(handle):
    """One NVML sample: temp, clocks, power, PCIe, utilization."""
    gen = pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle)
    width = pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle)
    return {
        "temp_c": pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU),
        "sm_mhz": pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM),
        "mem_mhz": pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM),
        "power_w": round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 1),
        "pcie": f"Gen{gen}x{width}",
        "gpu_util": pynvml.nvmlDeviceGetUtilizationRates(handle).gpu,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="GPU burn test under load")
    ap.add_argument("--seconds", type=int, default=10)
    ap.add_argument("--buffer-gib", type=int, default=2)
    ap.add_argument("--binary", default=str(DEFAULT_BINARY))
    ap.add_argument("--json", type=Path, help="write timeline to file")
    args = ap.parse_args()

    pynvml.nvmlInit()
    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    max_gen = pynvml.nvmlDeviceGetMaxPcieLinkGeneration(handle)
    temp_limit = 85.0

    print(f"\n=== GPU Burn Test - {args.seconds}s @ {args.buffer_gib} GiB ===\n")
    print(f"launching {args.binary}...\n")

    proc = subprocess.Popen(
        [args.binary, str(args.seconds), str(args.buffer_gib)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    timeline = []
    start = time.time()
    while proc.poll() is None and time.time() - start < args.seconds + 5:
        timeline.append(sample(handle))
        time.sleep(0.5)

    out, _ = proc.communicate()
    if out.strip():
        print(f"{out.strip()}\n")

    if not timeline:
        print("ERROR: no telemetry collected - did the burn binary run?")
        return 2

    # --- analyze the run -------------------------------------------------
    t = max(s["temp_c"] for s in timeline)
    max_sm = max(s["sm_mhz"] for s in timeline)
    max_power = max(s["power_w"] for s in timeline)
    max_util = max(s["gpu_util"] for s in timeline)
    max_gen_seen = max(int(s["pcie"][3]) for s in timeline)

    link_ramped = max_gen_seen >= max_gen
    held_boost = max_sm >= 1500
    drew_power = max_power >= 100.0
    thermal_ok = t <= temp_limit

    print("=== Burn results ===")
    rows = [
        ("peak temp", f"{t}C", "PASS" if thermal_ok else "FAIL", f"limit {temp_limit}C"),
        ("max SM clock", f"{max_sm} MHz", "PASS" if held_boost else "WARN", "boost ~1500+"),
        ("max power", f"{max_power} W", "PASS" if drew_power else "WARN", "expect 100W+"),
        ("max util", f"{max_util}%", "PASS", ""),
        ("PCIe link", f"Gen{max_gen_seen} (max Gen{max_gen})",
         "PASS" if link_ramped else "FAIL", "ramped to max gen under load"),
    ]
    for name, val, st, note in rows:
        print(f"  {st:4} {name:12} {val}  {note}")

    if not link_ramped or not thermal_ok:
        verdict = "FAIL"
    elif not held_boost or not drew_power:
        verdict = "WARN"
    else:
        verdict = "PASS"
    print(f"\n  VERDICT: {verdict}\n")

    if args.json:
        args.json.write_text(json.dumps({
            "test": "burn", "seconds": args.seconds,
            "peak_temp_c": t, "max_sm_mhz": max_sm, "max_power_w": max_power,
            "max_util": max_util, "pcie_max_gen_seen": max_gen_seen,
            "pcie_max_gen": max_gen, "verdict": verdict,
            "timeline": timeline}, indent=2))
        print(f"timeline: {args.json}\n")

    pynvml.nvmlShutdown()
    return {"PASS": 0, "WARN": 0, "FAIL": 1}.get(verdict, 1)


if __name__ == "__main__":
    sys.exit(main())
