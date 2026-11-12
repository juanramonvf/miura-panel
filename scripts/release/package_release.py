#!/usr/bin/env python3
"""Build and validate the exact Miura Panel release artifacts in a scratch dir."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAX_LEGACY_INSTALLER = 2 * 1024 * 1024  # v1.0.34 updater limit
MARKER = b"__MIURA_PANEL_SOURCE_ARCHIVE_BELOW__\n"
ALLOWED = {
    "version.json", "index.html", "admin.html", "admin-login.html", "admin-reset.html", "sep-logo.png",
    "collect.py", "live.py", "admin-web.py", "admin-setup.py", "admin-fleet.py", "fleet.py",
    "fleet-docker.py", "fleet-receiver.py", "docker-action.py", "docker-updates.py", "update.py",
}


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"ERROR: {message}")


def checked_version(path: pathlib.Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        fail(f"version.json inválido: {exc}")
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        fail("versión inválida; se exige semver X.Y.Z sin sufijo")
    return value


def verify_installer(installer: pathlib.Path, version: str) -> str:
    raw = installer.read_bytes()
    if len(raw) > MAX_LEGACY_INSTALLER:
        fail(f"instalador supera 2 MiB y deja de ser compatible con el actualizador v1.0.34 ({len(raw)} bytes)")
    digest = hashlib.sha256(raw).hexdigest()
    checked = subprocess.run(["sha256sum", "-c", installer.name + ".sha256"], cwd=installer.parent,
                             text=True, capture_output=True)
    if checked.returncode:
        fail(f"SHA256 del instalador no coincide: {checked.stderr.strip()}")
    subprocess.run(["bash", "-n", str(installer)], check=True)
    if MARKER not in raw:
        fail("no se encontró el marcador de payload autocontenido")
    encoded = raw.split(MARKER, 1)[1]
    try:
        archive = base64.b64decode(b"".join(encoded.split()), validate=True)
    except Exception as exc:
        fail(f"payload base64 inválido: {exc}")
    with tempfile.TemporaryDirectory(prefix="miura-payload-check-") as temp:
        bundle = pathlib.Path(temp) / "source.tar.gz"
        bundle.write_bytes(archive)
        with tarfile.open(bundle, "r:gz") as tar:
            members = tar.getmembers()
            dirs = [m for m in members if m.isdir()]
            files = [m for m in members if m.isfile()]
            if len(dirs) + len(files) != len(members):
                fail("payload contiene enlaces o miembros especiales")
            if len(dirs) != 1 or dirs[0].name.rstrip("/") != "source":
                fail("payload debe contener solo el directorio source")
            if len(files) != len(ALLOWED):
                fail("payload incompleto o con miembros duplicados")
            found: set[str] = set()
            for member in files:
                p = pathlib.PurePosixPath(member.name)
                if (p.is_absolute() or ".." in p.parts or "\\" in member.name or
                        len(p.parts) != 2 or p.parts[0] != "source" or p.parts[1] not in ALLOWED or
                        p.parts[1] in found or member.size > 8 * 1024 * 1024):
                    fail(f"miembro de payload inseguro/no permitido: {member.name}")
                found.add(p.parts[1])
                embedded = tar.extractfile(member)
                if embedded is None or embedded.read() != (ROOT / p.parts[1]).read_bytes():
                    fail(f"payload no coincide con el árbol fuente: {member.name}")
            if found != ALLOWED:
                fail("payload no contiene el conjunto de archivos permitido")
            embedded_version = json.loads(tar.extractfile("source/version.json").read())
            if embedded_version.get("version") != version:
                fail("versión de version.json dentro del payload no coincide con la versión objetivo")
    return digest


def test_demo_install(installer: pathlib.Path, version: str) -> None:
    """Exercise install and upgrade without touching host paths or systemd."""
    with tempfile.TemporaryDirectory(prefix="miura-demo-release-") as temp:
        prefix = pathlib.Path(temp) / "root"
        env = os.environ.copy()
        env["MIURA_ROOT_PREFIX"] = str(prefix)
        subprocess.run(["bash", str(installer), "--demo"], env=env, check=True)
        config = prefix / "etc/server-dashboard.conf"
        web = prefix / "var/www/server-dashboard"
        if f'"version": "{version}"' not in (web / "version.json").read_text(encoding="utf-8"):
            fail("instalación demo no contiene la versión objetivo")
        text = config.read_text(encoding="utf-8")
        if "DASHBOARD_MODE=demo" not in text:
            fail("instalador no creó modo demo")
        if "DASHBOARD_BIND_IP=127.0.0.1" not in text:
            fail("instalación nueva no usa bind local por defecto")
        config.write_text(text + "DASHBOARD_TITLE=Release smoke test\n", encoding="utf-8")
        subprocess.run(["bash", str(installer), "--upgrade"], env=env, check=True)
        after = config.read_text(encoding="utf-8")
        if "DASHBOARD_MODE=demo" not in after or "DASHBOARD_TITLE=Release smoke test" not in after:
            fail("upgrade ha perdido modo demo o configuración persistente")
        if f'"version": "{version}"' not in (web / "version.json").read_text(encoding="utf-8"):
            fail("upgrade demo no conserva la versión objetivo")
        for unit in ("server-dashboard-web.service", "server-dashboard-collector.service",
                     "server-dashboard-collector.timer", "server-dashboard-admin.service"):
            if not (prefix / "etc/systemd/system" / unit).is_file():
                fail(f"no se generó la unidad {unit}")
        analyzer = shutil.which("systemd-analyze")
        if not analyzer:
            fail("systemd-analyze es obligatorio para validar las unidades del paquete")
        units = [str(prefix / "etc/systemd/system" / name) for name in (
            "server-dashboard-web.service", "server-dashboard-collector.service",
            "server-dashboard-collector.timer", "server-dashboard-admin.service")]
        subprocess.run([analyzer, "verify", *units], check=True)
        admin_unit = (prefix / "etc/systemd/system/server-dashboard-admin.service").read_text(encoding="utf-8")
        if "User=sep-admin\n" not in admin_unit or "Group=sep-admin\n" not in admin_unit:
            fail("administration unit does not run as the dedicated sep-admin account")
        if "ProtectSystem=strict\n" not in admin_unit or "NoNewPrivileges=true\n" not in admin_unit:
            fail("administration unit is missing required sandbox controls")
        if "ReadWritePaths=/var/lib/server-dashboard/preferences\n" not in admin_unit:
            fail("administration unit has an unexpected writable path")
        if "UnsetEnvironment=MIURA_ROOT_PREFIX\n" not in admin_unit:
            fail("administration unit may inherit the isolated test root prefix")
        if (prefix / "etc/systemd/system/multi-user.target.wants/server-dashboard-admin.service").exists():
            fail("clean demo install must leave authenticated administration disabled")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="versión semver a empaquetar; debe coincidir con version.json")
    parser.add_argument("output_dir", type=pathlib.Path)
    args = parser.parse_args()
    version = checked_version(ROOT / "version.json")
    if args.version != version:
        fail(f"tag/versión {args.version} no coincide con version.json {version}")
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        fail(f"el directorio de salida no está vacío: {out}")
    env = os.environ.copy()
    env["MIURA_OUTPUT_DIR"] = str(out)
    subprocess.run(["bash", str(ROOT / "build-installer.sh")], env=env, check=True)
    installer = out / f"install-spanish-empire-panel-v{version}.sh"
    digest = verify_installer(installer, version)
    test_demo_install(installer, version)
    manifest = {
        "version": version,
        "installer": f"https://github.com/juanramonvf/miura-panel/releases/download/v{version}/{installer.name}",
        "sha256": digest,
    }
    manifest_path = out / "miura-panel-update-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["sha256sum", manifest_path.name], cwd=out, check=True,
                   stdout=(out / (manifest_path.name + ".sha256")).open("w", encoding="utf-8"))
    notes = ROOT / f"RELEASE_NOTES_v{version}.md"
    if not notes.is_file():
        fail(f"faltan notas de publicación: {notes.name}")
    if not re.search(rf"\bv{re.escape(version)}\b", "\n".join(notes.read_text(encoding="utf-8").splitlines()[:5])):
        fail(f"las notas no identifican la versión objetivo v{version}")
    (out / "RELEASE_NOTES.md").write_bytes(notes.read_bytes())
    manifest_info = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest_info) != {"version", "installer", "sha256"} or manifest_info["sha256"] != digest:
        fail("manifiesto generado inválido")
    print(f"Validated release v{version}: {installer} ({installer.stat().st_size} bytes), SHA256 {digest}")


if __name__ == "__main__":
    main()
