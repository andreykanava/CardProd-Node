from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Literal

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
PORTMAP_FILE = DATA_DIR / "portmap.json"

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

def load_state() -> dict:
    if not PORTMAP_FILE.exists():
        return {"rules": {}}
    return json.loads(PORTMAP_FILE.read_text())

def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PORTMAP_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))

def apply_rule(listen_port: int, target_ip: str, target_port: int, proto: Literal["tcp"] = "tcp") -> None:
    # DNAT: node:<listen_port> -> <target_ip>:<target_port>
    preroute = [
        "-p", proto,
        "--dport", str(listen_port),
        "-j", "DNAT",
        "--to-destination", f"{target_ip}:{target_port}",
    ]
    _iptables_add_unique("nat", "PREROUTING", preroute)

    # allow forward to VM
    forward = [
        "-p", proto,
        "-d", target_ip,
        "--dport", str(target_port),
        "-j", "ACCEPT",
    ]
    _iptables_add_unique("filter", "FORWARD", forward)

def delete_rule(listen_port: int, target_ip: str, target_port: int, proto: Literal["tcp"] = "tcp") -> None:
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

def restore_all() -> int:
    st = load_state()
    rules = st.get("rules", {})
    n = 0
    for _rid, r in rules.items():
        apply_rule(int(r["listen_port"]), r["target_ip"], int(r["target_port"]), r.get("proto", "tcp"))
        n += 1
    return n
