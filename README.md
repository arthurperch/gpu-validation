# GPU Validation Fleet

Production-style GPU hardware validation: burn-in, health, and network checks
that report into a **serverless gate** (Lambda + API Gateway + DynamoDB) which
decides whether each node gets **provisioned, held, or RMA'd**.

**Design principle:** the node *reports* raw checks; the control plane *decides*
the gate. A node cannot gate itself — mirroring real fleet onboarding and RMA.

---

## Components

### Validator (`validator/`)
| Module | Purpose |
|---|---|
| `gpu_validate.py` | NVML health check — driver, temp, throttle, power, VRAM, PCIe, clocks, ECC, serial |
| `burn_test.py` + `burn.cu` | CUDA burn-in — forces the PCIe link up + thermals |
| `network_validate.py` | DHCP / IPv6 / ICMP readiness |
| `combine_report.py` | Merge + reconcile static-vs-dynamic checks |
| `submit_report.py` | POST the JSON report to the gate |

### Orchestration (`ansible/`)
`onboard.yml` — 5-stage pipeline. Runs **identically** against a local node or a
rented cloud GPU by swapping the host/connection.

### The gate (`lab/cloud/labs/04-gpu-gate/`)
Lambda + API Gateway + DynamoDB, built on Terraform + LocalStack.

| Report status | Decision |
|---|---|
| any `FAIL` | `RMA` — pull the node |
| any `WARN` | `HOLD` — manual review / burn test |
| else | `PROVISION` — onboard to production |

---

## Architecture

```
[GPU node]  gpu_validate.py ──JSON──▶ API Gateway POST /validate
                                          │
                                          ▼
                                   Lambda "gpu-gate"
                                     • parse report
                                     • DECIDE the gate
                                     • append audit record
                                          │
                                          ▼
                                   DynamoDB "gpu-nodes"
                                   (node_id, report_ts) history
```

---

## Key insights discovered

- **PCIe downshifts on bandwidth demand, not utilization.** A desktop compositor
  hits 30% GPU util but parks the PCIe link at Gen1 (ASPM). A static read can't
  prove a bad link — you need a bandwidth load to force it up. *(Observed live:
  Gen1 idle → Gen3 under burn load.)*
- **ECC is datacenter-only.** Consumer silicon (RTX 3070) reports `N/A`; datacenter
  parts (A100/H100/MI300) expose real ECC counters.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install nvidia-ml-py ansible
python -m validator.gpu_validate
```

---

## Status

Active. The gate integration (`04-gpu-gate`) is the current work — wiring a real
validator report through the API to a PROVISION/HOLD/RMA decision.
