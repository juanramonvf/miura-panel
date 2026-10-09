#!/usr/bin/env python3
import json
import grp
import ipaddress
import tempfile
import threading
import urllib.parse
import os
import re
import socket
import subprocess
import shutil
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

CONFIG_FILE = Path("/etc/server-dashboard.conf")


def load_dashboard_config():
    config = {}
    try:
        for raw in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return config


_CONFIG = load_dashboard_config()

BIND_IP = _CONFIG.get("DASHBOARD_BIND_IP", "0.0.0.0")
try:
    PORT = int(_CONFIG.get("DASHBOARD_PORT", "8766"))
except ValueError:
    PORT = 8766

WEB_DIR = Path("/var/www/server-dashboard")
ACTION_SOCKET = "/run/server-dashboard/docker-action.sock"
PREF_DIR = Path("/var/lib/server-dashboard/preferences")
PREF_FILE = PREF_DIR / "ui.json"
PREF_LOCK = threading.Lock()
ADMIN_MODE = False
ADMIN_ACTIVE = Path("/var/www/server-dashboard/admin-status.json")


def validate_servers(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("Lista de servidores inválida")
    output, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Registro de servidor inválido")
        ip = str(item.get("host", "")).strip()
        try:
            if ipaddress.ip_address(ip).version != 4:
                raise ValueError()
        except ValueError:
            raise ValueError("La dirección debe ser IPv4") from None
        port = item.get("port")
        if type(port) is not int or port < 1 or port > 65535:
            raise ValueError("Puerto inválido")
        label = item.get("label", "")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 65 or any(ord(c) < 32 for c in label):
            raise ValueError("Nombre inválido")
        if (ip, port) not in seen:
            secure = item.get("secure", False)
            if type(secure) is not bool:
                raise ValueError("Protocolo inválido")
            # La marca "principal" es solo metadato visual, nunca concede privilegios.
            primary = item.get("primary", False)
            if type(primary) is not bool:
                raise ValueError("La marca principal debe ser booleana")
            output.append({"host": ip, "port": port, "label": label.strip(), "secure": secure,
                           "primary": primary})
            seen.add((ip, port))
    return output


def validate_aliases(value):
    if not isinstance(value, dict) or len(value) > 160:
        raise ValueError("Alias inválidos")
    out = {}
    for key, label in value.items():
        if (not isinstance(key, str) or len(key) > 256
                or not key.startswith(("mount:", "fs:", "emmc:"))
                or any(ord(c) < 32 for c in key)):
            raise ValueError("Disco inválido")
        if not isinstance(label, str) or len(label) > 65 or any(ord(c) < 32 for c in label):
            raise ValueError("Nombre de disco inválido")
        if label.strip():
            out[key] = label.strip()
    return out


def read_prefs():
    try:
        obj = json.loads(PREF_FILE.read_text(encoding="utf-8"))
        return {"servers": validate_servers(obj.get("servers", [])),
                "disk_aliases": validate_aliases(obj.get("disk_aliases", {}))}
    except (OSError, ValueError, TypeError, KeyError):
        return {"servers": [], "disk_aliases": {}}


def write_prefs(obj):
    PREF_DIR.mkdir(mode=0o770, parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".ui-", suffix=".tmp", dir=PREF_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o660)
            os.fchown(f.fileno(), -1, grp.getgrnam("server-dashboard").gr_gid)
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(path, PREF_FILE)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def is_own_origin(headers):
    # Sin autenticación: las escrituras se limitan al origen exacto por HTTP.
    # Rechazar nombres DNS evita ataques de DNS rebinding contra IP privadas.
    host = headers.get("Host", "")
    try:
        parsed = urllib.parse.urlsplit("http://" + host)
        ip = ipaddress.ip_address(parsed.hostname or "")
        valid_host = (ip.version == 4 and parsed.port == PORT and
                      (BIND_IP == "0.0.0.0" or str(ip) == BIND_IP))
    except ValueError:
        valid_host = False
    origin = headers.get("Origin", "")
    fetch_site = headers.get("Sec-Fetch-Site", "same-origin")
    return bool(valid_host and origin == "http://" + host and
                fetch_site in ("same-origin", "none") and
                headers.get("X-SEP-Preferences") == "1")


_last_net = None
_last_net_ts = None


def temps():
    cpu = None
    nvme = None
    try:
        groups = psutil.sensors_temperatures(fahrenheit=False) or {}
        core = groups.get("coretemp", [])
        package = [x for x in core if (x.label or "").lower().startswith("package")]
        src = package or core
        if src:
            cpu = round(float(src[0].current if package else max(x.current for x in src)), 1)
        entries = []
        for name, vals in groups.items():
            if name.lower().startswith("nvme"):
                entries.extend(vals)
        preferred = [x for x in entries if (x.label or "").lower() in ("composite", "")]
        src = preferred or entries
        if src:
            nvme = round(float(src[0].current), 1)
    except Exception:
        pass
    return cpu, nvme


def default_iface():
    try:
        with open("/proc/net/route", encoding="ascii", errors="ignore") as f:
            next(f, None)
            for line in f:
                p = line.split()
                if len(p) >= 4 and p[1] == "00000000" and int(p[3], 16) & 2:
                    return p[0]
    except Exception:
        pass
    return None


_disk_label_cache = {}
_disk_id_cache = {}


def disk_stable_key(device, mountpoint):
    if device in _disk_id_cache:
        return _disk_id_cache[device]
    result = ''
    lsblk = shutil.which('lsblk')
    if lsblk:
        try:
            p = subprocess.run([lsblk, '-dn', '-o', 'UUID', device],
                               capture_output=True, text=True, timeout=3, check=False)
            uid = (p.stdout or '').strip().splitlines()
            if p.returncode == 0 and uid and uid[0].strip():
                result = 'fs:' + uid[0].strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not result:
        result = 'mount:' + mountpoint
    _disk_id_cache[device] = result
    return result


def disk_label(device, mountpoint):
    if mountpoint == "/":
        return "Sistema"
    if device in _disk_label_cache:
        return _disk_label_cache[device]

    label = None
    lsblk = shutil.which("lsblk")
    if lsblk:
        try:
            p = subprocess.run(
                [lsblk, "-dn", "-o", "LABEL", device],
                text=True, capture_output=True, timeout=3, check=False,
            )
            candidate = (p.stdout or "").strip().splitlines()
            if p.returncode == 0 and candidate and candidate[0].strip():
                label = candidate[0].strip()
        except Exception:
            pass

    if not label:
        clean = mountpoint.rstrip("/")
        if clean and clean not in ("/boot", "/boot/efi"):
            base = os.path.basename(clean)
            if base and not base.startswith("dev-disk-by-"):
                label = base

    if not label:
        label = device

    _disk_label_cache[device] = label
    return label


_disk_model_cache = {}


def disk_model(device):
    """Modelo y fabricante del dispositivo real (no de su partición).

    No depende de smartctl: funciona también si SMART está desactivado.
    """
    if device in _disk_model_cache:
        return _disk_model_cache[device]

    model = ""
    vendor = ""
    lsblk = shutil.which("lsblk")
    target = device

    if lsblk:
        try:
            parent = subprocess.run(
                [lsblk, "-dn", "-o", "PKNAME", device],
                text=True, capture_output=True, timeout=3, check=False,
            )
            names = (parent.stdout or "").strip().splitlines()
            if parent.returncode == 0 and names and names[0].strip():
                target = "/dev/" + names[0].strip()

            for field in ("MODEL", "VENDOR"):
                result = subprocess.run(
                    [lsblk, "-dn", "-o", field, target],
                    text=True, capture_output=True, timeout=3, check=False,
                )
                if result.returncode == 0:
                    value = (result.stdout or "").strip()
                    if field == "MODEL":
                        model = value
                    else:
                        vendor = value
        except Exception:
            pass

    # Los dispositivos USB pueden identificar al fabricante mediante udev.
    # No se lee ni se muestra el número de serie.
    if not model or not vendor:
        udevadm = shutil.which("udevadm")
        if udevadm:
            try:
                p = subprocess.run(
                    [udevadm, "info", "--query=property", "--name", target],
                    text=True, capture_output=True, timeout=3, check=False,
                )
                if p.returncode == 0:
                    props = dict(line.split("=", 1) for line in p.stdout.splitlines()
                                 if "=" in line)
                    model = model or props.get("ID_MODEL", "")
                    vendor = vendor or props.get("ID_VENDOR", "") or props.get("ID_VENDOR_FROM_DATABASE", "")
            except Exception:
                pass

    # Los SSD Crucial de consumo no siempre anuncian VENDOR en NVMe.
    # Inferimos la marca únicamente para sus patrones de modelo conocidos.
    if not vendor and re.fullmatch(
        r"CT[0-9]+(?:P[0-9]+P?SSD[0-9]*|MX[0-9]+SSD[0-9]*|BX[0-9]+SSD[0-9]*)",
        model, flags=re.IGNORECASE,
    ):
        vendor = "Crucial"

    vendor = vendor.strip()
    model = model.strip()
    if vendor and model and not model.casefold().startswith(vendor.casefold()):
        label = f"{vendor} {model}"
    else:
        label = model or vendor or None
    _disk_model_cache[device] = label
    return label


def disk_rows():
    rows = []
    mounts_seen = set()
    devices_seen = set()
    for p in psutil.disk_partitions(all=False):
        if (not p.device.startswith("/dev/")
                or p.device.startswith("/dev/loop")
                or p.fstype == "squashfs"
                or p.mountpoint == "/boot/efi"):
            continue
        if p.mountpoint in mounts_seen or p.device in devices_seen:
            continue
        mounts_seen.add(p.mountpoint)
        devices_seen.add(p.device)
        try:
            u = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue
        rows.append({
            "device": p.device,
            "stable_key": disk_stable_key(p.device, p.mountpoint),
            "label": disk_label(p.device, p.mountpoint),
            "model": disk_model(p.device),
            "mount": p.mountpoint,
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "percent": round(float(u.percent), 1),
        })
    rows.sort(key=lambda x: (x["mount"] != "/", x["label"].casefold(), x["device"]))
    return rows


def live_payload():
    global _last_net, _last_net_ts
    cpu = round(float(psutil.cpu_percent(interval=0.12)), 1)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    uptime_seconds = max(0, int(time.time() - psutil.boot_time()))
    cpu_temp, nvme_temp = temps()
    load1, load5, load15 = os.getloadavg()
    iface = default_iface()
    now = time.time()
    rx_mbps = 0.0
    tx_mbps = 0.0
    counters = psutil.net_io_counters(pernic=True)
    current = counters.get(iface) if iface else None
    if current is not None and _last_net is not None and _last_net_ts is not None:
        elapsed = max(0.1, now - _last_net_ts)
        if current.bytes_recv >= _last_net.bytes_recv:
            rx_mbps = round((current.bytes_recv - _last_net.bytes_recv) * 8 / elapsed / 1_000_000, 3)
        if current.bytes_sent >= _last_net.bytes_sent:
            tx_mbps = round((current.bytes_sent - _last_net.bytes_sent) * 8 / elapsed / 1_000_000, 3)
    _last_net = current
    _last_net_ts = now
    return {
        "ts": now,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cpu": cpu,
        "ram": round(float(mem.percent), 1),
        "ram_used": mem.used,
        "ram_total": mem.total,
        "swap_total": swap.total,
        "swap_used": swap.used,
        "swap_percent": round(float(swap.percent), 1) if swap.total else 0.0,
        "uptime_seconds": uptime_seconds,
        "cpu_temp": cpu_temp,
        "nvme_temp": nvme_temp,
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "iface": iface,
        "rx_mbps": rx_mbps,
        "tx_mbps": tx_mbps,
        "disks": disk_rows(),
    }


def broker(req):
    data = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(ACTION_SOCKET)
        s.sendall(data)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
            if b"\n" in b:
                break
    return json.loads(b"".join(chunks).decode("utf-8").strip())


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/preferences":
            with PREF_LOCK:
                obj = read_prefs()
            self.send_json(200, obj)
            return
        if self.path.split("?", 1)[0] == "/api/live":
            body = json.dumps(live_payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # SEGURIDAD SEP 1.0.14: HTTP siempre es solo lectura, con o sin TLS
        # configurado. Ninguna petición HTTP anónima puede actualizar Docker
        # ni alterar preferencias persistentes del servidor.
        if not ADMIN_MODE:
            self.send_json(403, {"ok": False, "error": "Cambios disponibles únicamente con inicio de sesión HTTPS"})
            return
        if self.path == "/api/preferences":
            if not is_own_origin(self.headers) or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.send_json(403, {"ok": False, "error": "Origen no autorizado"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768:
                    raise ValueError("Tamaño inválido")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict) or len(data) != 2 or data.get("type") not in ("servers", "disk_aliases"):
                    raise ValueError("Operación inválida")
                with PREF_LOCK:
                    obj = read_prefs()
                    if data["type"] == "servers":
                        obj["servers"] = validate_servers(data.get("value"))
                    else:
                        obj["disk_aliases"] = validate_aliases(data.get("value"))
                    write_prefs(obj)
                self.send_json(200, {"ok": True})
            except (ValueError, TypeError, OSError) as e:
                self.send_json(400, {"ok": False, "error": str(e)})
            return
        if self.path != "/api/docker-action":
            self.send_error(404)
            return
        if self.headers.get("X-Dashboard-Action") != "1":
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                raise ValueError("Tamaño de petición inválido")
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            resp = broker(req)
            code = 200 if resp.get("ok") else 400
        except Exception as e:
            resp = {"ok": False, "error": str(e)}
            code = 500
        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    os.chdir(WEB_DIR)
    ThreadingHTTPServer((BIND_IP, PORT), Handler).serve_forever()
