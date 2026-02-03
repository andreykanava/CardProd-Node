#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
WG_IFACE="${WG_IFACE:-wg0}"

CONTROLLER_URL="${CONTROLLER_URL:-}"     # http://controller_public_ip:9000
JOIN_TOKEN="${JOIN_TOKEN:-}"
NODE_ID="${NODE_ID:-}"
NODE_API_PORT="${NODE_API_PORT:-8000}"

if [ -z "$CONTROLLER_URL" ] || [ -z "$JOIN_TOKEN" ] || [ -z "$NODE_ID" ]; then
  echo "Missing env: CONTROLLER_URL, JOIN_TOKEN, NODE_ID"
  exit 1
fi

mkdir -p "$DATA_DIR" /etc/wireguard

KEY_PRIV="$DATA_DIR/node.key"
KEY_PUB="$DATA_DIR/node.pub"
JOIN_FILE="$DATA_DIR/join.json"

# ----- keys -----
if [ ! -f "$KEY_PRIV" ] || [ ! -f "$KEY_PUB" ]; then
  echo "[*] Generating WireGuard keypair..."
  umask 077
  wg genkey | tee "$KEY_PRIV" | wg pubkey > "$KEY_PUB"
fi

NODE_PUBKEY="$(cat "$KEY_PUB")"
export NODE_PUBKEY

# ----- join (ALWAYS on start, idempotent) -----
echo "[*] Joining controller..."
python3 - <<'PY'
import json, os, time, requests, sys

url = os.environ["CONTROLLER_URL"].rstrip("/") + "/join"
hdr = {"X-Join-Token": os.environ["JOIN_TOKEN"]}
payload = {"node_id": os.environ["NODE_ID"], "node_pubkey": os.environ["NODE_PUBKEY"]}

# retries: controller might be up but still booting wg/gunicorn
for attempt in range(1, 11):
    try:
        r = requests.post(url, json=payload, headers=hdr, timeout=20)
        if r.status_code >= 400:
            print(r.text, file=sys.stderr)
            r.raise_for_status()
        open(os.environ.get("JOIN_FILE", "/data/join.json"), "w").write(r.text)
        print("[*] Join OK")
        break
    except Exception as e:
        print(f"[!] join attempt {attempt}/10 failed: {e}", file=sys.stderr)
        time.sleep(2)
else:
    raise SystemExit("Join failed after retries")
PY

# pass join file path to python
export JOIN_FILE="$JOIN_FILE"

NODE_IP="$(python3 -c 'import json; print(json.load(open("/data/join.json"))["node_ip"])')"
CTRL_PUB="$(python3 -c 'import json; print(json.load(open("/data/join.json"))["controller_pubkey"])')"
ENDPOINT="$(python3 -c 'import json; print(json.load(open("/data/join.json"))["endpoint"])')"
ALLOWED="$(python3 -c 'import json; print(json.load(open("/data/join.json"))["allowed_ips"])')"

if [ -z "$ENDPOINT" ] || [ "$ENDPOINT" = "None" ]; then
  echo "Controller did not provide WG_ENDPOINT. Set WG_ENDPOINT on controller (PUBLIC_IP:51820)."
  exit 1
fi

PRIVKEY="$(cat "$KEY_PRIV")"

# ----- write wg config -----
cat > "/etc/wireguard/${WG_IFACE}.conf" <<EOF
[Interface]
Address = ${NODE_IP}/32
PrivateKey = ${PRIVKEY}

[Peer]
PublicKey = ${CTRL_PUB}
Endpoint = ${ENDPOINT}
AllowedIPs = ${ALLOWED}
PersistentKeepalive = 25
EOF

# ----- bring up wg -----
echo "[*] Bringing up WireGuard..."
wg-quick down "${WG_IFACE}" >/dev/null 2>&1 || true
wg-quick up "${WG_IFACE}"

# Optional: quick sanity output
echo "[*] wg show:"
wg show "${WG_IFACE}" || true

# после wg show ...
echo "[*] Restoring portmap rules (best-effort)..."
python3 - <<'PY' || true
import os
try:
    from portmap import restore_all
    n = restore_all()
    print(f"[*] Portmap restored: {n}")
except Exception as e:
    print(f"[!] Portmap restore failed (ignored): {e}")
PY


# ----- start API -----
echo "[*] Starting node VM API on ${NODE_IP}:${NODE_API_PORT}"

# IMPORTANT: bind 0.0.0.0 so it listens on wg0 too (and any container iface)
# If you really want strict bind to WG only, change to: -b "${NODE_IP}:${NODE_API_PORT}"
exec gunicorn -w 1 --threads 4 --timeout 120 \
  -b "0.0.0.0:${NODE_API_PORT}" \
  app:app
