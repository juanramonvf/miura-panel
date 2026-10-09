#!/usr/bin/env python3
import datetime as dt
import glob
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import psutil

WEB_DIR = Path("/var/www/server-dashboard")
STATE_DIR = Path("/var/lib/server-dashboard")
DATA_FILE = WEB_DIR / "data.json"
HISTORY_FILE = WEB_DIR / "history.json"
REPORT_FILE = STATE_DIR / "report.txt"
CONFIG_FILE = Path("/etc/server-dashboard.conf")
PUBLIC_IP_CACHE = STATE_DIR / "public-ip.json"
WEATHER_CACHE = STATE_DIR / "weather.json"

IP_BIN = shutil.which("ip") or "/usr/bin/ip"
SMARTCTL_BIN = shutil.which("smartctl")
DOCKER_BIN = shutil.which("docker")
SYSTEMCTL_BIN = shutil.which("systemctl") or "/usr/bin/systemctl"
APT_BIN = shutil.which("apt") or "/usr/bin/apt"
WG_BIN = shutil.which("wg")



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
DASHBOARD_TITLE = _CONFIG.get("DASHBOARD_TITLE") or platform.node()
DASHBOARD_CITY = _CONFIG.get("DASHBOARD_CITY", "")
DASHBOARD_DOCKER_UPDATER = _CONFIG.get("DASHBOARD_DOCKER_UPDATER", "0") == "1"
if _CONFIG.get("DASHBOARD_SMART") == "0":
    SMARTCTL_BIN = None
HISTORY_DAYS = 7
MAX_HISTORY = int(HISTORY_DAYS * 24 * 60 / 5) + 20


def run(cmd, timeout=15):
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:
        return 255, "", str(exc)


def iface_ipv4(iface):
    if not iface:
        return None
    for addr in psutil.net_if_addrs().get(iface, []):
        if getattr(addr.family, "name", "") == "AF_INET":
            ip = addr.address
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    return None


def public_ip():
    cached = None
    try:
        cached = json.loads(PUBLIC_IP_CACHE.read_text(encoding="utf-8"))
        if (
            cached.get("ip")
            and time.time() - float(cached.get("ts", 0)) < 1800
        ):
            return cached["ip"]
    except Exception:
        cached = None

    try:
        req = urllib.request.Request(
            "https://api.ipify.org",
            headers={"User-Agent": "server-dashboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=4) as r:
            ip = r.read(128).decode("ascii", errors="ignore").strip()
        ipaddress.ip_address(ip)
        PUBLIC_IP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(PUBLIC_IP_CACHE, {"ip": ip, "ts": time.time()})
        return ip
    except Exception:
        if cached and cached.get("ip"):
            return cached["ip"]
        return None



def default_route_ipv4(fallback_iface=None):
    rc, out, _ = run(
        [IP_BIN, "-4", "route", "show", "default"],
        timeout=5,
    )
    if rc == 0:
        for line in out.splitlines():
            m = re.search(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})\b", line)
            if m:
                return m.group(1)

    return iface_ipv4(fallback_iface)




def local_timezone():
    try:
        target = Path("/etc/localtime").resolve()
        prefix = Path("/usr/share/zoneinfo")
        try:
            return str(target.relative_to(prefix))
        except ValueError:
            pass
    except Exception:
        pass
    return "UTC"


def weather_description(code):
    table = {
        0: ("Despejado", "☀️"),
        1: ("Principalmente despejado", "🌤️"),
        2: ("Parcialmente nuboso", "⛅"),
        3: ("Cubierto", "☁️"),
        45: ("Niebla", "🌫️"),
        48: ("Niebla", "🌫️"),
        51: ("Llovizna", "🌦️"),
        53: ("Llovizna", "🌦️"),
        55: ("Llovizna intensa", "🌧️"),
        56: ("Llovizna helada", "🌨️"),
        57: ("Llovizna helada", "🌨️"),
        61: ("Lluvia", "🌧️"),
        63: ("Lluvia", "🌧️"),
        65: ("Lluvia intensa", "🌧️"),
        66: ("Lluvia helada", "🌨️"),
        67: ("Lluvia helada", "🌨️"),
        71: ("Nieve", "🌨️"),
        73: ("Nieve", "🌨️"),
        75: ("Nieve intensa", "❄️"),
        77: ("Nieve", "❄️"),
        80: ("Chubascos", "🌦️"),
        81: ("Chubascos", "🌧️"),
        82: ("Chubascos intensos", "⛈️"),
        85: ("Chubascos de nieve", "🌨️"),
        86: ("Chubascos de nieve", "❄️"),
        95: ("Tormenta", "⛈️"),
        96: ("Tormenta con granizo", "⛈️"),
        99: ("Tormenta fuerte con granizo", "⛈️"),
    }
    return table.get(int(code), ("Tiempo actual", "🌡️"))


def weather_current():
    city_cfg = (DASHBOARD_CITY or "").strip()
    if not city_cfg:
        return None

    cached = None
    try:
        cached = json.loads(WEATHER_CACHE.read_text(encoding="utf-8"))
        if cached.get("weather") and time.time() - float(cached.get("ts", 0)) < 900:
            return cached["weather"]
    except Exception:
        cached = None

    try:
        parts = [x.strip() for x in city_cfg.split(",") if x.strip()]
        city_name = parts[0]
        region_hint = " ".join(parts[1:]).casefold()

        params = urllib.parse.urlencode({
            "name": city_name,
            "count": 10,
            "language": "es",
            "format": "json",
        })
        req = urllib.request.Request(
            "https://geocoding-api.open-meteo.com/v1/search?" + params,
            headers={"User-Agent": "server-dashboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            geo = json.loads(r.read().decode("utf-8", errors="replace"))

        results = geo.get("results") or []
        if not results:
            raise RuntimeError("Ciudad no encontrada")

        chosen = results[0]
        if region_hint:
            for item in results:
                haystack = " ".join(str(item.get(k) or "") for k in (
                    "admin1", "admin2", "admin3", "country"
                )).casefold()
                if all(word in haystack for word in region_hint.split()):
                    chosen = item
                    break

        lat = chosen["latitude"]
        lon = chosen["longitude"]

        params = urllib.parse.urlencode({
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code",
            "timezone": "auto",
        })
        req = urllib.request.Request(
            "https://api.open-meteo.com/v1/forecast?" + params,
            headers={"User-Agent": "server-dashboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            forecast = json.loads(r.read().decode("utf-8", errors="replace"))

        cur = forecast.get("current") or {}
        code = cur.get("weather_code")
        desc, icon = weather_description(code if code is not None else -1)

        weather = {
            "city": city_cfg,
            "temperature": (
                round(float(cur["temperature_2m"]), 1)
                if cur.get("temperature_2m") is not None else None
            ),
            "code": code,
            "description": desc,
            "icon": icon,
            "timezone": forecast.get("timezone"),
        }

        atomic_json(WEATHER_CACHE, {"ts": time.time(), "weather": weather})
        return weather

    except Exception:
        if cached and cached.get("weather"):
            return cached["weather"]
        return {
            "city": city_cfg,
            "temperature": None,
            "code": None,
            "description": "Sin datos",
            "icon": "🌡️",
            "timezone": None,
        }


def vpn_addresses():
    result = []
    addrs = psutil.net_if_addrs()

    wg_ifaces = set()
    if WG_BIN:
        rc, out, _ = run([WG_BIN, "show", "interfaces"], timeout=5)
    else:
        rc, out = 127, ""
    if rc == 0:
        wg_ifaces.update(out.split())

    seen = set()

    for iface, iface_addrs in addrs.items():
        low = iface.lower()
        kind = None

        if low.startswith("zt"):
            kind = "ZeroTier"
        elif low.startswith("tailscale"):
            kind = "Tailscale"
        elif iface in wg_ifaces or low.startswith("wg"):
            kind = "WireGuard"

        if not kind:
            continue

        for addr in iface_addrs:
            if getattr(addr.family, "name", "") != "AF_INET":
                continue
            ip = addr.address
            if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                continue
            key = (kind, iface, ip)
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "type": kind,
                "interface": iface,
                "ip": ip,
            })

    return result


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024.0


def human_uptime(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


def cpu_model():
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "CPU"


def temperatures():
    result = {"cpu": None, "nvme": None}
    try:
        groups = psutil.sensors_temperatures(fahrenheit=False) or {}
        core = groups.get("coretemp", [])
        preferred = [x for x in core if (x.label or "").lower().startswith("package")]
        if preferred:
            result["cpu"] = round(float(preferred[0].current), 1)
        elif core:
            result["cpu"] = round(max(float(x.current) for x in core), 1)

        nvme_entries = []
        for name, entries in groups.items():
            if name.lower().startswith("nvme"):
                nvme_entries.extend(entries)
        preferred_nvme = [x for x in nvme_entries if (x.label or "").lower() in ("composite", "")]
        src = preferred_nvme or nvme_entries
        if src:
            result["nvme"] = round(float(src[0].current), 1)
    except Exception:
        pass
    return result


def block_base(device):
    name = os.path.basename(device)
    m = re.match(r"^(nvme\d+n\d+)(?:p\d+)?$", name)
    if m:
        return m.group(1)
    m = re.match(r"^([a-z]+)(?:\d+)?$", name)
    if m:
        return m.group(1)
    return name


def disk_usage():
    rows = []
    seen = set()
    for p in psutil.disk_partitions(all=False):
        # Excluir imágenes de paquetes Snap (/dev/loop*, squashfs) y EFI.
        # No son volúmenes de datos monitorizables: squashfs está siempre al 100%.
        if (not p.device.startswith("/dev/")
                or p.device.startswith("/dev/loop")
                or p.fstype == "squashfs"
                or p.mountpoint == "/boot/efi"):
            continue
        if p.mountpoint in seen:
            continue
        seen.add(p.mountpoint)
        try:
            u = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue
        rows.append({
            "device": p.device,
            "base": block_base(p.device),
            "mount": p.mountpoint,
            "fstype": p.fstype,
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "percent": round(float(u.percent), 1),
        })
    rows.sort(key=lambda x: (x["mount"] != "/", x["mount"]))
    return rows


def smart_devices():
    if not SMARTCTL_BIN and _CONFIG.get("DASHBOARD_SMART") == "0":
        return []

    # Discos físicos detectados de forma portable: SATA/SAS/USB, NVMe, MMC, virtio, etc.
    devices = []
    lsblk = shutil.which("lsblk")
    if lsblk:
        rc, stdout, _ = run([lsblk, "-dn", "-o", "PATH,TYPE"], timeout=10)
        if rc == 0:
            for line in stdout.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[1].strip() == "disk" and parts[0].startswith("/dev/"):
                    devices.append(parts[0].strip())
    if not devices:
        devices = (
            sorted(glob.glob("/dev/sd[a-z]"))
            + sorted(glob.glob("/dev/nvme*n1"))
            + sorted(glob.glob("/dev/vd[a-z]"))
            + sorted(glob.glob("/dev/mmcblk[0-9]"))
        )

    out = []
    for dev in sorted(set(devices)):
        # La eMMC se consulta mediante sysfs (EXT_CSD expuesto por Linux):
        # no tiene interfaz ATA/NVMe SMART y sus particiones boot son parte del mismo chip.
        if re.fullmatch(r"/dev/mmcblk[0-9]+", dev):
            root = Path("/sys/block") / Path(dev).name / "device"
            def sysvalue(name):
                try:
                    return (root / name).read_text().strip()
                except (OSError, ValueError):
                    return ""
            raw_life, raw_eol = sysvalue("life_time"), sysvalue("pre_eol_info")
            name = sysvalue("name") or Path(dev).name
            bands = []
            for value in raw_life.split():
                try: bands.append(int(value, 16))
                except ValueError: continue
            usable = [v for v in bands if 1 <= v <= 11]
            level = max(usable) if usable else None
            # 0x03 = 20-30% usado, es una banda, no un valor exacto.
            life_range = None
            life = None
            if level is not None and level <= 10:
                life_range = f"{max(0, 100 - level * 10)}–{max(0, 110 - level * 10)}%"
                life = max(0, 100 - level * 10)
            elif level == 11:
                life_range = "Agotada (>100% de uso estimado)"
                life = 0
            try: eol = int(raw_eol, 16)
            except ValueError: eol = None
            health = True if eol == 1 else False if eol == 3 else None
            warning = "" if eol in (None, 0, 1) else ("eMMC en estado de aviso" if eol == 2 else "eMMC en estado urgente")
            try: size = int((Path("/sys/block") / Path(dev).name / "size").read_text().strip()) * 512
            except (OSError, ValueError): size = None
            cid = sysvalue("cid")
            opaque_id = hashlib.sha256(cid.encode("ascii", errors="ignore")).hexdigest()[:24] if cid else f"{name}|{size}|{dev}"
            out.append({"device": dev, "model": "eMMC " + name,
                        "stable_key": "emmc:" + opaque_id,
                        "type": "emmc", "passed": health,
                        "health_warning": warning, "pre_eol": eol,
                        "life": life, "life_label": life_range,
                        "size": size, "temperature": None, "hours": None})
            continue
        fallback = "Desconocido"
        vendor = ""
        model_value = ""
        if lsblk:
            mrc, mout, _ = run([lsblk, "-dn", "-o", "MODEL", dev], timeout=5)
            if mrc == 0:
                model_value = mout.strip()
            vrc, vout, _ = run([lsblk, "-dn", "-o", "VENDOR", dev], timeout=5)
            if vrc == 0:
                vendor = vout.strip()

        if not vendor or not model_value:
            udevadm = shutil.which("udevadm")
            if udevadm:
                urc, uout, _ = run([udevadm, "info", "--query=property", "--name", dev], timeout=5)
                if urc == 0:
                    props = dict(line.split("=", 1) for line in uout.splitlines() if "=" in line)
                    vendor = vendor or props.get("ID_VENDOR", "") or props.get("ID_VENDOR_FROM_DATABASE", "")
                    model_value = model_value or props.get("ID_MODEL", "")

        if not vendor and re.fullmatch(
            r"CT[0-9]+(?:P[0-9]+P?SSD[0-9]*|MX[0-9]+SSD[0-9]*|BX[0-9]+SSD[0-9]*)",
            model_value, flags=re.IGNORECASE,
        ):
            vendor = "Crucial"

        if model_value and vendor and not model_value.casefold().startswith(vendor.casefold()):
            fallback = f"{vendor} {model_value}"
        else:
            fallback = model_value or vendor or fallback
        if not SMARTCTL_BIN:
            continue
        rc, stdout, _ = run([SMARTCTL_BIN, "-j", "-H", "-A", dev], timeout=20)
        try:
            j = json.loads(stdout)
        except Exception:
            out.append({"device": dev, "model": fallback, "passed": None, "temperature": None, "life": None, "hours": None})
            continue

        passed = (j.get("smart_status") or {}).get("passed")
        temp = (j.get("temperature") or {}).get("current")
        hours = (j.get("power_on_time") or {}).get("hours")
        smart_model = j.get("model_name") or j.get("model_family") or ""
        model = fallback if fallback != "Desconocido" else (smart_model or fallback)
        life = None

        nv = j.get("nvme_smart_health_information_log") or {}
        if "percentage_used" in nv:
            try:
                life = max(0, min(100, 100 - int(nv["percentage_used"])))
            except Exception:
                pass

        attrs = ((j.get("ata_smart_attributes") or {}).get("table") or [])
        for a in attrs:
            if a.get("id") == 231:
                try:
                    life = int(a.get("value"))
                except Exception:
                    pass
                break

        out.append({
            "device": dev,
            "model": model,
            "passed": passed,
            "temperature": temp,
            "life": life,
            "hours": hours,
            "smartctl_rc": rc,
        })
    return out


def docker_status():
    if not DOCKER_BIN:
        return {"available": False, "running": 0, "healthy": 0, "unhealthy": 0, "stopped": 0, "containers": []}
    rc, stdout, _ = run([DOCKER_BIN, "ps", "-a", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"], timeout=15)
    rows = []
    if rc == 0 and stdout:
        for line in stdout.splitlines():
            parts = line.split("\t", 3)
            name = parts[0].strip()
            image = parts[1].strip() if len(parts) > 1 else ""
            status = parts[2].strip() if len(parts) > 2 else ""
            # Publicaciones TCP IPv4 solamente, tal y como las notifica Docker.
            # No se prueban puertos, ni se adivinan webs sobre servicios TCP.
            publications = []
            if len(parts) > 3:
                for match in re.finditer(r"(?<![0-9.])((?:[0-9]{1,3}\.){3}[0-9]{1,3}):([0-9]{1,5})->([0-9]{1,5})/tcp", parts[3]):
                    host, outside, inside = match.groups()
                    try:
                        if ipaddress.ip_address(host).version == 4 and 0 < int(outside) <= 65535 and 0 < int(inside) <= 65535:
                            publications.append({"ip": host, "host_port": int(outside), "container_port": int(inside)})
                    except ValueError:
                        continue
            rows.append({"name": name, "image": image, "status": status, "ports": publications})
    rows.sort(key=lambda x: (x["name"].casefold(), x["name"]))
    running = sum(1 for x in rows if x["status"].startswith("Up "))
    healthy = sum(1 for x in rows if "(healthy)" in x["status"])
    unhealthy = sum(1 for x in rows if "(unhealthy)" in x["status"])
    stopped = len(rows) - running
    return {"available": rc == 0, "running": running, "healthy": healthy, "unhealthy": unhealthy, "stopped": stopped, "containers": rows}


def failed_services():
    rc, stdout, _ = run([SYSTEMCTL_BIN, "--failed", "--no-legend", "--plain", "--no-pager"], timeout=15)
    if rc not in (0, 1) or not stdout:
        return []
    rows = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        unit = line.split()[0]
        if unit:
            rows.append(unit)
    return rows


def apt_updates():
    """Actualizaciones que APT instalaría ahora, sin forzar phased updates.

    apt list --upgradable ofrece candidatas incluidas las aplazadas; la
    simulación apt-get -s upgrade indica las realmente instalables ahora.
    Nunca ejecuta una actualización.
    """
    rc, stdout, _ = run([APT_BIN, "list", "--upgradable"], timeout=30)
    if rc != 0:
        return None, [], 0, []

    candidates = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if "[upgradable" not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        package = parts[0].split("/", 1)[0].strip()
        current_version = None
        if "[upgradable from:" in line:
            current_version = line.split("[upgradable from:", 1)[1].split("]", 1)[0].strip()
        candidates.append({
            "package": package,
            "current_version": current_version,
            "new_version": parts[1] if len(parts) > 1 else None,
            "arch": parts[2] if len(parts) > 2 else None,
        })

    sim_rc, sim_out, _ = run(["/usr/bin/apt-get", "-s", "upgrade"], timeout=45)
    if sim_rc != 0:
        # Error en simulación: mostrar candidatas, no ocultar incidencias.
        pending, deferred = candidates, []
    else:
        install_now = set()
        for line in sim_out.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "Inst":
                install_now.add(fields[1])
        pending = [x for x in candidates if x["package"] in install_now]
        deferred = [x for x in candidates if x["package"] not in install_now]

    pending.sort(key=lambda x: x["package"].lower())
    deferred.sort(key=lambda x: x["package"].lower())
    return len(pending), pending, len(deferred), deferred


def default_iface():
    rc, stdout, _ = run([IP_BIN, "route", "get", "1.1.1.1"], timeout=5)
    if rc == 0:
        m = re.search(r"\bdev\s+(\S+)", stdout)
        if m:
            return m.group(1)
    return None


def read_history():
    try:
        with HISTORY_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def main():
    now = time.time()
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    uptime_s = max(0, now - psutil.boot_time())
    cpu = round(float(psutil.cpu_percent(interval=0.8)), 1)
    mem = psutil.virtual_memory()
    load1, load5, load15 = os.getloadavg()
    temps = temperatures()
    disks = disk_usage()
    smart = smart_devices()
    docker = docker_status()
    docker["updater_enabled"] = DASHBOARD_DOCKER_UPDATER
    failed = failed_services()
    updates, update_details, deferred_updates, deferred_update_details = apt_updates()
    iface = default_iface()

    history = read_history()
    net_rx = net_tx = 0
    counters = psutil.net_io_counters(pernic=True)
    if iface and iface in counters:
        net_rx = int(counters[iface].bytes_recv)
        net_tx = int(counters[iface].bytes_sent)

    rx_mbps = tx_mbps = 0.0
    if history:
        prev = history[-1]
        elapsed = max(1.0, now - float(prev.get("ts", now)))
        prev_rx = int(prev.get("net_rx_bytes", net_rx))
        prev_tx = int(prev.get("net_tx_bytes", net_tx))
        if net_rx >= prev_rx:
            rx_mbps = round((net_rx - prev_rx) * 8 / elapsed / 1_000_000, 3)
        if net_tx >= prev_tx:
            tx_mbps = round((net_tx - prev_tx) * 8 / elapsed / 1_000_000, 3)

    point = {
        "ts": now,
        "time": now_iso,
        "cpu": cpu,
        "ram": round(float(mem.percent), 1),
        "cpu_temp": temps["cpu"],
        "nvme_temp": temps["nvme"],
        "rx_mbps": rx_mbps,
        "tx_mbps": tx_mbps,
        "net_rx_bytes": net_rx,
        "net_tx_bytes": net_tx,
    }
    history.append(point)
    cutoff = now - HISTORY_DAYS * 86400
    history = [x for x in history if float(x.get("ts", 0)) >= cutoff][-MAX_HISTORY:]

    warnings = []
    for d in disks:
        if d["percent"] >= 85:
            warnings.append(f"Disco {d['mount']} al {d['percent']:.0f}%")
    if temps["cpu"] is not None and temps["cpu"] >= 85:
        warnings.append(f"CPU a {temps['cpu']:.0f} °C")
    if mem.percent >= 90:
        warnings.append(f"RAM al {mem.percent:.0f}%")
    if failed:
        warnings.append(f"{len(failed)} servicio(s) systemd fallando")
    if docker["unhealthy"]:
        warnings.append(f"{docker['unhealthy']} contenedor(es) unhealthy")
    for s in smart:
        if s.get("type") == "emmc":
            if s.get("health_warning"):
                warnings.append(f"{s['health_warning']} en {s['device']}")
            # Ausencia de EXT_CSD no es un fallo SMART.
            continue
        if s["passed"] is False:
            warnings.append(f"SMART FAIL en {s['device']}")
        elif s["passed"] is None:
            warnings.append(f"SMART sin lectura en {s['device']}")

    identity = {
        "title": DASHBOARD_TITLE,
        "city": DASHBOARD_CITY,
        "weather": weather_current(),
        "timezone": local_timezone(),
        "local_ip": default_route_ipv4(iface),
        "public_ip": public_ip(),
        "vpns": vpn_addresses(),
    }

    data = {
        "hostname": platform.node(),
        "identity": identity,
        "timestamp": now_iso,
        "status": "warning" if warnings else "ok",
        "warnings": warnings,
        "system": {
            "cpu_model": cpu_model(),
            "cpu_percent": cpu,
            "cpu_count": psutil.cpu_count(logical=True),
            "ram_total": mem.total,
            "ram_used": mem.used,
            "ram_available": mem.available,
            "ram_percent": round(float(mem.percent), 1),
            "load1": round(load1, 2),
            "load5": round(load5, 2),
            "load15": round(load15, 2),
            "uptime_seconds": int(uptime_s),
            "uptime": human_uptime(uptime_s),
            "cpu_temp": temps["cpu"],
            "nvme_temp": temps["nvme"],
        },
        "network": {"iface": iface, "rx_mbps": rx_mbps, "tx_mbps": tx_mbps},
        "disks": disks,
        "smart": smart,
        "docker": docker,
        "failed_services": failed,
        "updates": updates,
        "update_details": update_details,
        "deferred_updates": deferred_updates,
        "deferred_update_details": deferred_update_details,
    }

    atomic_json(DATA_FILE, data)
    atomic_json(HISTORY_FILE, history)

    icon = "⚠️" if warnings else "✅"
    lines = [
        f"{icon} {DASHBOARD_TITLE} · {now_iso}",
        f"CPU: {cpu:.1f}% · {temps['cpu'] if temps['cpu'] is not None else 'N/D'} °C · carga {load1:.2f}",
        f"RAM: {mem.percent:.1f}% · {human_bytes(mem.used)} / {human_bytes(mem.total)}",
        f"Uptime: {human_uptime(uptime_s)}",
        "",
        "Discos:",
    ]
    for d in disks:
        mark = " ⚠️" if d["percent"] >= 85 else ""
        lines.append(f"- {d['mount']}: {d['percent']:.1f}% · {human_bytes(d['used'])}/{human_bytes(d['total'])}{mark}")
    lines += ["", "SMART:"]
    for s in smart:
        state = "OK" if s["passed"] is True else ("FALLO" if s["passed"] is False else "N/D")
        temp = f" · {s['temperature']} °C" if s["temperature"] is not None else ""
        life = f" · vida {s['life']}%" if s["life"] is not None else ""
        lines.append(f"- {s['device']}: {state}{temp}{life}")
    lines += [
        "",
        f"Docker: {docker['running']} activos · {docker['healthy']} healthy · {docker['unhealthy']} unhealthy · {docker['stopped']} detenidos",
        f"Servicios fallidos: {len(failed)}" + (f" ({', '.join(failed)})" if failed else ""),
        f"Actualizaciones pendientes: {updates if updates is not None else 'N/D'} (según caché APT)",
        f"Red {iface or 'N/D'}: ↓ {rx_mbps:.3f} Mbps · ↑ {tx_mbps:.3f} Mbps",
    ]
    if warnings:
        lines += ["", "Avisos:"] + [f"- {w}" for w in warnings]
    atomic_text(REPORT_FILE, "\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
