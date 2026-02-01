from __future__ import annotations

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
    Управляет VM через libvirt: download cloud image -> overlay disk -> cloud-init seed -> create/start -> wait IP.
    """

    def __init__(
        self,
        conn_uri: str = "qemu:///system",
        work_dir: Path | str = "vms",
        ubuntu_image_url: str = "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        base_image_name: str = "ubuntu.qcow2",
    ):
        self.conn_uri = conn_uri
        self.work_dir = Path(work_dir).resolve()
        self.ubuntu_image_url = ubuntu_image_url

        self.images_dir = self.work_dir / "images"
        self.configs_dir = self.work_dir / "configs"
        self.base_image_path = self.images_dir / base_image_name

        self._conn: libvirt.virConnect | None = None

    # ---------- Connection lifecycle ----------

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

    # ---------- Paths per VM ----------

    def vm_dir(self, cfg: VmConfig) -> Path:
        # Удобно хранить диски/seed по папкам VM
        return (self.work_dir / "instances" / cfg.name).resolve()

    def overlay_disk_path(self, cfg: VmConfig) -> Path:
        return self.vm_dir(cfg) / "disk.qcow2"

    def seed_iso_path(self, cfg: VmConfig) -> Path:
        return self.vm_dir(cfg) / "seed.iso"

    # ---------- Provision steps ----------

    def ensure_directories(self, cfg: VmConfig) -> None:
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        self.vm_dir(cfg).mkdir(parents=True, exist_ok=True)

    def download_base_image(self) -> Path:
        """
        Скачивает Ubuntu cloud image (qcow2/img) один раз.
        """
        self.images_dir.mkdir(parents=True, exist_ok=True)
        if not self.base_image_path.exists():
            print(f"*** Downloading base image -> {self.base_image_path}")
            urllib.request.urlretrieve(self.ubuntu_image_url, self.base_image_path)
        return self.base_image_path

    def create_overlay_disk(self, cfg: VmConfig) -> Path:
        """
        Создаёт overlay qcow2 на основе base image.
        """
        self.ensure_directories(cfg)
        base = self.base_image_path.resolve()
        overlay = self.overlay_disk_path(cfg)

        if overlay.exists():
            return overlay

        size = f"{cfg.disk_size_gb}G"
        print(f"*** Creating overlay disk -> {overlay} (size {size})")

        subprocess.run(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(base),
                str(overlay),
                size,
            ],
            check=True,
        )
        return overlay

    def build_cloud_init_seed(self, cfg: VmConfig) -> Path:
        """
        Собирает seed.iso из user-data/meta-data (NoCloud) через cloud-localds.
        Требует установленный cloud-localds (cloud-image-utils).
        """
        self.ensure_directories(cfg)
        user_data = self.configs_dir / "user-data"
        meta_data = self.configs_dir / "meta-data"

        if not user_data.exists():
            raise FileNotFoundError(f"Missing {user_data}. Create it first.")
        if not meta_data.exists():
            raise FileNotFoundError(f"Missing {meta_data}. Create it first.")

        seed = self.seed_iso_path(cfg)
        print(f"*** Building cloud-init seed -> {seed}")

        subprocess.run(
            [
                "cloud-localds",
                str(seed),
                str(user_data),
                str(meta_data),
            ],
            check=True,
        )
        return seed

    # ---------- Libvirt helpers ----------

    def ensure_network_active(self, network_name: str) -> None:
        net = self.conn.networkLookupByName(network_name)
        if net is None:
            raise RuntimeError(f"libvirt network '{network_name}' not found")

        if net.isActive() == 0:
            print(f"*** Network '{network_name}' inactive -> starting")
            net.create()

        if net.autostart() == 0:
            net.setAutostart(1)

    def domain_exists(self, name: str) -> bool:
        try:
            self.conn.lookupByName(name)
            return True
        except libvirt.libvirtError:
            return False

    def destroy_domain(self, name: str) -> None:
        """
        Останавливает и удаляет домен (define) если существует.
        """
        try:
            dom = self.conn.lookupByName(name)
        except libvirt.libvirtError:
            print(f"*** Domain '{name}' not found, nothing to delete")
            return

        if dom.isActive():
            print(f"*** Stopping domain '{name}'")
            dom.destroy()

        print(f"*** Undefining domain '{name}'")
        dom.undefine()

    # ---------- VM create/start ----------

    def render_domain_xml(self, cfg: VmConfig, overlay: Path, seed_iso: Path) -> str:
        """
        Рендерит XML домена. (минимально необходимое)
        """
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
      <source file='{overlay}'/>
      <target dev='vda' bus='virtio'/>
    </disk>

    <disk type='file' device='cdrom'>
      <source file='{seed_iso}'/>
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

    def create_and_start(self, cfg: VmConfig, recreate: bool = False) -> libvirt.virDomain:
        """
        Полный пайплайн:
        - download base image (если нет)
        - ensure network active
        - create overlay (если нет)
        - build seed.iso
        - define+start domain
        """
        self.ensure_directories(cfg)
        self.download_base_image()
        self.ensure_network_active(cfg.network_name)

        if recreate and self.domain_exists(cfg.name):
            self.destroy_domain(cfg.name)

        overlay = self.create_overlay_disk(cfg)
        seed = self.build_cloud_init_seed(cfg)

        xml = self.render_domain_xml(cfg, overlay, seed)

        print(f"*** Defining domain '{cfg.name}'")
        dom = self.conn.defineXML(xml)

        print(f"*** Starting domain '{cfg.name}'")
        dom.create()

        return dom

    # ---------- Wait for IP (default network) ----------

    def wait_for_ip(self, cfg: VmConfig, timeout_s: int = 120) -> str:
        """
        Получает IP через DHCP leases в libvirt network (подходит для 'default' NAT).
        """
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
