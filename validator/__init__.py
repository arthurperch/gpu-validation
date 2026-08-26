"""GPU Production Validation Suite — syndrax infrastructure gate.

Validates a GPU node's readiness for production onboarding. Mirrors datacenter
GPU validation methodology (NVML telemetry, PCIe link integrity, thermal/power
envelope, ECC, RMA evidence collection) so syndrax nodes only enter production
once they pass.

Runs against the local GPU by default (RTX 3070) and degrades gracefully on
consumer silicon (ECC reports N/A). Designed to be driven by Ansible (see
ansible/playbooks/validate.yml) as one step in an onboarding pipeline.

Exit codes:
  0 -> PASS    (node production-ready)
  1 -> FAIL    (validation failed; RMA evidence bundle written)
  2 -> ERROR   (validator could not run: no NVML / no GPU)
"""
