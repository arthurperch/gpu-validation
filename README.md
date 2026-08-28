# GPU Validation Fleet
https://media.discordapp.net/attachments/1507463032828199127/1542990747345756200/content.png?ex=6a933d9d&is=6a91ec1d&hm=d0d7f6cadb15f356ae16cf320eb01df0a95100abf5f4d23ae93dce6e84ac8276&=&format=webp&quality=lossless&width=768&height=577
Tools for checking whether a GPU node is healthy enough to put into production.
A node runs a health check, a burn in test, and a network check, then posts the
results to a serverless gate that decides whether to onboard it, hold it, or
send it back (RMA).

The node reports. The gate decides. A node never gates itself.

## What's in here

The repo has two halves.

**The validators** live in `validator/`. These run on the GPU machine itself and
each one writes a JSON report.

| File | What it does |
|---|---|
| `gpu_validate.py` | Reads NVML sensors directly. Driver, temperature, throttle, power, VRAM, PCIe link, clocks, ECC, serial. |
| `burn.cu` + `burn_test.py` | Stresses the card under load so a weak GPU can't hide behind an idle read. |
| `network_validate.py` | Checks DHCP, IPv6, and ICMP. A node that can't reach the network is useless no matter how good the GPU is. |
| `combine_report.py` | Merges the three reports into one and reconciles conflicting results. |
| `submit_report.py` | Posts the combined report to the gate. |

**The orchestration** lives in `ansible/`. `onboard.yml` runs the whole thing as
one pipeline: validate, burn, network check, combine, submit.

The gate itself is a separate repo (Lambda + API Gateway + DynamoDB on
Terraform + LocalStack). It receives a report and returns one of three
decisions:

| Report status | Decision |
|---|---|
| any `FAIL` | `RMA` (pull the node) |
| any `WARN` | `HOLD` (manual review, or rerun the burn test) |
| otherwise | `PROVISION` (onboard it) |

## Why this shape

Three ideas drove the design, and they're the interesting part.

The first is that **PCIe downshifts on bandwidth demand, not utilization.** A
desktop compositor can pin the GPU at 30% utilization while the PCIe link sits
parked at Gen1 to save power. A static read can't tell a downshifted link from
a broken one. So the health check only *warns* on a low link speed, and the
burn test is what actually forces the link up and proves it can hold Gen3.
That ordering, static check is conservative and dynamic test is authoritative,
is the whole trick.

The second is that **ECC only exists on datacenter cards.** A consumer RTX 3070
has no error correcting memory, so the validator reports `N/A` on it, and would
report real uncorrectable and correctable counts on an A100 or H100 or MI300.
Handling both without crashing matters if the same pipeline ever runs against
rented cloud GPUs.

The third is that **the gate lives off the node.** A machine shouldn't be able
to declare itself healthy. The node sends raw readings, and a central service
decides what to do with them. That's how real fleet onboarding and RMA work.

## Quick start

You need a machine with an NVIDIA GPU, the driver installed, and Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install nvidia-ml-py ansible

# compile the burn binary
nvcc -arch=sm_86 -O3 -o artifacts/burn validator/burn.cu

# run the health check
python validator/gpu_validate.py --json reports/health.json

# run the whole pipeline (needs the gate running, or LocalStack)
ansible-playbook -i ansible/inventory.ini ansible/playbooks/onboard.yml
```

`nvidia-ml-py` is the NVIDIA NVML Python binding. `ansible` is only needed for
the orchestration playbook. `nvcc` comes with the CUDA toolkit and is only
needed to build the burn binary.

## Layout

```
validator/
  gpu_validate.py      static health check
  burn.cu              CUDA burn kernel
  burn_test.py         runs the burn, samples telemetry
  network_validate.py  DHCP / IPv6 / ICMP
  combine_report.py    merge + reconcile
  submit_report.py     POST to the gate
ansible/
  inventory.ini        fleet inventory
  playbooks/onboard.yml  the pipeline
```

## Status

Working. The gate integration is the newest piece. The pipeline runs end to end
against a local RTX 3070 and returns a PROVISION / HOLD / RMA decision through
the API.
