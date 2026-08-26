#!/usr/bin/env python3
"""GPU validation runner — produces PASS/FAIL + JSON report + RMA evidence bundle."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pynvml

THRESHOLDS = {
    "temp_c_max": 85.0,
    "power_pct_max": 95.0,
    "min_pcie_gen": 3,
    "min_pcie_width": 8,
    "ecc_uncorrectable_max": 0,
}

RESET = "\033[0m"


def _s(x):
    """NVML returns bytes on old pynvml, str on nvidia-ml-py — normalize."""
    return x.decode() if isinstance(x, (bytes, bytearray)) else str(x)
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"


class Check:
    def __init__(self, name: str, value, status: str, detail: str = ""):
        self.name, self.value, self.status, self.detail = name, value, status, detail

    def as_dict(self) -> dict:
        return {"name": self.name, "value": self.value,
                "status": self.status, "detail": self.detail}


def _throttle_reasons(flags: int) -> list[str]:
    mapping = {
        1: "app clocks", 2: "SW power cap", 4: "HW power slowdown",
        8: "sync boost", 16: "SW thermal slowdown", 32: "HW thermal slowdown",
        64: "HW power brake", 128: "display clocks", 256: "SW power brake",
        512: "SW thermal brake",
    }
    if flags == 0:
        return ["none"]
    return [lbl for bit, lbl in mapping.items() if flags & bit]


def collect(handle) -> list[Check]:
    checks: list[Check] = []
    name = _s(pynvml.nvmlDeviceGetName(handle))

    # --- driver / library -------------------------------------------------
    drv = _s(pynvml.nvmlSystemGetDriverVersion())
    try:
        cuda = pynvml.nvmlSystemGetCudaDriverVersion_v2()
        cuda_s = f"{cuda // 1000}.{(cuda % 1000) // 10}"
    except Exception:
        cuda_s = "n/a"
    checks.append(Check("driver_version", drv, "PASS", f"CUDA driver {cuda_s}"))

    # --- temperature & throttle ------------------------------------------
    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
    st = "PASS" if temp <= THRESHOLDS["temp_c_max"] else "FAIL"
    checks.append(Check("temperature_c", temp, st,
                        f"limit {THRESHOLDS['temp_c_max']}C"))

    thr_flags = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(handle)
    reasons = _throttle_reasons(thr_flags)
    # informational bits (app clocks=1, sync boost=8) are not real throttling
    real_throttle = thr_flags & ~(1 | 8)
    throttling = real_throttle != 0
    checks.append(Check("throttle", "active" if throttling else "none",
                        "WARN" if throttling else "PASS",
                        ", ".join(reasons)))

    # --- power envelope ---------------------------------------------------
    power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
    try:
        plimit = pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0
        pct = power / plimit * 100 if plimit else 0.0
        st = "PASS" if pct <= THRESHOLDS["power_pct_max"] else "WARN"
        checks.append(Check("power_draw_w", round(power, 1), st,
                            f"{pct:.0f}% of {plimit:.0f}W limit"))
    except pynvml.NVMLError:
        checks.append(Check("power_draw_w", round(power, 1), "PASS",
                            "no power management limit exposed"))

    # --- memory -----------------------------------------------------------
    total_bytes = pynvml.nvmlDeviceGetMemoryInfo(handle).total
    checks.append(Check("vram_total_gib", round(total_bytes / (1024 ** 3), 1),
                        "PASS", name))

    # --- PCIe link integrity ----------------------------------------------
    gen = pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle)
    width = pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle)
    max_gen = pynvml.nvmlDeviceGetMaxPcieLinkGeneration(handle)
    max_w = pynvml.nvmlDeviceGetMaxPcieLinkWidth(handle)
    util = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
    ok_gen = gen >= THRESHOLDS["min_pcie_gen"]
    ok_w = width >= THRESHOLDS["min_pcie_width"]
    detail = f"Gen{gen} x{width} (max Gen{max_gen} x{max_w})"
    if not ok_gen or not ok_w:
        # A static read can't prove a bad link: PCIe downshifts on bandwidth
        # demand, not utilization. Flag as WARN and defer hard FAIL to the
        # bandwidth burn test (step 2) which forces the link up.
        st = "WARN"
        detail += " — downshifted; verify under bandwidth load (burn test)"
    else:
        st = "PASS"
    checks.append(Check("pcie_link", f"Gen{gen} x{width}", st, detail))

    # --- clocks & perf state ---------------------------------------------
    sm = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
    mem = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
    pstate = pynvml.nvmlDeviceGetPerformanceState(handle)
    checks.append(Check("clocks_mhz", f"SM {sm} / MEM {mem}", "PASS",
                        f"perf-state P{pstate}"))

    # --- ECC (datacenter feature; N/A on consumer silicon) ----------------
    try:
        ecc_u = pynvml.nvmlDeviceGetTotalEccErrors(
            handle, pynvml.NVML_MEMORY_ERROR_TYPE_UNCORRECTED,
            pynvml.NVML_VOLATILE_ECC)
        ecc_c = pynvml.nvmlDeviceGetTotalEccErrors(
            handle, pynvml.NVML_MEMORY_ERROR_TYPE_CORRECTED,
            pynvml.NVML_VOLATILE_ECC)
        st = "PASS" if ecc_u <= THRESHOLDS["ecc_uncorrectable_max"] else "FAIL"
        checks.append(Check("ecc", f"uncorrectable={ecc_u} correctable={ecc_c}",
                            st, "volatile ECC counters"))
    except pynvml.NVMLError:
        checks.append(Check("ecc", "N/A", "N/A",
                            "consumer silicon — no ECC (datacenter cards: A100/H100/MI300)"))

    # --- identity ---------------------------------------------------------
    try:
        sn = _s(pynvml.nvmlDeviceGetSerial(handle))
    except Exception:
        sn = "n/a"
    try:
        uuid = _s(pynvml.nvmlDeviceGetUUID(handle))
    except Exception:
        uuid = "n/a"
    checks.append(Check("serial/uuid", sn, "PASS", f"uuid={uuid}"))

    return checks


def verdict(checks: list[Check]) -> str:
    statuses = [c.status for c in checks]
    if any(s == "FAIL" for s in statuses):
        return "FAIL"
    if any(s == "WARN" for s in statuses):
        return "WARN"
    return "PASS"


def collect_rma_evidence(out_dir: Path, checks: list[Check]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "nvidia-smi.txt").write_text(
        subprocess.run(["nvidia-smi", "-q"], capture_output=True, text=True).stdout)
    dmesg = subprocess.run(["dmesg"], capture_output=True, text=True).stdout
    (out_dir / "dmesg.txt").write_text("\n".join(dmesg.splitlines()[-200:]))
    (out_dir / "report.json").write_text(json.dumps(
        [c.as_dict() for c in checks], indent=2))
    return out_dir / "report.json"


def color(status: str) -> str:
    return {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED, "N/A": DIM}.get(status, RESET)


def main() -> int:
    ap = argparse.ArgumentParser(description="GPU production validation suite")
    ap.add_argument("--rma-dir", type=Path,
                    default=Path("artifacts/rma"), help="RMA evidence output dir")
    ap.add_argument("--json", type=Path, help="write JSON report to this path")
    args = ap.parse_args()

    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError as e:
        print(f"ERROR: NVML init failed: {e}", file=sys.stderr)
        return 2

    if pynvml.nvmlDeviceGetCount() == 0:
        print("ERROR: no GPU devices found", file=sys.stderr)
        return 2

    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    checks = collect(handle)
    v = verdict(checks)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print(f"\n{CYAN}=== GPU Production Validation — {ts} ==={RESET}\n")
    for c in checks:
        print(f"  {color(c.status)}{c.status:4}{RESET} {c.name:20} "
              f"{DIM}{c.value}{RESET}")
        if c.detail:
            print(f"       {DIM}↳ {c.detail}{RESET}")
    print(f"\n  {color(v)}VERDICT: {v}{RESET}\n")

    if v == "FAIL":
        rpt = collect_rma_evidence(args.rma_dir, checks)
        print(f"{RED}RMA evidence bundle written to {rpt.parent}{RESET}\n")

    if args.json:
        # extract identity fields for the control-plane report (gate needs them)
        node_id = ""
        serial = ""
        for c in checks:
            if c.name == "serial/uuid":
                serial = str(c.value)
                if "uuid=" in c.detail:
                    node_id = c.detail.split("uuid=")[1]
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {"timestamp": ts, "verdict": v,
             "node_id": node_id, "serial": serial,
             "source": "gpu_validate/1.0",
             "checks": [c.as_dict() for c in checks]},
            indent=2))
        print(f"JSON report: {args.json}\n")

    pynvml.nvmlShutdown()
    return {"PASS": 0, "WARN": 0, "FAIL": 1}.get(v, 1)


if __name__ == "__main__":
    sys.exit(main())
