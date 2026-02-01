from __future__ import annotations

import os
import re
import time
import urllib.request
import subprocess
from dataclasses import dataclass
from pathlib import Path

import libvirt


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

    Docker note:
      - This process may run inside a container.
      - Libvirt/QEMU runs on the HOST.
      - Therefore, domain XML must reference HOST filesystem paths.

    Paths:
      - work_dir: where THIS process writes files (container path when in Docker)
      - host_dir: same directory as seen on HOST (set via env VMS_HOST_DIR)
    """

    def __init__(
        self,
        conn_uri: str = "qemu:///system",
        work_dir: str | Path = "vms",
        ubuntu_image_url: str = "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        base_image_name: str = "ubuntu.qcow2",
        host_dir_env: str = "VMS_HOST_DIR",
    ):
        self.conn_uri = conn_uri

        # Always absolute; prevents weird relative path behavior
        self.work_dir = Path(work_dir).expanduser().resolve()

        # Host-visible path to the same directory. Required when running inside Docker.
        host_dir_value = os.environ.get(host_dir_env)
        self.host_dir = (
            Path(host_dir_value).expanduser().resolve()
            if host_dir_value
            else self.work_dir
        )

        self.ubuntu_image_url = ubuntu_image_url

        # Container-view directories (where files are created)
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
        """
        Creates overlay qcow2 disk for the VM.

        CRITICAL: backing file path MUST be absolute, otherwise qemu-img treats it relative
        to the overlay location and you get:
          vms/instances/<vm>/vms/images/ubuntu.qcow2
        """
        self.ensure_directories()
        vm_dir = self.container_vm_dir(cfg)
        vm_dir.mkdir(parents=True, exist_ok=True)

        base = self.base_image_path.resolve()  # ABSOLUTE PATH
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
                "-b", str(base),          # ABSOLUTE backing file
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
        """
        Builds seed.iso from configs/user-data and configs/meta-data using cloud-localds.
        """
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
        """
        Stop and undefine domain if present.
        """
        try:
            dom = self.conn.lookupByName(name)
        except libvirt.libvirtError:
            # silent - normal when domain does not exist
            return

        if dom.isActive():
            print(f"*** Stopping domain '{name}'")
            dom.destroy()

        print(f"*** Undefining domain '{name}'")
        dom.undefine()

    # ---------- domain XML ----------

    def render_domain_xml(self, cfg: VmConfig) -> str:
        """
        Domain XML MUST reference host paths (host_dir) because qemu runs on host.
        """
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
        """
        Full pipeline:
          - download base image
          - ensure network active
          - optional recreate (destroy existing)
          - create overlay disk
          - build seed.iso
          - define + start domain
        """
        self.ensure_directories()
        self.download_base_image()
        self.ensure_network_active(cfg.network_name)

        if recreate:
            self.destroy_domain(cfg.name)

        # Create files in work_dir (container view)
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
