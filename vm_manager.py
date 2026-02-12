from __future__ import annotations

import os
import re
import time
import urllib.request
import subprocess
from dataclasses import dataclass
from pathlib import Path

import libvirt
import shutil


@dataclass(frozen=True)
class VmConfig:
    name: str = "testvm"
    memory_mib: int = 1024
    vcpus: int = 1
    disk_size_gb: int = 10
    network_name: str = "default"
    os_arch: str = "x86_64"


class VmManager:
    """
    Libvirt VM manager with Ubuntu cloud-image + cloud-init (NoCloud).

    Paths:
      - work_dir: where THIS process writes files
      - host_dir: same directory as seen on HOST (set via env VMS_HOST_DIR)
    """

    def __init__(
        self,
        conn_uri: str = "qemu:///system",
        work_dir: str | Path = "vms",
        ubuntu_image_url: str = "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        base_image_name: str = "ubuntu.qcow2",
        host_dir_env: str = "VMS_HOST_DIR",
        base_dir: Path | None = None
    ):
        self.conn_uri = conn_uri
        base_dir = base_dir or Path(__file__).resolve().parent  # папка файла, не cwd
        self.work_dir = (base_dir / work_dir).resolve()

        host_dir_value = os.environ.get(host_dir_env)
        self.host_dir = (
            Path(host_dir_value).expanduser().resolve()
            if host_dir_value
            else self.work_dir
        )

        self.ubuntu_image_url = ubuntu_image_url

        self.images_dir = self.work_dir / "images"
        self.configs_dir = self.work_dir / "configs"
        self.instances_dir = self.work_dir / "instances"

        self.base_image_path = self.images_dir / base_image_name

        self._conn: libvirt.virConnect | None = None

    # ---------- connection ----------

    def connect(self) -> libvirt.virConnect:
        conn = libvirt.open(self.conn_uri)
        if conn is None:
            raise RuntimeError(f"Failed to connect to libvirt: {self.conn_uri}")
        self._conn = conn
        return conn

    @property
    def conn(self) -> libvirt.virConnect:
        if self._conn is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._conn

    # ---------- directories ----------

    def ensure_directories(self) -> None:
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        self.instances_dir.mkdir(parents=True, exist_ok=True)

    # ---------- VM instance paths (container view) ----------

    def container_vm_dir(self, cfg: VmConfig) -> Path:
        return self.instances_dir / cfg.name

    def container_overlay_path(self, cfg: VmConfig) -> Path:
        return self.container_vm_dir(cfg) / "disk.qcow2"

    def container_seed_path(self, cfg: VmConfig) -> Path:
        return self.container_vm_dir(cfg) / "seed.iso"

    # ---------- VM instance paths (host view) used in XML ----------

    def host_vm_dir(self, cfg: VmConfig) -> Path:
        return self.host_dir / "instances" / cfg.name

    def host_overlay_path(self, cfg: VmConfig) -> Path:
        return self.host_vm_dir(cfg) / "disk.qcow2"

    def host_seed_path(self, cfg: VmConfig) -> Path:
        return self.host_vm_dir(cfg) / "seed.iso"

    # ---------- base image ----------

    def download_base_image(self) -> Path:
        self.ensure_directories()
        if not self.base_image_path.exists():
            print(f"*** Downloading base image -> {self.base_image_path}")
            urllib.request.urlretrieve(self.ubuntu_image_url, self.base_image_path)
        return self.base_image_path

    # ---------- overlay disk ----------

    def create_overlay_disk(self, cfg: VmConfig) -> Path:
        self.ensure_directories()
        vm_dir = self.container_vm_dir(cfg)
        vm_dir.mkdir(parents=True, exist_ok=True)

        base = self.base_image_path.resolve()
        overlay = self.container_overlay_path(cfg)

        if overlay.exists():
            return overlay

        if not base.exists():
            raise FileNotFoundError(f"Base image not found: {base}")

        size = f"{cfg.disk_size_gb}G"
        print(f"*** Creating overlay disk -> {overlay} (size {size})")

        subprocess.run(
            [
                "qemu-img", "create",
                "-f", "qcow2",
                "-F", "qcow2",
                "-b", str(base),
                str(overlay),
                size,
            ],
            check=True,
        )

        if not overlay.exists():
            raise RuntimeError(f"Overlay disk was not created: {overlay}")

        return overlay

    # ---------- cloud-init seed ----------

    def build_cloud_init_seed(self, cfg: VmConfig) -> Path:
        self.ensure_directories()
        vm_dir = self.container_vm_dir(cfg)
        vm_dir.mkdir(parents=True, exist_ok=True)

        user_data = self.configs_dir / "user-data"
        meta_data = self.configs_dir / "meta-data"

        if not user_data.exists():
            raise FileNotFoundError(f"Missing {user_data}")
        if not meta_data.exists():
            raise FileNotFoundError(f"Missing {meta_data}")

        seed = self.container_seed_path(cfg)
        print(f"*** Building cloud-init seed -> {seed}")

        subprocess.run(
            ["cloud-localds", str(seed), str(user_data), str(meta_data)],
            check=True,
        )

        if not seed.exists():
            raise RuntimeError(f"seed.iso was not created: {seed}")

        return seed

    # ---------- libvirt network ----------

    def ensure_network_active(self, network_name: str) -> None:
        net = self.conn.networkLookupByName(network_name)
        if net is None:
            raise RuntimeError(f"libvirt network '{network_name}' not found")

        if net.isActive() == 0:
            print(f"*** Network '{network_name}' inactive -> starting")
            net.create()

        if net.autostart() == 0:
            net.setAutostart(1)

    # ---------- domain lifecycle ----------

    def domain_exists(self, name: str) -> bool:
        try:
            self.conn.lookupByName(name)
            return True
        except libvirt.libvirtError:
            return False

    def destroy_domain(self, name: str) -> None:
        try:
            dom = self.conn.lookupByName(name)
        except libvirt.libvirtError:
            return

        if dom.isActive():
            print(f"*** Stopping domain '{name}'")
            dom.destroy()

        print(f"*** Undefining domain '{name}'")
        dom.undefine()

    # ---------- domain XML ----------

    def render_domain_xml(self, cfg: VmConfig) -> str:
        host_overlay = self.host_overlay_path(cfg)
        host_seed = self.host_seed_path(cfg)

        return f"""
<domain type='kvm'>
  <name>{cfg.name}</name>
  <memory unit='MiB'>{cfg.memory_mib}</memory>
  <vcpu>{cfg.vcpus}</vcpu>

  <os>
    <type arch='{cfg.os_arch}'>hvm</type>
  </os>

  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{host_overlay}'/>
      <target dev='vda' bus='virtio'/>
    </disk>

    <disk type='file' device='cdrom'>
      <source file='{host_seed}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>

    <interface type='network'>
      <source network='{cfg.network_name}'/>
      <model type='virtio'/>
    </interface>

    <graphics type='spice' autoport='yes'/>
  </devices>
</domain>
""".strip()

    # ---------- create & start ----------

    def create_and_start(self, cfg: VmConfig, recreate: bool = False) -> libvirt.virDomain:
        self.ensure_directories()
        self.download_base_image()
        self.ensure_network_active(cfg.network_name)

        if recreate:
            self.destroy_domain(cfg.name)

        self.create_overlay_disk(cfg)
        self.build_cloud_init_seed(cfg)

        xml = self.render_domain_xml(cfg)

        print(f"*** Defining domain '{cfg.name}'")
        dom = self.conn.defineXML(xml)

        print(f"*** Starting domain '{cfg.name}'")
        dom.create()

        return dom

    # ---------- wait for IP ----------

    def wait_for_ip(self, cfg: VmConfig, timeout_s: int = 120) -> str:
        dom = self.conn.lookupByName(cfg.name)
        mac = self._get_domain_mac(dom)
        if not mac:
            raise RuntimeError("Failed to detect domain MAC address")

        net = self.conn.networkLookupByName(cfg.network_name)
        deadline = time.time() + timeout_s

        while time.time() < deadline:
            for lease in net.DHCPLeases():
                if lease.get("mac", "").lower() == mac:
                    ip = lease.get("ipaddr")
                    if ip:
                        return ip
            time.sleep(1)

        raise TimeoutError(f"IP not acquired within {timeout_s} seconds")

    @staticmethod
    def _get_domain_mac(dom: libvirt.virDomain) -> str | None:
        xml = dom.XMLDesc(0)
        m = re.search(r"<mac address=['\"]([^'\"]+)['\"]", xml)
        return m.group(1).lower() if m else None

    # =======================
    #   ДОП. МЕТОДЫ ДЛЯ API
    # =======================

    def get_domain(self, name: str) -> libvirt.virDomain:
        try:
            return self.conn.lookupByName(name)
        except libvirt.libvirtError as e:
            raise KeyError(f"Domain not found: {name}") from e

    def start_vm(self, name: str) -> None:
        dom = self.get_domain(name)
        if dom.isActive() == 0:
            dom.create()

    def stop_vm(self, name: str) -> None:
        dom = self.get_domain(name)
        if dom.isActive() == 1:
            dom.destroy()

    def status_vm(self, name: str) -> dict:
        dom = self.get_domain(name)
        is_active = dom.isActive() == 1

        state, _reason = dom.state()
        state_map = {
            libvirt.VIR_DOMAIN_NOSTATE: "nostate",
            libvirt.VIR_DOMAIN_RUNNING: "running",
            libvirt.VIR_DOMAIN_BLOCKED: "blocked",
            libvirt.VIR_DOMAIN_PAUSED: "paused",
            libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
            libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
            libvirt.VIR_DOMAIN_CRASHED: "crashed",
            libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended",
        }

        return {
            "name": dom.name(),
            "active": is_active,
            "state": state_map.get(state, str(state)),
            "uuid": dom.UUIDString(),
        }

    def delete_vm(self, cfg: VmConfig, delete_files: bool = True) -> None:
        # 1) destroy + undefine
        self.destroy_domain(cfg.name)

        # 2) delete instance files
        if delete_files:
            vm_dir = self.container_vm_dir(cfg)
            if vm_dir.exists():
                for p in sorted(vm_dir.rglob("*"), reverse=True):
                    if p.is_file():
                        p.unlink(missing_ok=True)
                    elif p.is_dir():
                        try:
                            p.rmdir()
                        except OSError:
                            pass
                try:
                    vm_dir.rmdir()
                except OSError:
                    pass



    def list_domains(self) -> list[dict]:
        """
        Return basic info about all domains on this libvirt host.
        """
        domains = []
        for dom in self.conn.listAllDomains(0):
            try:
                state, _ = dom.state()
                active = dom.isActive() == 1
                domains.append({
                    "name": dom.name(),
                    "uuid": dom.UUIDString(),
                    "active": active,
                    "state_code": int(state),
                })
            except Exception:
                # don't let one broken VM kill the whole list
                continue
        return domains

    def host_stats(self) -> dict:
        """
        Minimal host stats for scheduling.
        """
        # CPU cores (logical)
        try:
            cores = os.cpu_count() or 0
        except Exception:
            cores = 0

        # Loadavg
        try:
            load1, load5, load15 = os.getloadavg()
        except Exception:
            load1, load5, load15 = (0.0, 0.0, 0.0)

        # RAM (from /proc/meminfo)
        mem_total_kb = 0
        mem_avail_kb = 0
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        mem_avail_kb = int(line.split()[1])
        except Exception:
            pass

        total_mb = mem_total_kb // 1024
        free_mb = mem_avail_kb // 1024

        # Disk free for WORK_DIR partition
        try:
            usage = shutil.disk_usage(self.work_dir)
            disk_total_gb = int(usage.total // (1024**3))
            disk_free_gb = int(usage.free // (1024**3))
        except Exception:
            disk_total_gb = 0
            disk_free_gb = 0

        # VM counts
        try:
            vms = self.list_domains()
            running = sum(1 for d in vms if d.get("active"))
            total = len(vms)
        except Exception:
            running = 0
            total = 0

        return {
            "cpu": {"cores": int(cores), "load1": float(load1), "load5": float(load5), "load15": float(load15)},
            "ram": {"total_mb": int(total_mb), "free_mb": int(free_mb)},
            "disk": {"total_gb": int(disk_total_gb), "free_gb": int(disk_free_gb)},
            "vms": {"total": int(total), "running": int(running)},
        }