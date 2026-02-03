from __future__ import annotations

import os
from flask import Flask, request, jsonify
from vm_manager import VmManager, VmConfig

# NEW:
from portmap import load_state as portmap_load_state, save_state as portmap_save_state
from portmap import apply_rule as portmap_apply_rule, delete_rule as portmap_delete_rule, restore_all as portmap_restore_all

app = Flask(__name__)

WORK_DIR = os.environ.get("VMS_WORK_DIR", "/srv/vms")
CONN_URI = os.environ.get("LIBVIRT_URI", "qemu:///system")
DEFAULT_NET = os.environ.get("VMS_NETWORK", "default")
DEFAULT_ARCH = os.environ.get("VMS_ARCH", "x86_64")

# NEW:
PORTMAP_TOKEN = os.environ.get("PORTMAP_TOKEN", "").strip()

def get_mgr() -> VmManager:
    mgr = VmManager(conn_uri=CONN_URI, work_dir=WORK_DIR)
    mgr.connect()
    return mgr

def err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code

def require_portmap_token():
    if not PORTMAP_TOKEN:
        return err("PORTMAP_TOKEN not set", 500)
    token = request.headers.get("X-Portmap-Token", "")
    if token != PORTMAP_TOKEN:
        return err("forbidden", 403)
    return None

@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.post("/vms")
def create_vm():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    if not name:
        return err("Missing field: name", 400)

    cfg = VmConfig(
        name=name,
        memory_mib=int(data.get("memory_mib", 1024)),
        vcpus=int(data.get("vcpus", 1)),
        disk_size_gb=int(data.get("disk_size_gb", 10)),
        network_name=str(data.get("network_name", DEFAULT_NET)),
        os_arch=str(data.get("os_arch", DEFAULT_ARCH)),
    )
    recreate = bool(data.get("recreate", False))

    try:
        mgr = get_mgr()
        dom = mgr.create_and_start(cfg, recreate=recreate)
        return jsonify({"ok": True, "name": dom.name()})
    except Exception as e:
        return err(str(e), 500)

@app.delete("/vms/<name>")
def delete_vm(name: str):
    delete_files = request.args.get("delete_files", "true").lower() in ("1", "true", "yes")
    try:
        mgr = get_mgr()
        cfg = VmConfig(name=name, network_name=DEFAULT_NET, os_arch=DEFAULT_ARCH)
        mgr.delete_vm(cfg, delete_files=delete_files)
        return jsonify({"ok": True, "name": name, "deleted_files": delete_files})
    except Exception as e:
        return err(str(e), 500)

@app.post("/vms/<name>/start")
def start_vm(name: str):
    try:
        mgr = get_mgr()
        mgr.start_vm(name)
        return jsonify({"ok": True, "name": name})
    except KeyError:
        return err("VM not found", 404)
    except Exception as e:
        return err(str(e), 500)

@app.post("/vms/<name>/stop")
def stop_vm(name: str):
    try:
        mgr = get_mgr()
        mgr.stop_vm(name)
        return jsonify({"ok": True, "name": name})
    except KeyError:
        return err("VM not found", 404)
    except Exception as e:
        return err(str(e), 500)

@app.get("/vms/<name>/status")
def status_vm(name: str):
    try:
        mgr = get_mgr()
        st = mgr.status_vm(name)
        return jsonify({"ok": True, "status": st})
    except KeyError:
        return err("VM not found", 404)
    except Exception as e:
        return err(str(e), 500)

@app.get("/vms/<name>/ip")
def get_ip(name: str):
    timeout = int(request.args.get("timeout", "120"))
    network_name = request.args.get("network", DEFAULT_NET)

    try:
        mgr = get_mgr()
        cfg = VmConfig(name=name, network_name=network_name, os_arch=DEFAULT_ARCH)
        ip = mgr.wait_for_ip(cfg, timeout_s=timeout)
        return jsonify({"ok": True, "name": name, "ip": ip})
    except TimeoutError as e:
        return err(str(e), 504)
    except Exception as e:
        return err(str(e), 500)


# ==========================
#   NEW: Port mapping API
# ==========================

@app.post("/ports")
def create_port():
    auth_err = require_portmap_token()
    if auth_err:
        return auth_err

    data = request.get_json(force=True, silent=True) or {}
    listen_port = int(data.get("listen_port", 0))
    target_ip = str(data.get("target_ip", "")).strip()
    target_port = int(data.get("target_port", 0))
    proto = str(data.get("proto", "tcp")).lower()

    if listen_port <= 0 or target_port <= 0 or not target_ip:
        return err("listen_port, target_ip, target_port required", 400)
    if proto != "tcp":
        return err("only tcp supported", 400)

    try:
        portmap_apply_rule(listen_port, target_ip, target_port, proto)  # idempotent

        st = portmap_load_state()
        rid = str(listen_port)
        st.setdefault("rules", {})[rid] = {
            "listen_port": listen_port,
            "target_ip": target_ip,
            "target_port": target_port,
            "proto": proto,
        }
        portmap_save_state(st)

        return jsonify({"ok": True, "rule_id": rid})
    except Exception as e:
        return err(str(e), 500)

@app.delete("/ports/<int:listen_port>")
def delete_port(listen_port: int):
    auth_err = require_portmap_token()
    if auth_err:
        return auth_err

    st = portmap_load_state()
    rid = str(listen_port)
    rule = st.get("rules", {}).get(rid)
    if not rule:
        return jsonify({"ok": True, "deleted": False})

    try:
        portmap_delete_rule(int(rule["listen_port"]), rule["target_ip"], int(rule["target_port"]), rule.get("proto", "tcp"))
        st["rules"].pop(rid, None)
        portmap_save_state(st)
        return jsonify({"ok": True, "deleted": True, "rule_id": rid})
    except Exception as e:
        return err(str(e), 500)

@app.get("/ports")
def list_ports():
    auth_err = require_portmap_token()
    if auth_err:
        return auth_err
    return jsonify({"ok": True, "rules": portmap_load_state().get("rules", {})})

@app.post("/ports/restore")
def restore_ports():
    auth_err = require_portmap_token()
    if auth_err:
        return auth_err
    try:
        n = portmap_restore_all()
        return jsonify({"ok": True, "restored": n})
    except Exception as e:
        return err(str(e), 500)
