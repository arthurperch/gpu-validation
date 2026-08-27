#!/usr/bin/env bash
# GPU validation lab — status board.
# Auto-detects what's built + LocalStack state, then prints your progress
# checklist from PROGRESS.md (which YOU edit). Run anytime:  ./status.sh
set -u

LAB="${GPU_LAB:-$HOME/lab/gpu-validation}"
V="$LAB/validator"
GATE="$HOME/lab/cloud/labs/04-gpu-gate"

# --- live build detection ------------------------------------------------
detect() { [ -f "$1" ] && echo "x" || echo " "; }
b_gpu=$(detect "$V/gpu_validate.py")
b_burn=$(detect "$V/burn_test.py")
b_burnbin=$(detect "$LAB/artifacts/burn")
b_net=$(detect "$V/network_validate.py")
b_combine=$(detect "$V/combine_report.py")
b_submit=$(detect "$V/submit_report.py")
b_ansible=$(detect "$LAB/ansible/playbooks/onboard.yml")
b_gate=$(detect "$GATE/main.tf")

if curl -sf localhost:4566/_localstack/health >/dev/null 2>&1; then
  ls_health="up  (localhost:4566)"
  b_ls="x"
else
  ls_health="down (start with: cd ~/lab/cloud && cloud up)"
  b_ls=" "
fi

echo "═══════════════════════════════════════════════════════════"
echo "  GPU VALIDATION LAB — STATUS"
echo "═══════════════════════════════════════════════════════════"
echo
echo "  BUILD (auto-detected)"
echo "    [$b_gpu] gpu_validate.py        NVML health check"
echo "    [$b_burn] burn.cu + burn_test.py  CUDA burn under load"
echo "    [$b_burnbin]   compiled burn binary (artifacts/burn)"
echo "    [$b_net] network_validate.py    DHCP / IPv6 / ICMP"
echo "    [$b_combine] combine_report.py    merge + reconcile"
echo "    [$b_submit] submit_report.py      POST to gate"
echo "    [$b_ansible] ansible/onboard.yml   5-stage pipeline"
echo "    [$b_gate] 04-gpu-gate           Lambda + DynamoDB + API GW"
echo "    [$b_ls] LocalStack              $ls_health"
echo
echo "  ═════════════════════════════════════════════════════════"
echo

# --- your manual checklist (edit PROGRESS.md to tick boxes) --------------
if [ -f "$LAB/PROGRESS.md" ]; then
  sed -e 's/^/  /' "$LAB/PROGRESS.md"
else
  echo "  (no PROGRESS.md yet)"
fi

echo
echo "  RUN THE FULL PIPELINE"
echo "    cd ~/lab/gpu-validation"
echo "    source ~/lab/cloud/env/localstack.env"
echo "    ansible-playbook -i ansible/inventory.ini ansible/playbooks/onboard.yml"
echo
echo "═══════════════════════════════════════════════════════════"
