#!/usr/bin/env bash
# Self-contained Miura Panel installer. Run from an extracted source tree.
set -Eeuo pipefail
umask 027

APP='server-dashboard'
WEB_REL='var/www/server-dashboard'
BIN_REL='usr/local/sbin'
CONF_REL='etc/server-dashboard.conf'
STATE_REL='var/lib/server-dashboard'
BACKUP_REL='var/backups/server-dashboard'
MODE=''
UPGRADE=0
ROLLBACK=''
ROLLBACK_REQUESTED=0
ROOT_PREFIX=''
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PAYLOAD_TMP=''
BACKUP=''
INSTALL_STARTED=0
CONF_CREATED=0
CONF_MODIFIED=0
OLD_WEB=''
OLD_HOLDER=''
cleanup_payload() { [[ -z "$PAYLOAD_TMP" ]] || rm -rf "$PAYLOAD_TMP"; }
trap cleanup_payload EXIT

# A built installer carries its complete source payload after this script.
# When invoked from an extracted repository tree, use that tree directly.
if [[ ! -f "$SCRIPT_DIR/version.json" ]]; then
  MARKER_LINE="$(awk '/^__MIURA_PANEL_SOURCE_ARCHIVE_BELOW__$/ {print NR; exit}' "${BASH_SOURCE[0]}")"
  [[ -n "$MARKER_LINE" ]] || { echo 'ERROR: Falta version.json y no hay carga autocontenida' >&2; exit 1; }
  PAYLOAD_TMP="$(mktemp -d -t miura-panel-source.XXXXXX)"
  tail -n "+$((MARKER_LINE + 1))" "${BASH_SOURCE[0]}" | base64 -d > "$PAYLOAD_TMP/source.tar.gz" || { echo 'ERROR: Carga autocontenida dañada' >&2; exit 1; }
  python3 - "$PAYLOAD_TMP/source.tar.gz" "$PAYLOAD_TMP/source" <<'PY'
import pathlib, shutil, sys, tarfile
allowed = {
    'version.json', 'index.html', 'admin.html', 'admin-login.html', 'admin-reset.html', 'sep-logo.png',
    'collect.py', 'live.py', 'admin-web.py', 'admin-setup.py', 'admin-fleet.py', 'fleet.py',
    'fleet-docker.py', 'fleet-receiver.py', 'docker-action.py', 'docker-updates.py', 'update.py',
}
dest = pathlib.Path(sys.argv[2])
with tarfile.open(sys.argv[1], 'r:gz') as bundle:
    members = bundle.getmembers()
    if len(members) > 18:
        raise SystemExit('Carga autocontenida rechazada: demasiados miembros')
    found = set()
    source_dirs = 0
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or '\\' in member.name:
            raise SystemExit('Carga autocontenida rechazada: ruta insegura')
        if member.isdir() and member.name.rstrip('/') == 'source':
            source_dirs += 1
            continue
        if not member.isfile() or len(path.parts) != 2 or path.parts[0] != 'source' or path.parts[1] not in allowed:
            raise SystemExit('Carga autocontenida rechazada: archivo no permitido')
        if path.parts[1] in found or member.size > 8 * 1024 * 1024:
            raise SystemExit('Carga autocontenida rechazada: duplicado o archivo demasiado grande')
        found.add(path.parts[1])
    if found != allowed or source_dirs != 1:
        raise SystemExit('Carga autocontenida incompleta')
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if member.isdir():
            continue
        target = dest / path.parts[1]
        with bundle.extractfile(member) as source, target.open('xb') as output:
            shutil.copyfileobj(source, output)
        target.chmod(0o600)
PY
  SCRIPT_DIR="$PAYLOAD_TMP/source"
fi
usage() {
  cat <<'EOF'
Uso:
  sudo ./install-spanish-empire-panel.sh --normal
  sudo ./install-spanish-empire-panel.sh --demo
  sudo ./install-spanish-empire-panel.sh --upgrade
  sudo ./install-spanish-empire-panel.sh --rollback [directorio-copia]

Para pruebas aisladas, define MIURA_ROOT_PREFIX=/ruta/temporal. En ese modo no
se requieren privilegios y no se invoca systemd.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
while (($#)); do
  case "$1" in
    --normal) [[ -z "$MODE" ]] || fail 'Selecciona un solo modo'; MODE=normal ;;
    --demo) [[ -z "$MODE" ]] || fail 'Selecciona un solo modo'; MODE=demo ;;
    --upgrade) UPGRADE=1 ;;
    --rollback)
      ROLLBACK_REQUESTED=1
      if (($# >= 2)) && [[ "$2" != -* ]]; then ROLLBACK="$2"; shift 2; else shift; fi
      continue
      ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Opción desconocida: $1" ;;
  esac
  shift
done

ROOT_PREFIX="${MIURA_ROOT_PREFIX:-}"
if [[ -z "$ROOT_PREFIX" && $EUID -ne 0 ]]; then fail 'Ejecuta con sudo o define MIURA_ROOT_PREFIX para una prueba aislada'; fi
if [[ -n "$ROOT_PREFIX" ]]; then
  mkdir -p "$ROOT_PREFIX"
  ROOT_PREFIX="$(cd -- "$ROOT_PREFIX" && pwd -P)"
  [[ "$ROOT_PREFIX" != / ]] || fail 'MIURA_ROOT_PREFIX no puede ser /; usa una raíz temporal independiente'
  command -v python3 >/dev/null || fail 'La prueba aislada necesita python3 en el sistema que ejecuta el instalador'
else
  [[ -x /usr/bin/python3 ]] || fail 'No se encuentra /usr/bin/python3; instala Python 3 antes de continuar'
  /usr/bin/python3 -c 'import psutil' >/dev/null 2>&1 || fail 'Falta psutil para Python 3. Instala el paquete del sistema python3-psutil y vuelve a ejecutar el instalador'
  [[ -f /etc/debian_version ]] || fail 'Esta versión candidata requiere Debian o Ubuntu con systemd'
  command -v systemctl >/dev/null || fail 'No se encuentra systemctl; se requiere Debian/Ubuntu con systemd'
  [[ -d /run/systemd/system ]] || fail 'systemd no está activo; ejecuta el instalador en el sistema anfitrión, no en un contenedor sin systemd'
fi
root_path() { printf '%s/%s' "${ROOT_PREFIX%/}" "${1#/}"; }
assert_safe_path() {
  local rel="${1#/}" current="${ROOT_PREFIX:-}" part
  [[ -n "$current" ]] || current=/
  IFS='/' read -r -a components <<< "$rel"
  for part in "${components[@]}"; do
    [[ -n "$part" ]] || continue
    current="${current%/}/$part"
    [[ ! -L "$current" ]] || fail "Ruta de instalación enlazada; operación cancelada: $current"
    [[ ! -e "$current" || -d "$current" ]] || fail "Se esperaba un directorio de instalación: $current"
  done
}
assert_safe_leaf() {
  [[ ! -L "$1" ]] || fail "No se permite un enlace simbólico en un archivo gestionado: $1"
  [[ ! -e "$1" || -f "$1" ]] || fail "El destino gestionado debe ser un archivo regular: $1"
  if [[ -f "$1" ]] && (( $(stat -c '%h' -- "$1") > 1 )); then fail "No se permite un enlace físico en un archivo gestionado: $1"; fi
}

# Check every parent before reading configuration or creating backup/staging
# files. This also confines the root-prefix test harness when its tree is
# pre-populated with attacker-controlled symlinks.
assert_safe_path "$BACKUP_REL"
assert_safe_path "$WEB_REL"
assert_safe_path "$STATE_REL"
assert_safe_path "$BIN_REL"
assert_safe_path 'etc/systemd/system'
assert_safe_path "$(dirname "$CONF_REL")"
assert_safe_leaf "$(root_path "$CONF_REL")"
for rel in \
  usr/local/sbin/server-dashboard-collect.py usr/local/sbin/server-dashboard-live.py \
  usr/local/sbin/server-dashboard-admin.py usr/local/sbin/miura-panel-admin-setup \
  usr/local/sbin/sep-admin-fleet.py usr/local/sbin/miura-panel-fleet \
  usr/local/sbin/miura-panel-fleet-docker usr/local/sbin/spanish-empire-panel-fleet-receiver \
  usr/local/sbin/server-dashboard-docker-action.py usr/local/sbin/server-dashboard-docker-updates.py \
  usr/local/sbin/spanish-empire-panel-update \
  var/backups/server-dashboard/latest \
  etc/systemd/system/server-dashboard-web.service \
  etc/systemd/system/server-dashboard-collector.service \
  etc/systemd/system/server-dashboard-collector.timer; do
  assert_safe_leaf "$(root_path "$rel")"
done
WEB="$(root_path "$WEB_REL")"
BIN="$(root_path "$BIN_REL")"
CONF="$(root_path "$CONF_REL")"
STATE="$(root_path "$STATE_REL")"
BACKUPS="$(root_path "$BACKUP_REL")"

if ((ROLLBACK_REQUESTED)); then
  [[ -n "$ROLLBACK" ]] || ROLLBACK="$BACKUPS/latest"
  if [[ "$ROLLBACK" == "$BACKUPS/latest" ]]; then
    assert_safe_leaf "$ROLLBACK"
    [[ -f "$ROLLBACK" ]] || fail 'No existe la referencia a la última copia'
    ROLLBACK="$(cat "$ROLLBACK")"
  else
    [[ -d "$ROLLBACK" && ! -L "$ROLLBACK" ]] || fail 'La ruta de rollback debe ser el puntero latest o un directorio de copia'
  fi
  case "$ROLLBACK" in
    "$BACKUPS"/*)
      BACKUP_SUBPATH="${ROLLBACK#"$BACKUPS"/}"
      [[ "$BACKUP_SUBPATH" != .. && "$BACKUP_SUBPATH" != ../* && "$BACKUP_SUBPATH" != */../* && "$BACKUP_SUBPATH" != */.. ]] || fail 'Ruta de copia insegura'
      assert_safe_path "$BACKUP_REL/$BACKUP_SUBPATH"
      ;;
    *) fail 'La copia debe estar dentro del directorio de copias de Miura Panel' ;;
  esac
  ROLLBACK="$(realpath -- "$ROLLBACK")"
  BACKUPS_REAL="$(realpath -- "$BACKUPS")"
  [[ "$ROLLBACK" == "$BACKUPS_REAL"/* ]] || fail 'La copia debe estar dentro del directorio de copias de Miura Panel'
  ARCHIVE="$ROLLBACK/panel-backup.tar.gz"
  assert_safe_leaf "$ARCHIVE"
  [[ -f "$ARCHIVE" ]] || fail 'No existe una copia regular panel-backup.tar.gz'
  python3 - "$ARCHIVE" "${ROOT_PREFIX:-/}" <<'PY'
import os, pathlib, shutil, sys, tarfile, tempfile
import grp
archive = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2]).resolve()
web_rel = pathlib.PurePosixPath('var/www/server-dashboard')
managed = {
    'etc/systemd/system/server-dashboard-web.service',
    'etc/systemd/system/server-dashboard-collector.service',
    'etc/systemd/system/server-dashboard-collector.timer',
    'usr/local/sbin/server-dashboard-collect.py',
    'usr/local/sbin/server-dashboard-live.py',
    'usr/local/sbin/server-dashboard-admin.py',
    'usr/local/sbin/miura-panel-admin-setup',
    'usr/local/sbin/sep-admin-fleet.py',
    'usr/local/sbin/miura-panel-fleet',
    'usr/local/sbin/miura-panel-fleet-docker',
    'usr/local/sbin/spanish-empire-panel-fleet-receiver',
    'usr/local/sbin/server-dashboard-docker-action.py',
    'usr/local/sbin/server-dashboard-docker-updates.py',
    'usr/local/sbin/spanish-empire-panel-update',
}
with tempfile.TemporaryDirectory(prefix='miura-rollback-') as temp_name:
    stage = pathlib.Path(temp_name)
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        if len(members) > 10000 or sum(max(0, m.size) for m in members) > 512 * 1024 * 1024:
            raise SystemExit('Copia rechazada: excede los límites de tamaño')
        seen = set()
        for m in members:
            p = pathlib.PurePosixPath(m.name)
            name = str(p)
            if p.is_absolute() or '..' in p.parts or name in seen or not name or '\\' in m.name:
                raise SystemExit('Copia rechazada: ruta insegura o duplicada')
            seen.add(name)
            in_web = p == web_rel or web_rel in p.parents
            if not in_web and name not in managed:
                raise SystemExit('Copia rechazada: contiene una ruta ajena al panel')
            if not (m.isdir() or m.isfile()):
                raise SystemExit('Copia rechazada: no se admiten enlaces ni archivos especiales')
            if m.mode & 0o7000:
                raise SystemExit('Copia rechazada: permisos especiales no permitidos')
        # Manually extract regular files into a private temporary directory.
        for m in members:
            target = stage.joinpath(*pathlib.PurePosixPath(m.name).parts)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o750)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    raise SystemExit('Copia rechazada: archivo incompleto')
                with src, target.open('xb') as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                target.chmod(0o755 if m.mode & 0o111 else 0o644)
    # Replace only panel-owned destinations; mode/config and other tools remain.
    web = root.joinpath(*web_rel.parts)
    if web.is_symlink():
        raise SystemExit('Rollback rechazado: la ruta web instalada es un enlace')
    if web.exists(): shutil.rmtree(web)
    backup_web = stage.joinpath(*web_rel.parts)
    if backup_web.exists():
        web.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup_web, web, symlinks=False)
        if root == pathlib.Path('/'):
            www_gid = grp.getgrnam('www-data').gr_gid
            for path in [web, *web.rglob('*')]:
                os.chown(path, 0, www_gid)
                if path.is_dir(): path.chmod(0o750)
                elif path.is_file(): path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
    for rel in managed:
        dest = root.joinpath(*pathlib.PurePosixPath(rel).parts)
        if dest.is_symlink() or dest.is_file(): dest.unlink()
        elif dest.is_dir(): shutil.rmtree(dest)
        src = stage.joinpath(*pathlib.PurePosixPath(rel).parts)
        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            dest.chmod(src.stat().st_mode & 0o755)
PY
  if [[ -z "$ROOT_PREFIX" ]] && command -v systemctl >/dev/null; then
    systemctl daemon-reload
    systemctl restart server-dashboard-web.service
    systemctl enable --now server-dashboard-collector.timer
  fi
  printf 'Rollback completado desde %s\n' "$ROLLBACK"
  exit 0
fi

if ((UPGRADE)); then
  [[ -z "$MODE" ]] || fail '--upgrade conserva el modo existente; no combines --upgrade con --normal/--demo'
  [[ -f "$CONF" ]] || fail 'No existe configuración previa; usa --normal o --demo'
  [[ -f "$WEB/version.json" ]] || fail 'No se encuentra una instalación previa; usa --normal o --demo'
else
  [[ -n "$MODE" ]] || { usage >&2; fail 'Indica --normal o --demo en una instalación nueva'; }
  [[ ! -f "$WEB/version.json" ]] || fail 'Ya existe una instalación; usa --upgrade o --rollback'
fi

VERSION_FILE="$SCRIPT_DIR/version.json"
if [[ ! -f "$VERSION_FILE" ]]; then
  # Version metadata is shipped with the source bundle and never fetched here.
  fail 'Falta version.json en el árbol de fuentes'
fi
VERSION="$(python3 - "$VERSION_FILE" <<'PY'
import json, re, sys
try:
    value = json.load(open(sys.argv[1], encoding='utf-8'))['version']
except Exception as exc:
    raise SystemExit('version.json inválido: '+str(exc))
if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
    raise SystemExit('versión inválida en version.json')
print(value)
PY
)" || fail 'No se pudo validar version.json'

FILES=(index.html admin.html admin-login.html admin-reset.html sep-logo.png version.json)
for file in "${FILES[@]}"; do [[ -f "$SCRIPT_DIR/$file" ]] || fail "Falta archivo del panel: $file"; done
PY_FILES=(collect.py live.py admin-web.py admin-setup.py admin-fleet.py fleet.py fleet-docker.py fleet-receiver.py docker-action.py docker-updates.py update.py)
for file in "${PY_FILES[@]}"; do [[ -f "$SCRIPT_DIR/$file" ]] || fail "Falta archivo de aplicación: $file"; done
mkdir -p "$BACKUPS" "$WEB" "$BIN" "$STATE"
python3 - "$WEB" <<'PY'
import os, pathlib, stat, sys
root=pathlib.Path(sys.argv[1])
for parent, dirs, files in os.walk(root, followlinks=False):
    for name in dirs+files:
        path=pathlib.Path(parent)/name
        st=path.lstat(); mode=st.st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise SystemExit('El árbol web contiene enlaces o archivos especiales; corrígelo antes de actualizar: '+str(path))
        if stat.S_ISREG(mode) and st.st_nlink != 1:
            raise SystemExit('El árbol web contiene archivos con enlaces físicos; corrígelo antes de actualizar: '+str(path))
PY
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$(mktemp -d "$BACKUPS/$STAMP.XXXXXX")" || fail 'No se pudo reservar un directorio de copia único'
# Back up app files, deployed helpers, and service definitions. The config
# (including selected mode) is persistent and is never rewritten on upgrades.
tar -czf "$BACKUP/panel-backup.tar.gz" -C "${ROOT_PREFIX:-/}" \
  --ignore-failed-read "${WEB_REL#/}" \
  etc/systemd/system/server-dashboard-web.service \
  etc/systemd/system/server-dashboard-collector.service \
  etc/systemd/system/server-dashboard-collector.timer \
  usr/local/sbin/server-dashboard-collect.py \
  usr/local/sbin/server-dashboard-live.py \
  usr/local/sbin/server-dashboard-admin.py \
  usr/local/sbin/miura-panel-admin-setup \
  usr/local/sbin/sep-admin-fleet.py \
  usr/local/sbin/miura-panel-fleet \
  usr/local/sbin/miura-panel-fleet-docker \
  usr/local/sbin/spanish-empire-panel-fleet-receiver \
  usr/local/sbin/server-dashboard-docker-action.py \
  usr/local/sbin/server-dashboard-docker-updates.py \
  usr/local/sbin/spanish-empire-panel-update 2>/dev/null || {
    rm -rf "$BACKUP"
    fail 'No se pudo crear la copia previa; instalación cancelada'
  }
if [[ -f "$CONF" ]]; then
  cp -p "$CONF" "$BACKUP/server-dashboard.conf"
  chmod 0600 "$BACKUP/server-dashboard.conf"
fi
LATEST_TMP="$(mktemp "$BACKUPS/.latest.XXXXXX")" || fail 'No se pudo preparar el puntero a la copia'
printf '%s\n' "$BACKUP" > "$LATEST_TMP"
mv -fT "$LATEST_TMP" "$BACKUPS/latest"

OLD_MODE=''
if [[ -f "$CONF" ]]; then
  OLD_MODE="$(python3 - "$CONF" <<'PY'
import sys
values=[]
for line in open(sys.argv[1], encoding='utf-8'):
    key, sep, value = line.strip().partition('=')
    if sep and key.strip() == 'DASHBOARD_MODE':
        values.append(value.strip().strip('"').strip("'"))
if len(values)>1: print('__INVALID__')
elif values: print(values[0])
else: print('__MISSING__')
PY
)"
fi
if ((UPGRADE)); then
  if [[ "$OLD_MODE" == __MISSING__ ]]; then
    MODE=normal
  else
    [[ "$OLD_MODE" == normal || "$OLD_MODE" == demo ]] || fail 'DASHBOARD_MODE previo inválido o duplicado; no se cambiará silenciosamente'
    MODE="$OLD_MODE"
  fi
fi

WEB_PARENT="$(dirname "$WEB")"
STAGE="$(mktemp -d "$WEB_PARENT/.server-dashboard.install-stage-$STAMP.XXXXXX")"
chmod 0750 "$STAGE"
INSTALL_STARTED=1
cleanup() {
  local status=$?
  trap - EXIT
  if ((status != 0 && INSTALL_STARTED)); then
    if ! MIURA_ROOT_PREFIX="$ROOT_PREFIX" bash "${BASH_SOURCE[0]}" --rollback "$BACKUP" >&2; then
      printf 'ERROR: falló también el rollback automático; copia preservada en %s\n' "$BACKUP" >&2
    fi
    if ((CONF_CREATED)); then
      rm -f "$CONF"
    elif ((CONF_MODIFIED)) && [[ -f "$BACKUP/server-dashboard.conf" ]]; then
      CONF_RESTORE="$(mktemp "$(dirname "$CONF")/.server-dashboard.conf.restore.XXXXXX")"
      cp -p "$BACKUP/server-dashboard.conf" "$CONF_RESTORE"
      mv -fT "$CONF_RESTORE" "$CONF"
    fi
  fi
  [[ -z "$OLD_HOLDER" || ! -e "$OLD_HOLDER" ]] || rm -rf "$OLD_HOLDER"
  rm -rf "$STAGE"
  [[ -z "$PAYLOAD_TMP" ]] || rm -rf "$PAYLOAD_TMP"
  return "$status"
}
trap cleanup EXIT
for file in "${FILES[@]}"; do
  if [[ "$file" == sep-logo.png ]]; then
    mkdir -m 0755 "$STAGE/assets"
    install -m 0644 "$SCRIPT_DIR/$file" "$STAGE/assets/sep-logo.png"
  else
    install -m 0644 "$SCRIPT_DIR/$file" "$STAGE/$file"
  fi
done
if [[ "$MODE" == normal ]]; then
  for file in data.json history.json docker-updates.json panel-update.json fleet-role.json admin-status.json; do
    [[ -f "$WEB/$file" && ! -L "$WEB/$file" ]] && cp -a "$WEB/$file" "$STAGE/$file"
  done
fi
# Publish the complete web tree as one directory swap.
OLD_HOLDER="$(mktemp -d "$WEB_PARENT/.server-dashboard.swap-$STAMP.XXXXXX")"
OLD_WEB="$OLD_HOLDER/tree"
mv -T "$WEB" "$OLD_WEB"
mv -T "$STAGE" "$WEB"

declare -A ALIASES=(
  [collect.py]=server-dashboard-collect.py [live.py]=server-dashboard-live.py
  [admin-web.py]=server-dashboard-admin.py [admin-setup.py]=miura-panel-admin-setup
  [admin-fleet.py]=sep-admin-fleet.py [fleet.py]=miura-panel-fleet
  [fleet-docker.py]=miura-panel-fleet-docker [fleet-receiver.py]=spanish-empire-panel-fleet-receiver
  [docker-action.py]=server-dashboard-docker-action.py [docker-updates.py]=server-dashboard-docker-updates.py
  [update.py]=spanish-empire-panel-update
)
for src in "${!ALIASES[@]}"; do
  [[ -f "$SCRIPT_DIR/$src" ]] || continue
  install -m 0750 "$SCRIPT_DIR/$src" "$BIN/${ALIASES[$src]}"
done

if [[ ! -f "$CONF" ]]; then
  CONF_CREATED=1
  mkdir -p "$(dirname "$CONF")"
  cat > "$CONF" <<EOF
# Miura Panel configuration. Updates preserve this file.
DASHBOARD_MODE=$MODE
DASHBOARD_BIND_IP=0.0.0.0
DASHBOARD_PORT=8766
DASHBOARD_SMART=1
DASHBOARD_DOCKER_UPDATER=0
EOF
  chmod 0640 "$CONF"
fi
if ((UPGRADE)) && [[ "$OLD_MODE" == __MISSING__ ]]; then
  CONF_MODIFIED=1
  python3 - "$CONF" <<'PY'
import os, pathlib, stat, sys, tempfile
path=pathlib.Path(sys.argv[1])
original=path.read_bytes()
if any(line.partition(b'=')[0].strip()==b'DASHBOARD_MODE' for line in original.splitlines()):
    raise SystemExit('DASHBOARD_MODE apareció durante la migración; cancelo')
separator=b'' if not original or original.endswith(b'\n') else b'\n'
updated=original+separator+b'DASHBOARD_MODE=normal\n'
st=path.stat()
fd,tmp=tempfile.mkstemp(prefix=path.name+'.pending-', dir=path.parent)
try:
    with os.fdopen(fd,'wb') as output:
        output.write(updated); output.flush(); os.fsync(output.fileno())
    os.chmod(tmp, stat.S_IMODE(st.st_mode)); os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp,path)
    dirfd=os.open(path.parent,os.O_RDONLY)
    try: os.fsync(dirfd)
    finally: os.close(dirfd)
finally:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
PY
fi
if [[ -z "$ROOT_PREFIX" ]]; then chown root:www-data "$CONF"; fi
chmod 0640 "$CONF"
if [[ -z "$ROOT_PREFIX" ]]; then chown -R root:www-data "$WEB"; fi
chmod 0750 "$WEB"

mkdir -p "$(root_path etc/systemd/system)"
cat > "$(root_path etc/systemd/system/server-dashboard-web.service)" <<'EOF'
[Unit]
Description=Miura Panel read-only web interface
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-live.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
cat > "$(root_path etc/systemd/system/server-dashboard-collector.service)" <<'EOF'
[Unit]
Description=Miura Panel system data collector
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-collect.py
EOF
cat > "$(root_path etc/systemd/system/server-dashboard-collector.timer)" <<'EOF'
[Unit]
Description=Refresh Miura Panel data every minute

[Timer]
OnBootSec=20s
OnUnitActiveSec=60s
Unit=server-dashboard-collector.service

[Install]
WantedBy=timers.target
EOF

if [[ -z "$ROOT_PREFIX" ]] && command -v systemctl >/dev/null; then
  systemctl daemon-reload
  systemctl enable --now server-dashboard-web.service server-dashboard-collector.timer
  systemctl restart server-dashboard-web.service
fi
rm -rf "$OLD_HOLDER"
OLD_WEB=''
OLD_HOLDER=''
printf 'Miura Panel v%s instalado en modo %s. Copia previa: %s\n' "$VERSION" "$MODE" "$BACKUP"
exit 0
