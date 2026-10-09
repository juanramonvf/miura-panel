#!/usr/bin/env python3
import concurrent.futures
import fcntl
import sys
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path

OUT = Path("/var/www/server-dashboard/docker-updates.json")

DOCKER_BIN = shutil.which("docker") or "/usr/bin/docker"
SKOPEO_BIN = shutil.which("skopeo") or "/usr/bin/skopeo"

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


IMMICH_SERVER_REPO = "ghcr.io/immich-app/immich-server"
IMMICH_ML_REPO = "ghcr.io/immich-app/immich-machine-learning"
_immich_group = None

VERSION_LABELS = (
    "org.opencontainers.image.version", "org.label-schema.version",
    "version", "build_version",
)
VERSION_ENVS = (
    "VERSION", "APP_VERSION", "N8N_VERSION", "NEXTCLOUD_VERSION",
    "REDIS_VERSION", "VALKEY_VERSION", "MARIADB_VERSION", "MYSQL_VERSION",
    "PIHOLE_VERSION", "IMMICH_VERSION", "NAVIDROME_VERSION", "PORTAINER_VERSION",
    "MOSQUITTO_VERSION", "SFTPGO_VERSION", "HOME_ASSISTANT_VERSION",
)

_cache_lock = threading.Lock()
_inspect_cache = {}
_tags_cache = {}


def run(cmd, timeout=45):
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 125, "", str(e)


def split_ref(ref):
    raw = (ref or "").strip()
    digest = None
    if "@" in raw:
        raw, digest = raw.split("@", 1)
    last = raw.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = raw.rsplit(":", 1)
    else:
        repo, tag = raw, "latest"
    first = repo.split("/", 1)[0]
    explicit_registry = "." in first or ":" in first or first == "localhost"
    if explicit_registry:
        canonical = repo
    else:
        canonical = "docker.io/" + repo
        if "/" not in repo:
            canonical = "docker.io/library/" + repo
    return canonical, tag, digest


def version_tag(tag):
    m = re.fullmatch(r"(v?)(\d+(?:\.\d+)*)(-.+)?", tag or "")
    if not m:
        return None
    return {
        "v": m.group(1),
        "nums": tuple(int(x) for x in m.group(2).split(".")),
        "suffix": m.group(3) or "",
    }


def is_fixed_version_tag(tag):
    p = version_tag(tag)
    return bool(p and len(p["nums"]) >= 3)


def choose_latest_fixed(current, tags):
    cur = version_tag(current)
    if not cur or len(cur["nums"]) < 3:
        return current
    candidates = []
    for t in tags or []:
        p = version_tag(t)
        if not p or len(p["nums"]) < 3:
            continue
        if p["v"] != cur["v"] or p["suffix"] != cur["suffix"]:
            continue
        # No saltos mayores automáticos.
        if p["nums"][0] != cur["nums"][0]:
            continue
        candidates.append((p["nums"], t))
    if not candidates:
        return current
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def env_map(items):
    out = {}
    for item in items or []:
        if "=" in item:
            k, v = item.split("=", 1)
            out[k] = v
    return out


def metadata_version(meta, fallback_tag):
    if is_fixed_version_tag(fallback_tag):
        return fallback_tag
    labels = (meta or {}).get("Labels") or (((meta or {}).get("config") or {}).get("Labels") or {})
    env = (meta or {}).get("Env") or (((meta or {}).get("config") or {}).get("Env") or [])
    labels_l = {str(k).lower(): str(v) for k, v in labels.items() if v not in (None, "")}
    for key in VERSION_LABELS:
        val = labels_l.get(key.lower())
        if val:
            return val
    em = env_map(env)
    for key in VERSION_ENVS:
        val = em.get(key)
        if val:
            return val
    return fallback_tag or "N/D"


def service_image_in_file(path, service):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    services_indent = None
    service_indent = None
    in_service = False
    result = None
    service_re = re.compile(r"^\s*" + re.escape(service) + r"\s*:\s*(?:#.*)?$")
    image_re = re.compile(r"^\s*image\s*:\s*(.+?)\s*$")
    for line in lines:
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
            val = m.group(1).strip()
            if " #" in val:
                val = val.split(" #", 1)[0].rstrip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            result = val
            break
    return result


def compose_source(container_json):
    labels = ((container_json.get("Config") or {}).get("Labels") or {})
    service = (labels.get("com.docker.compose.service") or "").strip()
    workdir = (labels.get("com.docker.compose.project.working_dir") or "").strip()
    raw = (labels.get("com.docker.compose.project.config_files") or "").strip()
    if service and workdir and not raw:
        # Los stacks de Portainer normalmente no exponen config_files.
        # Se comprueba la versión remota, pero jamás se actualizan desde SEP.
        return {"mode": "external", "reason": "Stack o Compose gestionado externamente (p. ej. Portainer); actualizar desde su gestor"}
    if not service or not workdir:
        return {"mode": "unknown", "reason": "Sin metadatos Compose completos"}
    wd = Path(workdir).resolve()
    if not wd.is_dir() or not path_under_allowed_root(wd):
        allowed = ", ".join(str(x) for x in configured_compose_roots()) or "ninguna"
        return {
            "mode": "unknown",
            "reason": f"Directorio Compose fuera de rutas autorizadas: {wd} "
                      f"(permitidas: {allowed})",
        }
    found = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        p = Path(item)
        if not p.is_absolute():
            p = wd / p
        try:
            p = p.resolve()
        except Exception:
            continue
        val = service_image_in_file(p, service)
        if val:
            found.append((str(p), val))
    if not found:
        return {"mode": "unknown", "reason": "No se localiza image: del servicio en Compose"}
    path, value = found[-1]  # el último -f tiene prioridad en Compose
    if "@sha256:" in value:
        return {"mode": "digest", "file": path, "value": value, "reason": "Imagen fijada por digest SHA256"}
    if "${" in value:
        return {"mode": "variable", "file": path, "value": value, "reason": "Versión gestionada por variable/.env"}
    return {"mode": "literal", "file": path, "value": value, "reason": None}


def local_image_info(container):
    rc, out, err = run([DOCKER_BIN, "inspect", container], 20)
    if rc != 0:
        raise RuntimeError(err or "docker inspect falló")
    j = json.loads(out)[0]
    image_ref = ((j.get("Config") or {}).get("Image") or "").strip()
    image_id = j.get("Image") or ""
    rc, out, err = run([DOCKER_BIN, "image", "inspect", image_id], 20)
    if rc != 0:
        raise RuntimeError(err or "docker image inspect falló")
    img = json.loads(out)[0]
    meta = {
        "Labels": ((img.get("Config") or {}).get("Labels") or {}),
        "Env": ((img.get("Config") or {}).get("Env") or []),
    }
    return j, image_ref, img.get("RepoDigests") or [], meta


def local_digest_for(repo_digests, canonical_repo):
    target = canonical_repo
    short = target.removeprefix("docker.io/library/").removeprefix("docker.io/")
    for rd in repo_digests or []:
        if "@" not in rd:
            continue
        name, digest = rd.rsplit("@", 1)
        names = {name, name.removeprefix("docker.io/")}
        if target in names or short in names or name.endswith("/" + short):
            return digest
    for rd in repo_digests or []:
        if "@" in rd:
            return rd.rsplit("@", 1)[1]
    return None


def skopeo_inspect(ref):
    with _cache_lock:
        if ref in _inspect_cache:
            return _inspect_cache[ref]
    rc, out, err = run([SKOPEO_BIN, "inspect", "--no-tags", ref], 45)
    if rc != 0:
        raise RuntimeError((err or out or "skopeo inspect falló")[-600:])
    data = json.loads(out)
    with _cache_lock:
        _inspect_cache[ref] = data
    return data


def skopeo_tags(canonical):
    with _cache_lock:
        if canonical in _tags_cache:
            return _tags_cache[canonical]
    rc, out, err = run([SKOPEO_BIN, "list-tags", f"docker://{canonical}"], 55)
    if rc != 0:
        raise RuntimeError((err or out or "skopeo list-tags falló")[-600:])
    tags = json.loads(out).get("Tags") or []
    with _cache_lock:
        _tags_cache[canonical] = tags
    return tags


def read_env_value(path, key):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    rx = re.compile(r"^\s*" + re.escape(key) + r"\s*=\s*(.*?)\s*$")
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = rx.match(line)
        if m:
            val = m.group(1).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            return val
    return None



def compose_workdir(container):
    rc, out, err = run([DOCKER_BIN, "inspect", container], 20)
    if rc != 0:
        raise RuntimeError(err or f"docker inspect falló para {container}")
    j = json.loads(out)[0]
    labels = ((j.get("Config") or {}).get("Labels") or {})
    workdir = (labels.get("com.docker.compose.project.working_dir") or "").strip()
    if not workdir:
        raise RuntimeError(f"{container}: sin directorio Compose")
    wd = Path(workdir).resolve()
    if not wd.is_dir() or not path_under_allowed_root(wd):
        allowed = ", ".join(str(x) for x in configured_compose_roots()) or "ninguna"
        raise RuntimeError(
            f"{container}: directorio Compose no permitido: {wd} "
            f"(rutas autorizadas: {allowed})"
        )
    return wd


def check_immich_group():
    group = {
        "id": "immich", "name": "Immich", "installed": "N/D", "latest": "N/D",
        "update": False, "actionable": False, "bulk_allowed": False,
        "check": "error", "reason": None,
        "members": ["immich_server", "immich_machine_learning"],
    }
    try:
        immich_dir = compose_workdir("immich_server")
        immich_env = immich_dir / ".env"
        current = read_env_value(immich_env, "IMMICH_VERSION")
        if not current:
            raise RuntimeError(f"IMMICH_VERSION no está definida en {immich_env}")
        cur = version_tag(current)
        if not cur or len(cur["nums"]) < 3:
            raise RuntimeError(f"IMMICH_VERSION no está fijada a una versión completa: {current}")

        req = urllib.request.Request(
            "https://api.github.com/repos/immich-app/immich/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "server-dashboard",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            release = json.loads(response.read().decode("utf-8"))

        latest = str(release.get("tag_name") or "").strip()
        if not latest:
            raise RuntimeError("GitHub no devolvió tag_name para la última release de Immich")
        lat = version_tag(latest)
        if not lat or len(lat["nums"]) < 3:
            raise RuntimeError(f"Versión remota de Immich no reconocida: {latest}")

        group["installed"] = current
        group["latest"] = latest

        # Un salto de versión mayor requiere revisión manual de las notas de migración.
        if lat["nums"][0] != cur["nums"][0]:
            group["update"] = lat["nums"] > cur["nums"]
            group["actionable"] = False
            group["check"] = "manual" if group["update"] else "current"
            group["reason"] = (
                f"Nueva versión mayor {latest}; requiere revisión manual antes de cambiar IMMICH_VERSION"
                if group["update"] else
                "server + machine-learning comparten IMMICH_VERSION; PostgreSQL y Redis permanecen fijados"
            )
            return group

        group["update"] = lat["nums"] > cur["nums"]
        group["actionable"] = group["update"]
        group["check"] = "update" if group["update"] else "current"
        group["reason"] = "server + machine-learning comparten IMMICH_VERSION; PostgreSQL y Redis permanecen fijados"
    except Exception as e:
        group["reason"] = str(e)
    return group

def check_one(row):
    name = row["name"]
    result = {
        "name": name, "status": row.get("status") or "", "image": row.get("image") or "",
        "installed": "N/D", "latest": "N/D", "remote_tag": None,
        "update": False, "actionable": False, "bulk_allowed": False,
        "action_mode": None, "check": "unknown", "error": None,
        "source_mode": None, "source_file": None, "source_value": None, "reason": None,
    }
    if name in ("immich_server", "immich_machine_learning") and _immich_group:
        result["installed"] = _immich_group.get("installed", "N/D")
        result["latest"] = _immich_group.get("latest", "N/D")
        result["check"] = "grouped"
        result["group"] = "immich"
        result["reason"] = "Gestionado conjuntamente mediante IMMICH_VERSION"
        return result
    try:
        j, image_ref, repo_digests, local_meta = local_image_info(name)
        canonical, current_tag, digest = split_ref(image_ref)
        if not canonical:
            raise RuntimeError("imagen sin referencia de registro")
        result["image"] = image_ref
        result["installed"] = metadata_version(local_meta, current_tag)

        source = compose_source(j)
        result["source_mode"] = source.get("mode")
        result["source_file"] = source.get("file")
        result["source_value"] = source.get("value")
        result["reason"] = source.get("reason")

        # Un digest es una fijación intencionada: nunca lo cambia el panel.
        if digest or source.get("mode") == "digest":
            result["check"] = "protected"
            result["latest"] = "Fijado por SHA256"
            result["reason"] = "Imagen fijada por digest SHA256"
            return result

        # Si Compose no es identificable, mostramos información pero no habilitamos cambios.
        if source.get("mode") in ("unknown", "external"):
            result["check"] = "manual"
            result["reason"] = source.get("reason") or "Configuración Compose no identificable"

        # Etiqueta fija completa: busca una versión más nueva SOLO en la misma rama mayor.
        fixed = is_fixed_version_tag(current_tag)
        latest_tag = current_tag
        if fixed:
            latest_tag = choose_latest_fixed(current_tag, skopeo_tags(canonical))

        remote_ref = f"docker://{canonical}:{latest_tag}"
        remote = skopeo_inspect(remote_ref)
        remote_digest = remote.get("Digest")
        local_digest = local_digest_for(repo_digests, canonical)
        result["remote_tag"] = latest_tag
        result["latest"] = metadata_version(remote, latest_tag)
        result["local_digest"] = local_digest
        result["remote_digest"] = remote_digest

        version_advance = fixed and latest_tag != current_tag
        digest_advance = (not version_advance and local_digest and remote_digest and local_digest != remote_digest)
        available = bool(version_advance or digest_advance)
        result["update"] = available

        # Variables como IMMICH_VERSION se revisan, pero el panel NO toca .env automáticamente.
        if source.get("mode") == "variable":
            result["actionable"] = False
            result["bulk_allowed"] = False
            result["check"] = "manual" if available else "managed"
            result["reason"] = "Versión gestionada por variable/.env; actualización manual protegida"
            return result

        if source.get("mode") != "literal":
            result["actionable"] = False
            result["bulk_allowed"] = False
            result["check"] = "manual" if (available or source.get("mode") == "external") else "managed"
            return result

        if available:
            result["actionable"] = True
            if version_advance:
                # Cambia el tag literal del Compose, con copia y rollback. Nunca en actualización masiva.
                result["action_mode"] = "rewrite_tag"
                result["bulk_allowed"] = False
            else:
                # Tags flotantes (latest/stable/lts/15/11.8/8-alpine...) solo hacen pull del mismo tag.
                result["action_mode"] = "pull_only"
                result["bulk_allowed"] = True
            result["check"] = "update"
        else:
            result["check"] = "current"
    except Exception as e:
        result["error"] = str(e)
        result["check"] = "error"
        result["update"] = False
        result["actionable"] = False
        result["bulk_allowed"] = False
    return result


def main():
    global _immich_group
    rc, out, err = run([DOCKER_BIN, "ps", "-a", "--format", "{{json .}}"], 25)
    if rc != 0:
        raise SystemExit(err or "docker ps falló")
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        j = json.loads(line)
        rows.append({"name": j.get("Names") or "", "image": j.get("Image") or "", "status": j.get("Status") or ""})

    names = {r["name"] for r in rows}
    if {"immich_server", "immich_machine_learning"}.issubset(names):
        _immich_group = check_immich_group()
    else:
        _immich_group = None

    results = []
    # Pocas conexiones simultáneas para no castigar Docker Hub/GHCR.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        futures = [ex.submit(check_one, row) for row in rows]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    order = {r["name"]: i for i, r in enumerate(rows)}
    results.sort(key=lambda r: order.get(r["name"], 9999))
    group_updates = 1 if (_immich_group and _immich_group.get("update") and _immich_group.get("actionable")) else 0
    updates = sum(1 for r in results if r.get("update") and r.get("actionable")) + group_updates
    bulk_updates = sum(1 for r in results if r.get("update") and r.get("bulk_allowed"))
    manual_updates = sum(1 for r in results if r.get("update") and not r.get("actionable"))
    protected = sum(1 for r in results if r.get("check") == "protected")
    errors = sum(1 for r in results if r.get("check") == "error") + (
        1 if (_immich_group and _immich_group.get("check") == "error") else 0
    )
    payload = {
        "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(results), "updates": updates, "bulk_updates": bulk_updates,
        "manual_updates": manual_updates, "protected": protected, "errors": errors,
        "groups": ([_immich_group] if _immich_group else []), "containers": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="docker-updates-", suffix=".json", dir=str(OUT.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if _immich_group is None:
        immich_state = "Immich no instalado"
    elif _immich_group.get("check") == "error":
        immich_state = "Immich sin comprobar"
    else:
        immich_state = f"Immich {_immich_group.get('installed')}→{_immich_group.get('latest')}"
    print(f"Docker: {len(results)} comprobados · {updates} actualizables · {bulk_updates} seguros en lote · {manual_updates} manuales · {protected} protegidos · {errors} sin comprobar · {immich_state}")


if __name__ == "__main__":
    lock_path = "/run/server-dashboard/docker-check.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Docker: comprobación de versiones ya en curso")
            sys.exit(0)
        main()
