#!/usr/bin/env python3
"""Network validation, DHCP, IPv6, ICMP readiness for a GPU node.

A production node is useless if it can't talk to the network. Before a GPU
node onboards, we verify the three data-center basics:

  * DHCP , did the node get a lease, and is it valid / non-link-local?
  * IPv6 , is link-local up, and is there a routable (SLAAC/global) address?
  * ICMP , can we reach the default gateway (and an external host) over ping?

Runs entirely from the standard library (no root, no external deps).

Exit codes:
  0 -> PASS   (all network checks green)
  1 -> FAIL   (a hard requirement failed, e.g. no gateway reachability)
  2 -> ERROR  (could not run)
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import subprocess
import sys
from pathlib import Path


def is_link_local_v4(ip: str) -> bool:
    return ip.startswith("169.254.")


def is_link_local_v6(ip: str) -> bool:
    return ip.lower().startswith("fe80:")


def get_interface_addresses():
    """Return (ipv4_list, ipv6_list) for the primary interface.

    We discover the outbound interface by connecting a UDP socket (no packets
    are actually sent), the standard trick to find the address that owns the
    default route.
    """
    ipv4, ipv6 = [], []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        if local_ip and not is_link_local_v4(local_ip):
            ipv4.append(local_ip)
    except OSError:
        pass

    # IPv6: enumerate via /proc/net/if_inet6 (no root needed)
    try:
        for line in Path("/proc/net/if_inet6").read_text().splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            addr = parts[0]
            # /proc gives 32 hex chars (16 bytes), no colons, insert them
            groups = [addr[i:i + 4] for i in range(0, 32, 4)]
            ip6 = ":".join(groups)
            if ip6 == "::1":  # skip loopback
                continue
            ipv6.append(ip6)
    except OSError:
        pass
    return ipv4, ipv6


def dhcp_check():
    """DHCP: is there a valid (non-link-local) IPv4 lease?

    A 169.254.x.x address means DHCP failed and the host fell back to
    link-local, a red flag for a node meant to join the fleet.
    """
    ipv4, _ = get_interface_addresses()
    if not ipv4:
        return ("FAIL", "no IPv4 address", "no lease at all")
    ip = ipv4[0]
    if is_link_local_v4(ip):
        return ("FAIL", ip, "link-local only - DHCP lease missing")
    return ("PASS", ip, "valid lease")


def ipv6_check():
    """IPv6: link-local must be present; a global (SLAAC) address is ideal."""
    _, ipv6 = get_interface_addresses()
    link_local = [a for a in ipv6 if is_link_local_v6(a)]
    global_addrs = [a for a in ipv6 if not is_link_local_v6(a)]
    if not link_local:
        return ("FAIL", "none", "no IPv6 link-local")
    if global_addrs:
        return ("PASS", global_addrs[0], "global + link-local present")
    return ("WARN", link_local[0], "link-local only, no global/SLAAC address")


def icmp_check(target: str = "8.8.8.8"):
    """ICMP: can we reach the default gateway and an external host?

    Uses ping (IPv4). Datacenter nodes must reach their gateway; external
    reachability confirms routing/NAT end-to-end.
    """
    gateway = _default_gateway()
    results = {}
    for label, host in (("gateway", gateway), ("external", target)):
        if not host:
            results[label] = ("N/A", "no gateway discovered")
            continue
        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "2", host],
                               capture_output=True, text=True, timeout=5)
            ok = r.returncode == 0
            results[label] = ("PASS" if ok else "FAIL", host, "reachable" if ok else "no reply")
        except (subprocess.TimeoutExpired, OSError) as e:
            results[label] = ("FAIL", host, f"ping error: {e}")
    return results


def _default_gateway():
    """Read the default gateway from /proc/net/route (no root needed)."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            parts = line.split()
            if parts[1] == "00000000":  # destination 0.0.0.0 = default route
                gw_hex = parts[2]
                gw = socket.inet_ntoa(struct.pack("<L", int(gw_hex, 16)))
                return gw
    except (OSError, ValueError, IndexError):
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="GPU node network validation")
    ap.add_argument("--external", default="8.8.8.8", help="external ICMP target")
    ap.add_argument("--json", type=Path, help="write report to file")
    args = ap.parse_args()

    print("\n=== Network Validation ===\n")

    checks = []

    st, val, note = dhcp_check()
    checks.append({"name": "dhcp", "value": val, "status": st, "detail": note})

    st, val, note = ipv6_check()
    checks.append({"name": "ipv6", "value": val, "status": st, "detail": note})

    for label, (st, host, note) in icmp_check(args.external).items():
        checks.append({"name": f"icmp_{label}", "value": host,
                       "status": st, "detail": note})

    for c in checks:
        print(f"  {c['status']:4} {c['name']:12} {c['value']}")
        if c["detail"]:
            print(f"       -> {c['detail']}")

    statuses = [c["status"] for c in checks]
    verdict = "FAIL" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "PASS")
    print(f"\n  VERDICT: {verdict}\n")

    if args.json:
        args.json.write_text(json.dumps({"test": "network", "verdict": verdict,
                                         "checks": checks}, indent=2))
        print(f"report: {args.json}\n")

    return {"PASS": 0, "WARN": 0, "FAIL": 1}.get(verdict, 1)


if __name__ == "__main__":
    sys.exit(main())
