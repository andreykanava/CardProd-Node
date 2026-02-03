FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# System deps:
# - python3-libvirt: дает модуль `libvirt` (не ставим libvirt-python через pip)
# - wireguard-tools/iproute2/iptables: поднимать wg0 и маршруты
# - qemu-utils/cloud-image-utils: qemu-img + cloud-localds
# - python3-venv: чтобы создать venv
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    python3-libvirt \
    wireguard-tools iproute2 iptables ca-certificates \
    qemu-utils cloud-image-utils \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (venv, но с доступом к системным site-packages чтобы видеть libvirt)
COPY requirements.txt .
RUN python3 -m venv --system-site-packages /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/venv/bin:$PATH"

# App files
COPY app.py vm_manager.py portmap.py ./
COPY entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

ENV DATA_DIR=/data
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
