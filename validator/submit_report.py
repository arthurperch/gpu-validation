#!/usr/bin/env python3
"""Submit a GPU validation report to the control-plane gate.

Reads the JSON report produced by gpu_validate.py and POSTs it to the
gpu-gate Lambda (via API Gateway). The node reports; the control plane
decides PROVISION / HOLD / RMA.

Usage:
    python validator/submit_report.py --report reports/20260825.json \
        --endpoint http://localhost:4566/restapis/<id>/prod/_user_request_/validate
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser(description="submit GPU report to control plane")
    ap.add_argument("--report", required=True, help="path to JSON report")
    ap.add_argument("--endpoint", required=True, help="gate API endpoint URL")
    args = ap.parse_args()

    with open(args.report) as f:
        report = json.load(f)

    req = urllib.request.Request(
        args.endpoint,
        data=json.dumps(report).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            print(f"gate decision: {body['decision']} "
                  f"(node {body['node_id']}, {body['report_ts']})")
            for r in body["reasons"]:
                print(f"  - {r}")
            return 0 if body["decision"] != "RMA" else 1
    except urllib.error.HTTPError as e:
        print(f"gate returned HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"cannot reach gate: {e.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
