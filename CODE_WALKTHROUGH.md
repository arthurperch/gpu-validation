# How the code works

A plain walkthrough of every file in this repo. If you want to understand the
whole thing, read this top to bottom with the code open next to it.

## The shape of the system

There are five small Python programs and one C program, plus an Ansible
playbook. Each Python program does one job and writes a JSON file. That's it.
No framework, no database on the node side, nothing hidden.

The flow is:

```
gpu_validate.py   ->  health.json
burn_test.py      ->  burn.json
network_validate.py -> network.json
combine_report.py ->  combined.json   (merges the three above)
submit_report.py  ->  POSTs combined.json to the gate
```

Ansible calls them in that order. The gate is a separate repo.

---

## gpu_validate.py

This is the main health check. It reads every sensor the GPU exposes and turns
each reading into a `Check` object, then prints a verdict.

### The thresholds

At the top is a dictionary called `THRESHOLDS`. This is the rulebook.

```python
THRESHOLDS = {
    "temp_c_max": 85.0,
    "power_pct_max": 95.0,
    "min_pcie_gen": 3,
    "min_pcie_width": 8,
    "ecc_uncorrectable_max": 0,
}
```

Each key is a name, each value is the number a check compares against. If you
want to change the temperature limit, you change `85.0` here and nothing else.
That's the point of keeping all the rules in one place.

### The Check class

A `Check` is just a little container holding four things: a name, a value, a
status (`PASS`, `WARN`, `FAIL`, or `N/A`), and a detail string. The class has
one method, `as_dict`, which turns it into a plain dictionary so it can be
dumped to JSON later.

### collect()

`collect(handle)` is the core of the file. `handle` is a reference to the GPU
that NVML hands you after init. The function walks through each sensor, reads
it, compares against a threshold, and appends a `Check` to a list.

The temperature check is the simplest example of the pattern:

```python
temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
st = "PASS" if temp <= THRESHOLDS["temp_c_max"] else "FAIL"
```

Read the sensor, compare to the rule, label it. Every other check is a fancier
version of those two lines.

The PCIe check is the most interesting one, because it's the only check that
uses `WARN` deliberately:

```python
if not ok_gen or not ok_w:
    st = "WARN"
    detail += ", downshifted; verify under bandwidth load (burn test)"
else:
    st = "PASS"
```

The reason is subtle and important. An idle GPU downshifts its PCIe link to
save power. So a low link speed at idle is not a defect, it's normal behavior.
Only a load test can tell them apart. So this check says "I can't prove the
link is bad from here, go run the burn test" instead of failing outright.

The ECC check is wrapped in a try/except because consumer cards don't have ECC
counters. Calling the ECC function on an RTX 3070 raises an error, which we
catch and report as `N/A`. On a datacenter card the same code would return
real numbers.

### verdict()

Takes the list of checks and returns the worst one. If any check is `FAIL`,
the verdict is `FAIL`. Otherwise if any is `WARN`, it's `WARN`. Otherwise
`PASS`. Three lines of logic.

### collect_rma_evidence()

This is the answer to "why save files instead of just printing FAIL?". When the
verdict is `FAIL`, this function writes three files into a folder:

- `nvidia-smi.txt`, the full `nvidia-smi -q` dump
- `dmesg.txt`, the last 200 lines of the kernel log
- `report.json`, the structured checks

The reason is that "the GPU is broken" is not evidence. A vendor doing an RMA
wants proof: the hardware snapshot, the kernel log, and the structured results.
So the tool captures all three the moment the failure happens, before anyone
has a chance to reboot the box and lose the state.

### main()

The driver. It parses command line arguments, inits NVML, gets the GPU handle,
calls `collect`, calls `verdict`, prints the report, writes the RMA bundle if
needed, and writes the JSON if asked. The `return` value is the exit code: 0
for pass, 1 for fail, 2 for error. That's what lets Ansible act on the result.

---

## burn.cu and burn_test.py

The health check reads a card at rest. A card at rest can hide problems. The
burn test is what makes a weak card reveal itself.

### burn.cu

This is a small CUDA program that runs the GPU hard for a set number of
seconds. It has one kernel, `burn_kernel`, that does a tight loop of fused
multiply-add operations. Each thread does a few hundred FMAs, which keeps every
streaming multiprocessor busy.

The main loop alternates between two phases:

- a compute phase, where it launches the kernel sixteen times back to back
- a bandwidth phase, where it copies the buffer to the host and back

The compute phase pushes clocks up and power toward the limit. The bandwidth
phase pushes the PCIe link up to its max generation. The two are separated
because if you copy between every kernel launch, the GPU sits idle waiting on
the PCIe bus and never actually stresses the thermal envelope. That was a real
bug I hit and fixed; the first version only drew 74 watts on a 240 watt card,
and separating the phases took it to 159.

### burn_test.py

This is the Python side. It launches `burn.cu` in the background, then samples
NVML every half second while it runs. By the end it has a timeline of
temperature, clock, power, PCIe state, and utilization readings.

Then it asks four questions of that timeline:

- did the PCIe link ramp to its max generation?
- did the clocks reach boost?
- did power draw climb?
- did temperature stay under the limit?

Each becomes a PASS or WARN or FAIL. The whole test fails if the link didn't
ramp or the temperature breached; it warns if the clocks or power stayed low.

The exit code again: 0 pass, 1 fail, 2 error.

---

## network_validate.py

The simplest file, and the easiest one to read if you're learning Python. It
has no GPU code at all, just the standard library.

It checks three things the job cares about:

- DHCP. It looks up the interface's IPv4 address. If the address is in the
  `169.254.x.x` range, that means DHCP failed and the box fell back to a
  link-local address, which is a red flag.
- IPv6. It reads `/proc/net/if_inet6` (a Linux kernel file, no root needed) and
  checks for a link-local address, which should always be there, and ideally a
  global address.
- ICMP. It pings the default gateway and an external host. The gateway is read
  from `/proc/net/route`, another kernel file.

Each check returns a tuple of `(status, value, note)`, and `main()` assembles
them into a list and prints them.

The reason this file matters: a GPU node that can't reach the network is dead
weight, no matter how healthy the GPU is. So network readiness is checked
before the node is allowed to onboard.

---

## combine_report.py

The three validators write three separate JSON files. The gate wants one
picture. This file merges them.

The interesting part is the `reconcile` function. Remember the health check
warns on a downshifted PCIe link because it can't prove the link is bad. But
the burn test *can* prove it, because it watched the link ramp up under load.
So `reconcile` does this: if the health check said `WARN` on PCIe, and the burn
test saw the link reach its max generation, it upgrades that `WARN` to `PASS`.

That's the reconciliation. The static check is conservative. The dynamic check
is authoritative. When they disagree, the dynamic check wins.

The rest of the file turns the burn results into checks (peak temp, max clock,
max power, PCIe under load, max utilization), appends the network checks, and
computes the overall verdict the same way `gpu_validate.py` does.

---

## submit_report.py

The smallest file. It reads a JSON report and POSTs it to the gate endpoint
over HTTP, then prints the decision the gate sends back.

It uses `urllib`, the standard library HTTP client, so there are no third party
dependencies. The `--endpoint` argument is the API URL, which in the LocalStack
setup looks like `http://localhost:4566/restapis/.../prod/_user_request_/validate`.

---

## ansible/playbooks/onboard.yml

The pipeline that ties everything together. It runs five steps in order:

1. compile the burn binary if it's missing
2. run `gpu_validate.py`
3. run `burn_test.py`
4. run `network_validate.py`
5. combine and submit

Each step uses `ignore_errors: true` because a failed check should not stop the
pipeline. You want all the checks to run and report, and let the gate make the
final call, not have Ansible bail out halfway through.

The playbook targets `gpu-node-01` in the inventory, which is set to
`localhost` with a local connection. To validate a rented cloud GPU, you change
that one line in the inventory to point at the remote host. The playbook itself
doesn't change. That's the whole reason for using Ansible here: the pipeline
is identical whether the node is under your desk or in a datacenter.

---

## What's not in this repo

The gate. It lives in a separate repo because it deploys to a different place.
The validators run on the GPU node. The gate runs in the cloud (or LocalStack
for local dev). Keeping them separate means you can deploy the node side to a
hundred machines and the gate side once, without coupling them.
