#!/usr/bin/env python3
"""Combine health + burn + network reports into one gate submission.

The three validators each emit a JSON report. The gate decides on a single
combined picture. The key engineering detail is reconciliation:

  * the static health check flags a downshifted PCIe link as WARN (a static
    read cannot prove a bad link);
  * the burn test proves whether the link actually ramps under bandwidth load;
  * so the burn result OVERRIDES the health PCIe status.

Static checks are conservative; dynamic checks are authoritative.

Usage:
  python validator/combine_report.py --out reports/combined.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS = REPO_ROOT / "reports"


def load(name: str) -> dict:
    p = DEFAULT_REPORTS / name
    if not p.exists():
        print(f"missing {p} - run the corresponding validator first", file=sys.stderr)
        return {}
    return json.loads(p.read_text())


def reconcile(health_checks, burn):
    """Override health PCIe WARN with the burn test's authoritative result."""
    ramped = burn.get("pcie_max_gen_seen", 0) >= burn.get("pcie_max_gen", 0)
    out = []
    for c in health_checks:
        if c["name"] == "pcie_link" and c["status"] == "WARN" and ramped:
            c = dict(c)
            c["status"] = "PASS"
            c["detail"] = (f"{c['value']} - downshift was idle-only; "
                           f"ramped to Gen{burn['pcie_max_gen_seen']} under load")
        out.append(c)
    return out


def status_for(ok: bool, fail_allowed: bool) -> str:
    """Map a boolean to a status. fail_allowed=True means WARN instead of FAIL."""
    if ok:
        return "PASS"
    return "WARN" if fail_allowed else "FAIL"


def burn_checks(burn) -> list:
    if not burn:
        return []
    # Baselines tuned for the RTX 3070 used to develop this; per-SKU in a
    # real fleet. Kept here rather than in burn_test.py so the gate can also
    # reason about the same numbers without importing the node-side script.
    temp_limit = 85.0
    boost_sm_min = 1500
    power_min = 100.0
    ramped = burn.get("pcie_max_gen_seen", 0) >= burn.get("pcie_max_gen", 0)
    return [
        {"name": "burn_peak_temp_c", "value": burn.get("peak_temp_c"),
         "status": status_for(burn.get("peak_temp_c", 999) <= temp_limit, fail_allowed=False),
         "detail": f"limit {temp_limit:.0f}C"},
        {"name": "burn_max_sm_mhz", "value": burn.get("max_sm_mhz"),
         "status": status_for(burn.get("max_sm_mhz", 0) >= boost_sm_min, fail_allowed=True),
         "detail": "held boost under load"},
        {"name": "burn_max_power_w", "value": burn.get("max_power_w"),
         "status": status_for(burn.get("max_power_w", 0) >= power_min, fail_allowed=True),
         "detail": "memory-bound kernel; compute-dense would draw more"},
        {"name": "burn_pcie_under_load", "value": f"Gen{burn.get('pcie_max_gen_seen')}",
         "status": status_for(ramped, fail_allowed=False),
         "detail": "ramped to max gen under bandwidth load"},
        {"name": "burn_max_util", "value": burn.get("max_util"),
         "status": status_for(burn.get("max_util", 0) >= 90, fail_allowed=True),
         "detail": "percent"},
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="combine reports for the gate")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "combined.json")
    args = ap.parse_args()

    health = load("health.json")
    burn = load("burn.json")
    network = load("network.json")

    health_checks = reconcile(health.get("checks", []), burn)
    all_checks = health_checks + burn_checks(burn) + network.get("checks", [])

    statuses = [c["status"] for c in all_checks]
    verdict = "FAIL" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "PASS")

    combined = {
        "node_id": health.get("node_id", ""),
        "serial": health.get("serial", ""),
        "source": "gpu_validation_pipeline/1.0",
        "verdict": verdict,
        "checks": all_checks,
    }
    args.out.write_text(json.dumps(combined, indent=2))

    print(f"\n=== Combined report ({len(all_checks)} checks) ===\n")
    for c in all_checks:
        print(f"  {c['status']:4} {c['name']:22} {c['value']}")
    print(f"\n  VERDICT: {verdict}")
    print(f"  written: {args.out}\n")

    return {"PASS": 0, "WARN": 0, "FAIL": 1}.get(verdict, 1)


if __name__ == "__main__":
    sys.exit(main())
