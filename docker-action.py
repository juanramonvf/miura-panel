#!/usr/bin/env python3
import grp
import json
import os
import re
import shutil
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path

SOCKET = "/run/server-dashboard/docker-action.sock"
CHECKER = "/usr/local/sbin/server-dashboard-docker-updates.py"
VERSIONS = Path("/var/www/server-dashboard/docker-updates.json")
LOG = Path("/var/lib/server-dashboard/docker-actions.log")
COMPOSE_BACKUPS = Path("/var/lib/server-dashboard/compose-backups")

DOCKER_BIN = shutil.which("docker") or "/usr/bin/docker"
WEB_GROUP = "server-dashboard"

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


def configured_compose_roots():
    raw = load_dashboard_config().get("DASHBOARD_COMPOSE_ROOTS", "/srv")
    roots = []
    for item in raw.split(":"):
        item = item.strip()
        if not item:
            continue
        try:
            p = Path(item).resolve()
        except Exception:
            continue
        if p.is_dir():
            roots.append(p)
    return roots


def path_under_allowed_root(path):
    try:
        target = Path(path).resolve()
    except Exception:
        return False
    for root in configured_compose_roots():
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False



jobs = {}
jobs_lock = threading.Lock()
action_lock = threading.Lock()


def run(cmd, cwd=None, timeout=900):
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
    out = ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()
    return p.returncode, out[-16000:]


def docker_inspect(name):
    p = subprocess.run([DOCKER_BIN, "inspect", name], text=True, capture_output=True, timeout=30, check=False)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or f"No existe el contenedor {name}").strip())
    return json.loads(p.stdout)[0]


def service_image_line(path, service):
    lines = Path(path).read_text(encoding="utf-8").splitlines(True)
    services_indent = None
    service_indent = None
    in_service = False
    service_re = re.compile(r"^\s*" + re.escape(service) + r"\s*:\s*(?:#.*)?$")
    image_re = re.compile(r"^(\s*image\s*:\s*)(['\"]?)([^'\"#\s]+)(\2)(\s*(?:#.*)?\r?\n?)$")
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if services_indent is None:
            if re.match(r"^\s*services\s*:\s*(?:#.*)?$", line):
                services_indent = indent
            continue
        if not in_service:
            if indent > services_indent and service_re.match(line):
                service_indent = indent
                in_service = True
            continue
        if indent <= service_indent:
            break
        m = image_re.match(line)
        if m:
            return lines, idx, m
    return None, None, None


def replace_tag(ref, new_tag):
    if "@" in ref:
        raise RuntimeError("No se modifica una imagen fijada por digest")
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo = ref.rsplit(":", 1)[0]
    else:
        repo = ref
    return f"{repo}:{new_tag}"


def compose_info(name):
    j = docker_inspect(name)
    labels = ((j.get("Config") or {}).get("Labels") or {})
    project = (labels.get("com.docker.compose.project") or "").strip()
    service = (labels.get("com.docker.compose.service") or "").strip()
    workdir = (labels.get("com.docker.compose.project.working_dir") or "").strip()
    config_raw = (labels.get("com.docker.compose.project.config_files") or "").strip()
    running = bool((j.get("State") or {}).get("Running"))
    if not project or not service or not workdir:
        raise RuntimeError(f"{name}: no está gestionado por Docker Compose")
    # Portainer y otros gestores pueden no publicar config_files en Docker.
    # Nunca invocar docker compose sin el fichero exacto autorizado.
    if not config_raw:
        raise RuntimeError(f"{name}: Compose gestionado externamente o sin config_files; solo lectura")
    wd = Path(workdir).resolve()
    if not wd.is_dir() or not path_under_allowed_root(wd):
        allowed = ", ".join(str(x) for x in configured_compose_roots()) or "ninguna"
        raise RuntimeError(
            f"{name}: directorio Compose no permitido: {wd} "
            f"(rutas autorizadas: {allowed})"
        )
    files = []
    for raw in config_raw.split(",") if config_raw else []:
        raw = raw.strip()
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = wd / p
        p = p.resolve()
        if not str(p).startswith(str(wd) + os.sep):
            raise RuntimeError(f"{name}: compose file fuera del proyecto: {p}")
        if not p.is_file():
            raise RuntimeError(f"{name}: no existe {p}")
        files.append(str(p))
    base = [DOCKER_BIN, "compose", "--project-directory", str(wd), "--project-name", project]
    for f in files:
        base += ["-f", f]
    rc, out = run(base + ["config", "--services"], cwd=str(wd), timeout=60)
    if rc != 0:
        raise RuntimeError(f"{name}: configuración Compose inválida: {out}")
    if service not in {x.strip() for x in out.splitlines() if x.strip()}:
        raise RuntimeError(f"{name}: servicio {service} no aparece en Compose")
    return {"name": name, "project": project, "service": service, "workdir": str(wd), "files": files, "base": base, "running": running}


def version_row(name):
    try:
        data = json.loads(VERSIONS.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"No puedo leer el estado de versiones: {e}")
    for row in data.get("containers", []):
        if row.get("name") == name:
            return row
    raise RuntimeError(f"{name}: no aparece en la última comprobación de versiones")


def group_row(group_id):
    try:
        data = json.loads(VERSIONS.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"No puedo leer el estado de versiones: {e}")
    for row in data.get("groups", []):
        if row.get("id") == group_id:
            return row
    raise RuntimeError(f"Grupo {group_id}: no aparece en la última comprobación de versiones")


def env_value(path, key):
    rx = re.compile(r"^\s*" + re.escape(key) + r"\s*=\s*(.*?)\s*$")
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = rx.match(line)
        if m:
            val = m.group(1).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            return val
    return None


def replace_env_value(path, key, expected, target):
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(True)
    rx = re.compile(r"^(\s*" + re.escape(key) + r"\s*=\s*)(['\"]?)([^'\"#\r\n]*)(\2)(\s*(?:#.*)?)(\r?\n?)$")
    done = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = rx.match(line)
        if not m:
            continue
        current = m.group(3).strip()
        if current != expected:
            raise RuntimeError(f"{key} cambió desde la comprobación ({current} != {expected}); refresca el panel")
        lines[i] = f"{m.group(1)}{m.group(2)}{target}{m.group(4)}{m.group(5)}{m.group(6)}"
        done = True
        break
    if not done:
        raise RuntimeError(f"No encuentro {key} en {path}")
    path.write_text("".join(lines), encoding="utf-8")
    return original


def append_job(job_id, line):
    line = str(line).strip()
    if not line:
        return
    ts = time.strftime("%H:%M:%S")
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["log"].append(f"[{ts}] {line}")
        job["log"] = job["log"][-100:]
        job["updated_at"] = time.time()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {job_id} {line}\n")


def rewrite_compose_tag(info, row, job_id):
    if row.get("action_mode") != "rewrite_tag":
        return None
    target = str(row.get("remote_tag") or "").strip()
    source_file = str(row.get("source_file") or "").strip()
    source_value = str(row.get("source_value") or "").strip()
    if not target or not source_file or not source_value:
        raise RuntimeError("faltan datos para modificar el tag de Compose")
    path = Path(source_file).resolve()
    wd = Path(info["workdir"]).resolve()
    if not str(path).startswith(str(wd) + os.sep) or str(path) not in info["files"]:
        raise RuntimeError(f"compose file no permitido: {path}")
    lines, idx, m = service_image_line(path, info["service"])
    if m is None:
        raise RuntimeError(f"no encuentro image: del servicio {info['service']} en {path}")
    current_value = m.group(3)
    if "${" in current_value or "@sha256:" in current_value:
        raise RuntimeError("la imagen está gestionada por variable o digest; no se modifica automáticamente")
    if current_value != source_value:
        raise RuntimeError(f"el Compose cambió desde la comprobación ({current_value} != {source_value}); refresca el panel")
    new_value = replace_tag(current_value, target)
    if new_value == current_value:
        return None

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = COMPOSE_BACKUPS / stamp / info["project"]
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / path.name
    shutil.copy2(path, backup)
    original = "".join(lines)
    lines[idx] = f"{m.group(1)}{m.group(2)}{new_value}{m.group(4)}{m.group(5)}"
    path.write_text("".join(lines), encoding="utf-8")

    rc, out = run(info["base"] + ["config"], cwd=info["workdir"], timeout=90)
    if rc != 0:
        path.write_text(original, encoding="utf-8")
        raise RuntimeError(f"Compose inválido tras cambiar el tag; restaurado: {out[-1200:]}")
    append_job(job_id, f"{info['name']}: copia {backup}; tag {current_value} → {new_value}")
    return {"path": path, "backup": backup, "original": original, "old_value": current_value, "new_value": new_value}


def recreate(info, job_id):
    service = info["service"]
    base = info["base"]
    cwd = info["workdir"]
    was_running = info["running"]
    append_job(job_id, f"{info['name']}: descargando imagen de {service}…")
    rc, out = run(base + ["pull", service], cwd=cwd, timeout=1200)
    if rc != 0:
        raise RuntimeError(f"pull falló: {out[-2000:]}")
    if was_running:
        append_job(job_id, f"{info['name']}: recreando servicio y manteniéndolo activo…")
        rc, out = run(base + ["up", "-d", "--no-deps", "--force-recreate", service], cwd=cwd, timeout=1200)
    else:
        append_job(job_id, f"{info['name']}: recreando servicio; seguirá detenido…")
        rc, out = run(base + ["create", "--force-recreate", service], cwd=cwd, timeout=1200)
    if rc != 0:
        raise RuntimeError(f"recreación falló: {out[-2000:]}")
    now = docker_inspect(info["name"])
    is_running = bool((now.get("State") or {}).get("Running"))
    if was_running and not is_running:
        raise RuntimeError("estaba activo y no ha quedado activo")
    if not was_running and is_running:
        run([DOCKER_BIN, "stop", info["name"]], timeout=120)
        append_job(job_id, f"{info['name']}: detenido de nuevo para conservar su estado anterior")



def recalc_version_counters(data):
    rows = data.get("containers") or []
    groups = data.get("groups") or []
    group_updates = sum(1 for g in groups if g.get("update") is True and g.get("actionable") is True)
    data["updates"] = sum(1 for r in rows if r.get("update") is True and r.get("actionable") is True) + group_updates
    data["bulk_updates"] = sum(1 for r in rows if r.get("update") is True and r.get("bulk_allowed") is True)
    data["manual_updates"] = sum(1 for r in rows if r.get("update") is True and r.get("actionable") is not True)
    data["protected"] = sum(1 for r in rows if r.get("check") == "protected")
    data["errors"] = sum(1 for r in rows if r.get("check") == "error") + sum(1 for g in groups if g.get("check") == "error")
    data["count"] = len(rows)
    data["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_versions_atomic(data):
    tmp = VERSIONS.with_name(VERSIONS.name + ".tmp-local")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, VERSIONS)


def image_tag(ref):
    raw = str(ref or "").split("@", 1)[0]
    last = raw.rsplit("/", 1)[-1]
    return raw.rsplit(":", 1)[1] if ":" in last else "latest"


def mark_container_current(name, previous_row, job_id):
    """Actualiza inmediatamente la caché del panel tras una recreación validada.
    La comprobación remota completa seguirá ejecutándose después en segundo plano.
    """
    try:
        data = json.loads(VERSIONS.read_text(encoding="utf-8"))
        row = next((x for x in data.get("containers", []) if x.get("name") == name), None)
        if row is None:
            return
        j = docker_inspect(name)
        image = ((j.get("Config") or {}).get("Image") or "").strip()
        tag = image_tag(image)

        # Antes de actualizar ya conocíamos la versión remota que acabamos de instalar.
        verified = str(previous_row.get("latest") or "").strip()
        if not verified or verified == "N/D":
            verified = str(previous_row.get("remote_tag") or tag).strip() or tag

        row["image"] = image or row.get("image")
        row["installed"] = verified
        row["latest"] = verified
        row["update"] = False
        row["actionable"] = False
        row["bulk_allowed"] = False
        row["action_mode"] = None
        row["check"] = "current"
        row["error"] = None
        if image:
            row["source_value"] = image
        if previous_row.get("remote_digest"):
            row["local_digest"] = previous_row.get("remote_digest")
            row["remote_digest"] = previous_row.get("remote_digest")

        recalc_version_counters(data)
        write_versions_atomic(data)
        append_job(job_id, f"{name}: panel actualizado al instante → AL DÍA ({verified})")
    except Exception as e:
        append_job(job_id, f"Aviso: no pude refrescar inmediatamente {name} en el panel: {e}")


def mark_immich_current(target, job_id):
    try:
        data = json.loads(VERSIONS.read_text(encoding="utf-8"))
        for g in data.get("groups", []):
            if g.get("id") == "immich":
                g["installed"] = target
                g["latest"] = target
                g["update"] = False
                g["actionable"] = False
                g["bulk_allowed"] = False
                g["check"] = "current"
                g["reason"] = "server + machine-learning comparten IMMICH_VERSION; PostgreSQL y Redis permanecen fijados"
        for row in data.get("containers", []):
            if row.get("name") in ("immich_server", "immich_machine_learning"):
                row["installed"] = target
                row["latest"] = target
                row["update"] = False
                row["actionable"] = False
                row["bulk_allowed"] = False
                row["action_mode"] = None
                row["check"] = "grouped"
                row["error"] = None
        recalc_version_counters(data)
        write_versions_atomic(data)
        append_job(job_id, f"Immich: panel actualizado al instante → AL DÍA ({target})")
    except Exception as e:
        append_job(job_id, f"Aviso: no pude refrescar inmediatamente Immich en el panel: {e}")

def update_service(name, job_id):
    row = version_row(name)
    if not row.get("update"):
        raise RuntimeError(f"{name}: ya no figura con actualización pendiente")
    if not row.get("actionable"):
        raise RuntimeError(f"{name}: actualización protegida/manual ({row.get('reason') or 'sin motivo'})")
    mode = row.get("action_mode")
    if mode not in ("pull_only", "rewrite_tag"):
        raise RuntimeError(f"{name}: modo de actualización no permitido")
    info = compose_info(name)
    edit = None
    try:
        if mode == "rewrite_tag":
            edit = rewrite_compose_tag(info, row, job_id)
        recreate(info, job_id)
    except Exception as e:
        if edit:
            try:
                edit["path"].write_text(edit["original"], encoding="utf-8")
                append_job(job_id, f"{name}: ERROR; Compose restaurado desde copia")
                # Intento de dejar el servicio con su imagen anterior.
                run(info["base"] + ["pull", info["service"]], cwd=info["workdir"], timeout=1200)
                if info["running"]:
                    run(info["base"] + ["up", "-d", "--no-deps", "--force-recreate", info["service"]], cwd=info["workdir"], timeout=1200)
                else:
                    run(info["base"] + ["create", "--force-recreate", info["service"]], cwd=info["workdir"], timeout=1200)
            except Exception as rollback_error:
                append_job(job_id, f"{name}: ERROR también durante rollback: {rollback_error}")
        raise
    mark_container_current(name, row, job_id)
    append_job(job_id, f"{name}: actualización completada")


def update_immich_group(job_id):
    group = group_row("immich")
    if not group.get("update") or not group.get("actionable"):
        raise RuntimeError("Immich no tiene una actualización de grupo permitida")
    current = str(group.get("installed") or "").strip()
    target = str(group.get("latest") or "").strip()
    if not current or not target or current == "N/D" or target == "N/D":
        raise RuntimeError("No hay versiones válidas para actualizar Immich")

    info = compose_info("immich_server")
    env = Path(info["workdir"]) / ".env"
    if not env.is_file():
        raise RuntimeError(f"No existe {env}")
    actual = env_value(env, "IMMICH_VERSION")
    if actual != current:
        raise RuntimeError(f"IMMICH_VERSION cambió desde la comprobación ({actual} != {current}); refresca el panel")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = COMPOSE_BACKUPS / stamp / "immich"
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / ".env"
    shutil.copy2(env, backup)
    original = env.read_text(encoding="utf-8")

    members = [("immich_server", "immich-server"), ("immich_machine_learning", "immich-machine-learning")]
    states = {}
    for container, _service in members:
        j = docker_inspect(container)
        states[container] = bool((j.get("State") or {}).get("Running"))

    try:
        replace_env_value(env, "IMMICH_VERSION", current, target)
        append_job(job_id, f"Immich: copia {backup}; IMMICH_VERSION {current} → {target}")
        rc, out = run(info["base"] + ["config"], cwd=info["workdir"], timeout=90)
        if rc != 0:
            raise RuntimeError(f"Compose de Immich inválido tras cambiar .env: {out[-1800:]}")

        services = [service for _container, service in members]
        append_job(job_id, f"Immich: descargando {target} para server y machine-learning…")
        rc, out = run(info["base"] + ["pull"] + services, cwd=info["workdir"], timeout=1800)
        if rc != 0:
            raise RuntimeError(f"pull de Immich falló: {out[-2500:]}")

        append_job(job_id, "Immich: recreando server y machine-learning; PostgreSQL y Redis no se tocan…")
        rc, out = run(info["base"] + ["up", "-d", "--no-deps", "--force-recreate"] + services, cwd=info["workdir"], timeout=1800)
        if rc != 0:
            raise RuntimeError(f"recreación de Immich falló: {out[-2500:]}")

        for container, _service in members:
            j = docker_inspect(container)
            image = ((j.get("Config") or {}).get("Image") or "")
            if not image.endswith(":" + target):
                raise RuntimeError(f"{container} no quedó en {target}: {image}")
            if not states[container] and bool((j.get("State") or {}).get("Running")):
                run([DOCKER_BIN, "stop", container], timeout=120)
                append_job(job_id, f"{container}: detenido de nuevo para conservar su estado anterior")
            elif states[container] and not bool((j.get("State") or {}).get("Running")):
                raise RuntimeError(f"{container} estaba activo y no ha quedado activo")
        mark_immich_current(target, job_id)
        append_job(job_id, f"Immich: actualización de grupo completada en {target}")
    except Exception:
        env.write_text(original, encoding="utf-8")
        append_job(job_id, "Immich: ERROR; .env restaurado. Intentando rollback de server y machine-learning…")
        services = [service for _container, service in members]
        run(info["base"] + ["pull"] + services, cwd=info["workdir"], timeout=1800)
        run(info["base"] + ["up", "-d", "--no-deps", "--force-recreate"] + services, cwd=info["workdir"], timeout=1800)
        for container, _service in members:
            if not states.get(container, True):
                run([DOCKER_BIN, "stop", container], timeout=120)
        raise


def pending_names():
    try:
        data = json.loads(VERSIONS.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"No puedo leer las actualizaciones pendientes: {e}")
    # En lote solo tags flotantes/canales. Los tags fijos requieren clic individual.
    return [x.get("name") for x in data.get("containers", []) if x.get("update") is True and x.get("actionable") is True and x.get("bulk_allowed") is True and x.get("name")]


def refresh_versions(job_id):
    append_job(job_id, "Actualización completada; refrescando versiones en segundo plano…")
    try:
        subprocess.Popen(
            ["/usr/bin/python3", CHECKER],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        append_job(job_id, f"Aviso: no se pudo iniciar el refresco de versiones: {e}")


def worker(job_id, action, container=None):
    with action_lock:
        with jobs_lock:
            jobs[job_id]["state"] = "running"
            jobs[job_id]["started_at"] = time.time()
        failures = []
        try:
            if action == "update_group":
                with jobs_lock:
                    jobs[job_id]["total"] = 1
                    jobs[job_id]["current"] = "Immich"
                    jobs[job_id]["done"] = 0
                try:
                    update_immich_group(job_id)
                except Exception as e:
                    failures.append({"name": "Immich", "error": str(e)})
                    append_job(job_id, f"ERROR Immich: {e}")
                with jobs_lock:
                    jobs[job_id]["done"] = 1
            else:
                targets = [container] if action == "update_one" else pending_names()
                if action == "update_all" and not targets:
                    append_job(job_id, "No hay actualizaciones seguras en lote pendientes")
                with jobs_lock:
                    jobs[job_id]["total"] = len(targets)
                for idx, name in enumerate(targets, 1):
                    with jobs_lock:
                        jobs[job_id]["current"] = name
                        jobs[job_id]["done"] = idx - 1
                    try:
                        update_service(name, job_id)
                    except Exception as e:
                        failures.append({"name": name, "error": str(e)})
                        append_job(job_id, f"ERROR {name}: {e}")
                    with jobs_lock:
                        jobs[job_id]["done"] = idx
            refresh_versions(job_id)
            with jobs_lock:
                jobs[job_id]["failures"] = failures
                jobs[job_id]["state"] = "partial" if failures else "done"
                jobs[job_id]["finished_at"] = time.time()
                jobs[job_id]["current"] = None
        except Exception as e:
            append_job(job_id, f"ERROR: {e}")
            with jobs_lock:
                jobs[job_id]["state"] = "error"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["finished_at"] = time.time()
                jobs[job_id]["current"] = None


def new_job(action, container=None):
    with jobs_lock:
        if any(j.get("state") in ("queued", "running") for j in jobs.values()):
            raise RuntimeError("Ya hay una actualización Docker en curso")
        job_id = uuid.uuid4().hex[:12]
        jobs[job_id] = {"job_id": job_id, "action": action, "container": container, "state": "queued", "created_at": time.time(), "started_at": None, "finished_at": None, "current": None, "done": 0, "total": 1 if action in ("update_one", "update_group") else 0, "failures": [], "log": []}
    threading.Thread(target=worker, args=(job_id, action, container), daemon=True).start()
    return job_id


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = json.loads(self.rfile.readline(65536).decode("utf-8"))
            action = req.get("action")
            if action == "status":
                job_id = str(req.get("job_id") or "")
                with jobs_lock:
                    job = jobs.get(job_id)
                    if not job:
                        raise RuntimeError("Trabajo no encontrado")
                    resp = {"ok": True, "job": dict(job)}
            elif action == "update_one":
                name = str(req.get("container") or "").strip()
                if not name or len(name) > 128:
                    raise RuntimeError("Nombre de contenedor inválido")
                compose_info(name)
                row = version_row(name)
                if not row.get("update") or not row.get("actionable"):
                    raise RuntimeError("Ese contenedor no tiene una actualización automática permitida")
                resp = {"ok": True, "job_id": new_job(action, name)}
            elif action == "update_group":
                group_id = str(req.get("group") or "").strip()
                if group_id != "immich":
                    raise RuntimeError("Grupo no permitido")
                row = group_row(group_id)
                if not row.get("update") or not row.get("actionable"):
                    raise RuntimeError("Immich no tiene una actualización de grupo permitida")
                resp = {"ok": True, "job_id": new_job(action, group_id)}
            elif action == "update_all":
                resp = {"ok": True, "job_id": new_job(action)}
            else:
                raise RuntimeError("Acción no permitida")
        except Exception as e:
            resp = {"ok": False, "error": str(e)}
        self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


if __name__ == "__main__":
    Path(SOCKET).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    srv = Server(SOCKET, Handler)
    try:
        socket_gid = grp.getgrnam(WEB_GROUP).gr_gid
    except KeyError:
        raise SystemExit(f"No existe el grupo requerido: {WEB_GROUP}")
    os.chown(SOCKET, 0, socket_gid)
    os.chmod(SOCKET, 0o660)
    srv.serve_forever()
