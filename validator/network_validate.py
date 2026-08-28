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
import ipaddress
import json
import socket
import struct
import subprocess
import sys
from pathlib import Path


def is_link_local_v4(ip: str) -> bool:
    return ip.startswith("169.254.")


def get_interface_addresses():
    """Return (ipv4_list, ipv6_list) for the primary interface.

    We discover the outbound interface by connecting a UDP socket (no packets
    are actually sent), the standard trick to find the address that owns the
    default route. IPv6 addresses are parsed with the ipaddress module so
    scope (link-local, loopback, global) is classified correctly.
    """
    ipv4 = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        if local_ip and not is_link_local_v4(local_ip):
            ipv4.append(local_ip)
    except OSError:
        pass
    return ipv4, _ipv6_addresses()


def _ipv6_addresses():
    """All IPv6 addresses on the host, parsed from /proc/net/if_inet6.

    /proc gives each address as 32 hex chars (16 bytes) with no colons. We
    convert that to an ipaddress.IPv6Address, which handles scope correctly,
    and drop loopback (::1) and the unspecified address (::).
    """
    addrs = []
    try:
        for line in Path("/proc/net/if_inet6").read_text().splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            # first token is the raw 32-hex-char address; build "::"-less form
            raw = parts[0]
            groups = [raw[i:i + 4] for i in range(0, 32, 4)]
            text = ":".join(groups)
            a = ipaddress.IPv6Address(text)
            if a.is_loopback or a.is_unspecified:
                continue
            addrs.append(a)
    except OSError:
        pass
    return addrs


def dhcp_check():
    """IPv4 addressing: is there a valid (non-link-local) address?

    A 169.254.x.x address is what a host falls back to when DHCP fails, so
    detecting link-local is the observable signal of a missing lease. This
    does not read the lease itself (a static address would also pass here);
    it checks the symptom a failed DHCP leaves behind.
    """
    ipv4, _ = get_interface_addresses()
    if not ipv4:
        return ("FAIL", "no IPv4 address", "no address at all")
    ip = ipv4[0]
    if is_link_local_v4(ip):
        return ("FAIL", ip, "link-local only, the classic DHCP-failure fallback")
    return ("PASS", ip, "valid non-link-local address")


def ipv6_check():
    """IPv6: link-local must be present; a global (SLAAC) address is ideal."""
    _, ipv6 = get_interface_addresses()
    link_local = [a for a in ipv6 if a.is_link_local]
    global_addrs = [a for a in ipv6 if a.is_global]
    if not link_local:
        return ("FAIL", "none", "no IPv6 link-local")
    if global_addrs:
        return ("PASS", str(global_addrs[0]), "global + link-local present")
    return ("WARN", str(link_local[0]), "link-local only, no global/SLAAC address")


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
