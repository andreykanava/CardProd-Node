from __future__ import annotations

import ipaddress
import json
import os
import subprocess
from pathlib import Path
from typing import Literal, Optional

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
PORTMAP_FILE = DATA_DIR / "portmap.json"

# libvirt default bridge; override if you use a different network
VM_BRIDGE = os.environ.get("VMS_BRIDGE", "virbr0")
WG_IFACE = os.environ.get("WG_IFACE", "wg0")

# NAT for VM internet access
VM_SUBNET = os.environ.get("VM_SUBNET", "192.168.122.0/24")
EXTERNAL_IFACE = os.environ.get("EXTERNAL_IFACE", None)  # None = auto detect

Proto = Literal["tcp"]


def _sh_ok(*args: str) -> None:
    subprocess.run(list(args), check=True)


def _iptables_rule_exists(table: str, chain: str, rule_parts: list[str]) -> bool:
    try:
        _sh_ok("iptables", "-t", table, "-C", chain, *rule_parts)
        return True
    except subprocess.CalledProcessError:
        return False


def _iptables_add_unique(table: str, chain: str, rule_parts: list[str]) -> None:
    if not _iptables_rule_exists(table, chain, rule_parts):
        _sh_ok("iptables", "-t", table, "-A", chain, *rule_parts)


def _iptables_del_if_exists(table: str, chain: str, rule_parts: list[str]) -> None:
    if _iptables_rule_exists(table, chain, rule_parts):
        _sh_ok("iptables", "-t", table, "-D", chain, *rule_parts)


def _ensure_forwarding_enabled() -> None:
    try:
        _sh_ok("sysctl", "-w", "net.ipv4.ip_forward=1")
    except Exception:
        pass


def _validate(listen_port: int, target_ip: str, target_port: int, proto: Proto) -> None:
    if proto != "tcp":
        raise ValueError("only tcp supported for now")
    if not (1 <= int(listen_port) <= 65535):
        raise ValueError("listen_port out of range")
    if not (1 <= int(target_port) <= 65535):
        raise ValueError("target_port out of range")
    ipaddress.ip_address(target_ip)


def load_state() -> dict:
    if not PORTMAP_FILE.exists():
        return {"rules": {}}
    return json.loads(PORTMAP_FILE.read_text())


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PORTMAP_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def _get_default_iface() -> str:
    """Return the interface used for the default route."""
    try:
        out = subprocess.check_output(["ip", "route", "show", "default"], text=True)
        parts = out.split()
        if "dev" in parts:
            idx = parts.index("dev")
            return parts[idx + 1]
    except Exception:
        pass
    return "eth0"  # fallback


def _ensure_wg_masquerade(wg_iface: str) -> None:
    """
    IMPORTANT for multi-hop:
    Make sure replies go back via WG (proxy/controller), not via default route.
    This is global and should exist once.
    """
    _iptables_add_unique("nat", "POSTROUTING", ["-o", wg_iface, "-j", "MASQUERADE"])


def _maybe_cleanup_wg_masquerade(wg_iface: str) -> None:
    """
    Optional cleanup: remove wg masquerade only if no portmap rules exist.
    Safer to keep it always if this host is always behind WG.
    """
    st = load_state()
    if st.get("rules"):
        return
    _iptables_del_if_exists("nat", "POSTROUTING", ["-o", wg_iface, "-j", "MASQUERADE"])


def _ensure_vm_nat(bridge: str) -> None:
    """
    Ensure that traffic from the VM subnet can reach the internet.
    Adds MASQUERADE on the external interface for the whole VM subnet.
    """
    subnet = VM_SUBNET
    iface = EXTERNAL_IFACE or _get_default_iface()
    if not iface:
        # Cannot determine external interface – skip
        return

    rule = ["-s", subnet, "-o", iface, "-j", "MASQUERADE"]
    if not _iptables_rule_exists("nat", "POSTROUTING", rule):
        _sh_ok("iptables", "-t", "nat", "-I", "POSTROUTING", *rule)


def _maybe_cleanup_vm_nat(bridge: str) -> None:
    """
    Remove the global VM MASQUERADE rule when no port‑forwarding rules remain.
    """
    st = load_state()
    if st.get("rules"):
        return
    subnet = VM_SUBNET
    iface = EXTERNAL_IFACE or _get_default_iface()
    if not iface:
        return
    rule = ["-s", subnet, "-o", iface, "-j", "MASQUERADE"]
    _iptables_del_if_exists("nat", "POSTROUTING", rule)


def apply_rule(
    listen_port: int,
    target_ip: str,
    target_port: int,
    proto: Proto = "tcp",
    vm_bridge: Optional[str] = None,
    wg_iface: Optional[str] = None,
) -> None:
    """
    Port forward on the *node*:
      node:<listen_port>  ->  target_ip:<target_port>
    Typical target_ip is a VM in libvirt net (192.168.122.x).
    """
    _validate(listen_port, target_ip, target_port, proto)
    _ensure_forwarding_enabled()

    br = (vm_bridge or VM_BRIDGE).strip() or "virbr0"
    wg = (wg_iface or WG_IFACE).strip() or "wg0"

    # 0) make sure WG SNAT exists (critical for your proxy chain)
    _ensure_wg_masquerade(wg)

    # 0a) ensure VM subnet can reach internet
    _ensure_vm_nat(br)

    # 1) DNAT incoming packets to the VM
    preroute = [
        "-p", proto,
        "--dport", str(listen_port),
        "-j", "DNAT",
        "--to-destination", f"{target_ip}:{target_port}",
    ]
    _iptables_add_unique("nat", "PREROUTING", preroute)

    # 2) Allow forward to VM + allow return traffic
    _iptables_add_unique("filter", "FORWARD", [
        "-m", "conntrack",
        "--ctstate", "RELATED,ESTABLISHED",
        "-j", "ACCEPT",
    ])

    forward = [
        "-p", proto,
        "-d", target_ip,
        "--dport", str(target_port),
        "-j", "ACCEPT",
    ]
    _iptables_add_unique("filter", "FORWARD", forward)

    # 3) MASQUERADE towards libvirt bridge so VM replies go back to node
    postroute_vm = [
        "-o", br,
        "-p", proto,
        "-d", target_ip,
        "--dport", str(target_port),
        "-j", "MASQUERADE",
    ]
    _iptables_add_unique("nat", "POSTROUTING", postroute_vm)


def delete_rule(
    listen_port: int,
    target_ip: str,
    target_port: int,
    proto: Proto = "tcp",
    vm_bridge: Optional[str] = None,
    wg_iface: Optional[str] = None,
) -> None:
    _validate(listen_port, target_ip, target_port, proto)
    br = (vm_bridge or VM_BRIDGE).strip() or "virbr0"
    wg = (wg_iface or WG_IFACE).strip() or "wg0"

    preroute = [
        "-p", proto,
        "--dport", str(listen_port),
        "-j", "DNAT",
        "--to-destination", f"{target_ip}:{target_port}",
    ]
    _iptables_del_if_exists("nat", "PREROUTING", preroute)

    forward = [
        "-p", proto,
        "-d", target_ip,
        "--dport", str(target_port),
        "-j", "ACCEPT",
    ]
    _iptables_del_if_exists("filter", "FORWARD", forward)

    postroute_vm = [
        "-o", br,
        "-p", proto,
        "-d", target_ip,
        "--dport", str(target_port),
        "-j", "MASQUERADE",
    ]
    _iptables_del_if_exists("nat", "POSTROUTING", postroute_vm)

    # Remove global helpers if no rules left
    _maybe_cleanup_wg_masquerade(wg)
    _maybe_cleanup_vm_nat(br)


def restore_all() -> int:
    st = load_state()
    rules = st.get("rules", {})
    n = 0
    for _rid, r in rules.items():
        apply_rule(
            int(r["listen_port"]),
            str(r["target_ip"]),
            int(r["target_port"]),
            r.get("proto", "tcp"),
        )
        n += 1
    return n