# Node VM Agent (WireGuard + Libvirt)

Node-side agent that joins a WireGuard controller, exposes an API for managing **libvirt/QEMU VMs**, and supports **TCP port forwarding** to VMs via iptables with persistent rule storage.

Designed to run on an edge node as part of a private mesh network.

---

## Features

* Auto-join WireGuard controller on startup (idempotent)
* Generates and persists WireGuard keys in `DATA_DIR`
* Exposes Flask API (Gunicorn) for:

  * VM lifecycle: create / start / stop / delete
  * VM status + DHCP IP discovery
  * TCP port mapping: node port → VM IP:port (iptables DNAT/MASQUERADE)
* Persistent portmap state (`/data/portmap.json`)
* Restores portmap rules on reboot/start
* Uses `python3-libvirt` from apt (`--system-site-packages` venv)

---

## Architecture

```
Controller (WG + API)
        │
        │  /join  (token)
        ▼
Node Agent container
  - wg0 up
  - libvirt client (qemu:///system)
  - Flask API (8000)
  - iptables port mapping
        │
        ▼
Libvirt network (default -> virbr0)
        │
        ▼
VMs (cloud-image + cloud-init)
```

---

## Requirements

Host must provide:

* `libvirtd` running on the host
* QEMU/KVM support (recommended)
* Docker with access to host libvirt socket (common setup: mount `/var/run/libvirt`)
* NET_ADMIN capability in the container (iptables + wg)

The container installs:

* `python3-libvirt` (system module)
* `wireguard-tools`, `iptables`, `iproute2`
* `qemu-img` and `cloud-localds` (via `qemu-utils`, `cloud-image-utils`)

---

## Quick Start

### 1) Build

```bash
docker build -t node-vm-agent .
```

### 2) Run (example)

You typically mount host libvirt socket and persistent data:

```bash
docker run -d \
  --name node-vm-agent \
  --cap-add=NET_ADMIN \
  --cap-add=SYS_MODULE \
  --sysctl net.ipv4.ip_forward=1 \
  -p 8000:8000 \
  -e DATA_DIR=/data \
  -e CONTROLLER_URL="http://<controller_public_ip>:9000" \
  -e JOIN_TOKEN="<join_token>" \
  -e NODE_ID="node-1" \
  -v node_agent_data:/data \
  -v /var/run/libvirt:/var/run/libvirt \
  -v /lib/modules:/lib/modules:ro \
  node-vm-agent
```

> If your libvirt connection requires a different socket/path, adjust mounts + `LIBVIRT_URI`.

---

## Environment Variables

### WireGuard / Join

| Variable         | Description                                      |
| ---------------- | ------------------------------------------------ |
| `CONTROLLER_URL` | Controller API base URL (`http://x.x.x.x:9000`)  |
| `JOIN_TOKEN`     | Join token (sent as `X-Join-Token`)              |
| `NODE_ID`        | Unique node identifier                           |
| `WG_IFACE`       | WireGuard interface name (default: `wg0`)        |
| `DATA_DIR`       | Persistent dir for keys/state (default: `/data`) |
| `NODE_API_PORT`  | API port exposed by agent (default: `8000`)      |

### Libvirt / VM settings

| Variable       | Description                                              |
| -------------- | -------------------------------------------------------- |
| `LIBVIRT_URI`  | Libvirt URI (default: `qemu:///system`)                  |
| `VMS_WORK_DIR` | Working directory inside container (default: `/srv/vms`) |
| `VMS_HOST_DIR` | Same directory as seen on host (used in domain XML)      |
| `VMS_NETWORK`  | Libvirt network name (default: `default`)                |
| `VMS_ARCH`     | VM arch for domain XML (default: `x86_64`)               |

### Port mapping

| Variable         | Description                                                 |
| ---------------- | ----------------------------------------------------------- |
| `VMS_BRIDGE`     | Libvirt bridge interface (default: `virbr0`)                |
| `VM_SUBNET`      | VM subnet for NAT (default: `192.168.122.0/24`)             |
| `EXTERNAL_IFACE` | External iface for VM internet NAT (auto-detect by default) |
| `PORTMAP_TOKEN`  | Optional API protection token (currently disabled in code)  |

---

## API Reference

Base URL: `http://<node_ip_or_host>:8000`

### Health

```http
GET /health
```

---

## VM Management

### Create VM

```http
POST /vms
Content-Type: application/json
```

Example:

```json
{
  "name": "vm1",
  "memory_mib": 1024,
  "vcpus": 1,
  "disk_size_gb": 10,
  "network_name": "default",
  "os_arch": "x86_64",
  "recreate": false
}
```

Returns:

```json
{ "ok": true, "name": "vm1" }
```

---

### Start VM

```http
POST /vms/<name>/start
```

### Stop VM

```http
POST /vms/<name>/stop
```

### VM Status

```http
GET /vms/<name>/status
```

### Delete VM

```http
DELETE /vms/<name>?delete_files=true
```

### Get VM IP (DHCP lease lookup)

```http
GET /vms/<name>/ip?timeout=120&network=default
```

---

## Port Mapping (Node → VM)

Port forwarding is implemented with iptables:

* DNAT in `nat/PREROUTING`
* FORWARD accept rules
* MASQUERADE to VM bridge (so VM replies return properly)
* MASQUERADE on `wg0` (important for multi-hop proxy chains)

### Create Port Map

```http
POST /ports
Content-Type: application/json
```

Example:

```json
{
  "listen_port": 8080,
  "target_ip": "192.168.122.50",
  "target_port": 80,
  "proto": "tcp"
}
```

### List Port Maps

```http
GET /ports
```

### Delete Port Map

```http
DELETE /ports/<listen_port>
```

### Restore Port Maps

```http
POST /ports/restore
```

---

## Persistence

### WireGuard keys

Stored in:

* `/data/node.key`
* `/data/node.pub`

### Join response cache

* `/data/join.json`

### Portmap state

* `/data/portmap.json`

---

## Startup Flow

1. Generate WG keys (if missing)
2. `POST /join` to controller (retries included)
3. Write `/etc/wireguard/wg0.conf`
4. `wg-quick up wg0`
5. Restore portmap rules (best-effort)
6. Start Gunicorn Flask API

---

## Notes / Gotchas

* `VMS_HOST_DIR` must match the path visible on the host if you want libvirt domain XML to point to correct disk/seed paths.
* This container expects to talk to host libvirt (usually via `/var/run/libvirt` mount).
* Port mapping currently supports **TCP only**.
* `PORTMAP_TOKEN` auth is present but commented out in code (`require_portmap_token()`).
