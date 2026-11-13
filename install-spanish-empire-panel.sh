#!/usr/bin/env bash
# Self-contained Miura Panel installer. Run from an extracted source tree.
set -Eeuo pipefail
umask 027

APP='server-dashboard'
WEB_REL='var/www/server-dashboard'
BIN_REL='usr/local/sbin'
CONF_REL='etc/server-dashboard.conf'
STATE_REL='var/lib/server-dashboard'
PREFS_REL="$STATE_REL/preferences"
PREFS_FILE_REL="$PREFS_REL/ui.json"
READ_GROUP='server-dashboard-read'
ADMIN_GROUP='sep-admin'
ADMIN_USER='sep-admin'
ADMIN_STATE_REL='var/lib/spanish-empire-panel-admin'
ADMIN_UNIT_REL='etc/systemd/system/server-dashboard-admin.service'
ADMIN_DROPIN_REL='etc/systemd/system/server-dashboard-admin.service.d'
ADMIN_DROPIN_FILE_REL="$ADMIN_DROPIN_REL/miura-preferences.conf"
BACKUP_REL='var/backups/server-dashboard'
LEGACY_ADMIN_ALIAS_REL='usr/local/sbin/miura-panel-admin-setup'
LEGACY_ADMIN_TARGET_REL='usr/local/sbin/spanish-empire-panel-admin-setup'
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
assert_safe_root_prefix_systemd_wants() {
  [[ -n "$ROOT_PREFIX" ]] || return 0
  # systemctl --root creates/removes entries below these paths. Validate the
  # target directories and every ancestor before it can follow an external
  # symlink or write outside the isolated root.
  assert_safe_path 'etc/systemd/system/multi-user.target.wants'
  assert_safe_path 'etc/systemd/system/timers.target.wants'
}
assert_safe_leaf() {
  [[ ! -L "$1" ]] || fail "No se permite un enlace simbólico en un archivo gestionado: $1"
  [[ ! -e "$1" || -f "$1" ]] || fail "El destino gestionado debe ser un archivo regular: $1"
  if [[ -f "$1" ]] && (( $(stat -c '%h' -- "$1") > 1 )); then fail "No se permite un enlace físico en un archivo gestionado: $1"; fi
}
assert_safe_root_owned_chain() {
  local path="$1" boundary expected_uid owner mode
  boundary="${ROOT_PREFIX:-/}"
  boundary="$(cd -- "$boundary" && pwd -P)"
  expected_uid=0
  [[ -z "$ROOT_PREFIX" ]] || expected_uid="$EUID"
  while [[ ! -e "$path" ]]; do
    [[ "$path" != "$boundary" && "$path" != / ]] || fail "No existe un directorio seguro para el drop-in: $path"
    path="$(dirname -- "$path")"
  done
  [[ -d "$path" && ! -L "$path" ]] || fail "El padre del drop-in debe ser un directorio real: $path"
  while :; do
    owner="$(stat -c '%u' -- "$path")"
    mode="$(stat -c '%a' -- "$path")"
    [[ "$owner" == "$expected_uid" ]] || fail "Un directorio del drop-in no pertenece al propietario esperado: $path"
    (( (8#$mode & 0022) == 0 )) || fail "Un directorio del drop-in permite escritura a grupo u otros: $path"
    [[ "$path" == "$boundary" ]] && break
    [[ "$path" != / ]] || fail 'No se pudo confinar la comprobación del drop-in al root prefix'
    path="$(dirname -- "$path")"
  done
}
assert_safe_legacy_admin_alias() {
  local path="$1" expected_target target_path parent_path expected_uid target_uid root_boundary
  local target_mode target_links parent_uid parent_mode
  expected_target='spanish-empire-panel-admin-setup'
  [[ "$path" == "$(root_path "$LEGACY_ADMIN_ALIAS_REL")" && -L "$path" ]] ||
    fail "Solo se admite el alias heredado conocido de Miura Panel: $path"
  [[ "$(readlink -- "$path")" == "$expected_target" ]] ||
    fail "El alias heredado de administración tiene un destino no permitido: $path"
  target_path="$(root_path "$LEGACY_ADMIN_TARGET_REL")"
  parent_path="$(dirname -- "$path")"
  [[ -f "$target_path" && ! -L "$target_path" ]] ||
    fail "El destino del alias heredado debe ser un archivo regular: $target_path"

  # A normal installation runs as root. In a root-prefix test, the fixture
  # belongs to the test runner, so validate against that isolated owner.
  expected_uid=0
  [[ -z "$ROOT_PREFIX" ]] || expected_uid="$EUID"
  target_uid="$(stat -c '%u' -- "$target_path")"
  target_mode="$(stat -c '%a' -- "$target_path")"
  target_links="$(stat -c '%h' -- "$target_path")"
  [[ "$target_uid" == "$expected_uid" ]] ||
    fail "El destino del alias heredado no pertenece al usuario esperado: $target_path"
  (( (8#$target_mode & 0022) == 0 && (8#$target_mode & 07000) == 0 && (8#$target_mode & 0100) != 0 )) ||
    fail "El destino del alias heredado debe ser ejecutable por su propietario y no escribible por grupo u otros: $target_path"
  root_boundary="${ROOT_PREFIX:-/}"
  root_boundary="$(cd -- "$root_boundary" && pwd -P)"
  while :; do
    parent_uid="$(stat -c '%u' -- "$parent_path")"
    parent_mode="$(stat -c '%a' -- "$parent_path")"
    [[ "$parent_uid" == "$expected_uid" ]] ||
      fail "Un directorio del alias heredado no pertenece al usuario esperado: $parent_path"
    (( (8#$parent_mode & 0022) == 0 )) ||
      fail "Un directorio del alias heredado no puede ser escribible por grupo u otros: $parent_path"
    [[ "$parent_path" == "$root_boundary" ]] && break
    [[ "$parent_path" == / ]] && fail 'No se pudo confinar la comprobación del alias heredado al root prefix'
    parent_path="$(dirname -- "$parent_path")"
  done
  [[ "$target_links" == 1 ]] || fail "No se permite un destino heredado con enlaces físicos: $target_path"
}
assert_safe_managed_leaf() {
  local path="$1"
  if [[ "$path" == "$(root_path "$LEGACY_ADMIN_ALIAS_REL")" && -L "$path" ]]; then
    assert_safe_legacy_admin_alias "$path"
  else
    assert_safe_leaf "$path"
  fi
}

# Check every parent before reading configuration or creating backup/staging
# files. This also confines the root-prefix test harness when its tree is
# pre-populated with attacker-controlled symlinks.
assert_safe_path "$BACKUP_REL"
assert_safe_path "$WEB_REL"
assert_safe_path "$STATE_REL"
assert_safe_path "$ADMIN_STATE_REL"
assert_safe_path "$PREFS_REL"
assert_safe_path "$ADMIN_DROPIN_REL"
assert_safe_path "$BIN_REL"
assert_safe_path 'etc/systemd/system'
assert_safe_path "$(dirname "$CONF_REL")"
assert_safe_leaf "$(root_path "$CONF_REL")"
assert_safe_leaf "$(root_path "$PREFS_FILE_REL")"
assert_safe_leaf "$(root_path "$ADMIN_DROPIN_FILE_REL")"
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
  etc/systemd/system/server-dashboard-collector.timer \
  etc/systemd/system/server-dashboard-admin.service; do
  assert_safe_managed_leaf "$(root_path "$rel")"
done
ADMIN_UNIT_PRESENT=0
ADMIN_UNIT_MANAGED=0
if [[ -n "$ROOT_PREFIX" ]]; then
  for candidate in \
    "$(root_path "$ADMIN_UNIT_REL")" \
    "$(root_path usr/lib/systemd/system/server-dashboard-admin.service)" \
    "$(root_path lib/systemd/system/server-dashboard-admin.service)"; do
    if [[ -f "$candidate" ]]; then ADMIN_UNIT_PRESENT=1; break; fi
  done
elif systemctl cat server-dashboard-admin.service >/dev/null 2>&1; then
  ADMIN_UNIT_PRESENT=1
fi
if ((ADMIN_UNIT_PRESENT)); then
  for candidate in \
    "$(root_path "$ADMIN_UNIT_REL")" \
    "$(root_path usr/lib/systemd/system/server-dashboard-admin.service)" \
    "$(root_path lib/systemd/system/server-dashboard-admin.service)"; do
    if [[ -f "$candidate" && ! -L "$candidate" ]] && \
       grep -Fqx 'User=sep-admin' "$candidate" && \
       grep -Fqx 'Group=sep-admin' "$candidate" && \
       grep -Fqx 'ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-admin.py' "$candidate" && \
       grep -Fqx 'ProtectSystem=strict' "$candidate" && \
       grep -Fqx 'ReadWritePaths=/var/lib/server-dashboard/preferences' "$candidate"; then
      ADMIN_UNIT_MANAGED=1
      break
    fi
  done
fi
if ((ADMIN_UNIT_PRESENT)) && [[ -e "$(root_path "$ADMIN_DROPIN_FILE_REL")" ]]; then
  printf '[Service]\nSupplementaryGroups=server-dashboard-prefs\nSupplementaryGroups=%s\nReadWritePaths=/var/lib/server-dashboard/preferences\n' "$READ_GROUP" |
    cmp -s - "$(root_path "$ADMIN_DROPIN_FILE_REL")" ||
    printf '[Service]\nSupplementaryGroups=server-dashboard-prefs\nReadWritePaths=/var/lib/server-dashboard/preferences\n' |
      cmp -s - "$(root_path "$ADMIN_DROPIN_FILE_REL")" ||
    fail 'El drop-in de preferencias de server-dashboard-admin.service contiene cambios no gestionados; no se sobrescribirá'
fi
if ((ADMIN_UNIT_PRESENT)); then
  assert_safe_root_owned_chain "$(root_path "$ADMIN_DROPIN_REL")"
  ADMIN_DROPIN_FILE="$(root_path "$ADMIN_DROPIN_FILE_REL")"
  if [[ -e "$ADMIN_DROPIN_FILE" ]]; then
    expected_dropin_uid=0
    [[ -z "$ROOT_PREFIX" ]] || expected_dropin_uid="$EUID"
    [[ "$(stat -c '%u' -- "$ADMIN_DROPIN_FILE")" == "$expected_dropin_uid" ]] ||
      fail 'El drop-in existente no pertenece al propietario esperado'
    existing_mode="$(stat -c '%a' -- "$ADMIN_DROPIN_FILE")"
    (( (8#$existing_mode & 0022) == 0 )) || fail 'El drop-in existente permite escritura a grupo u otros'
  fi
fi
if [[ -z "$ROOT_PREFIX" ]] && ! getent group server-dashboard-prefs >/dev/null; then
  command -v groupadd >/dev/null || fail 'Falta groupadd para crear el grupo server-dashboard-prefs'
  groupadd --system server-dashboard-prefs || fail 'No se pudo crear el grupo server-dashboard-prefs'
fi
if [[ -z "$ROOT_PREFIX" ]] && ! getent group "$READ_GROUP" >/dev/null; then
  command -v groupadd >/dev/null || fail "Falta groupadd para crear el grupo $READ_GROUP"
  groupadd --system "$READ_GROUP" || fail "No se pudo crear el grupo $READ_GROUP"
fi
if [[ -z "$ROOT_PREFIX" ]]; then
  PREFS_GROUP_ENTRY="$(getent group server-dashboard-prefs)" || fail 'No se encuentra el grupo server-dashboard-prefs'
  PREFS_GROUP_MEMBERS="${PREFS_GROUP_ENTRY##*:}"
  [[ -z "$PREFS_GROUP_MEMBERS" ]] || fail 'El grupo server-dashboard-prefs debe ser exclusivo y no tener miembros añadidos'
  READ_GROUP_ENTRY="$(getent group "$READ_GROUP")" || fail "No se encuentra el grupo $READ_GROUP"
  READ_GROUP_MEMBERS="${READ_GROUP_ENTRY##*:}"
  [[ -z "$READ_GROUP_MEMBERS" ]] || fail "El grupo $READ_GROUP debe estar vacío; solo se concede por las unidades systemd gestionadas"
  READ_GID="$(cut -d: -f3 <<< "$READ_GROUP_ENTRY")"
  if getent passwd "$ADMIN_USER" >/dev/null && ! getent group "$ADMIN_GROUP" >/dev/null; then
    fail "La cuenta $ADMIN_USER existe sin su grupo $ADMIN_GROUP; revisa esa cuenta antes de instalar"
  fi
  if ! getent group "$ADMIN_GROUP" >/dev/null; then
    command -v groupadd >/dev/null || fail "Falta groupadd para crear el grupo $ADMIN_GROUP"
    groupadd --system "$ADMIN_GROUP" || fail "No se pudo crear el grupo $ADMIN_GROUP"
  fi
  ADMIN_GROUP_ENTRY="$(getent group "$ADMIN_GROUP")" || fail "No se encuentra el grupo $ADMIN_GROUP"
  ADMIN_GID="$(cut -d: -f3 <<< "$ADMIN_GROUP_ENTRY")"
  ADMIN_GROUP_MEMBERS="${ADMIN_GROUP_ENTRY##*:}"
  [[ -z "$ADMIN_GROUP_MEMBERS" ]] || fail "El grupo $ADMIN_GROUP debe estar vacío; se usa como grupo primario dedicado"
  if ! getent passwd "$ADMIN_USER" >/dev/null; then
    command -v useradd >/dev/null || fail "Falta useradd para crear la cuenta $ADMIN_USER"
    useradd --system --no-create-home --gid "$ADMIN_GROUP" \
      --home-dir "/$ADMIN_STATE_REL" --shell /usr/sbin/nologin "$ADMIN_USER" ||
      fail "No se pudo crear la cuenta de servicio $ADMIN_USER"
  fi
  ADMIN_PASSWD_ENTRY="$(getent passwd "$ADMIN_USER")" || fail "No se encuentra la cuenta $ADMIN_USER"
  ADMIN_UID="$(cut -d: -f3 <<< "$ADMIN_PASSWD_ENTRY")"
  ADMIN_USER_GID="$(cut -d: -f4 <<< "$ADMIN_PASSWD_ENTRY")"
  ADMIN_SHELL="$(cut -d: -f7 <<< "$ADMIN_PASSWD_ENTRY")"
  [[ "$ADMIN_USER_GID" == "$ADMIN_GID" ]] || fail "La cuenta $ADMIN_USER no usa su grupo primario dedicado"
  [[ "$ADMIN_SHELL" == /usr/sbin/nologin || "$ADMIN_SHELL" == /bin/false ]] ||
    fail "La cuenta $ADMIN_USER debe tener una shell no interactiva"
  ADMIN_USER_GROUPS="$(id -Gn "$ADMIN_USER")" || fail "No se pudieron comprobar los grupos de $ADMIN_USER"
  [[ "$ADMIN_USER_GROUPS" == "$ADMIN_GROUP" ]] ||
    fail "La cuenta $ADMIN_USER pertenece a grupos adicionales; revísala antes de instalar"
else
  # Isolated root-prefix runs do not create host groups. Reuse www-data's
  # numeric gid solely inside the fixture so effective traversal can be tested.
  READ_GID="$(getent group "$READ_GROUP" | cut -d: -f3 || true)"
  if [[ -z "$READ_GID" ]]; then READ_GID="$(getent group www-data | cut -d: -f3 || true)"; fi
  ADMIN_GID="$(getent group "$ADMIN_GROUP" | cut -d: -f3 || true)"
  ADMIN_UID="$(getent passwd "$ADMIN_USER" | cut -d: -f3 || true)"
  ADMIN_GID="${ADMIN_GID:-$READ_GID}"
  ADMIN_UID="${ADMIN_UID:-29994}"
  if [[ -n "$ROOT_PREFIX" ]]; then
    ADMIN_GID="${ADMIN_GID:-29994}"
    ADMIN_UID="${ADMIN_UID:-29994}"
    python3 - "$ROOT_PREFIX" "$ADMIN_UID" "$ADMIN_GID" "$ADMIN_USER" "$ADMIN_GROUP" "$ADMIN_STATE_REL" <<'PY'
import os, pathlib, stat, sys
uid=int(sys.argv[2]); gid=int(sys.argv[3])
user,group,home=sys.argv[4:]

def fail(message):
    raise SystemExit(message)

root_arg=pathlib.Path(os.path.abspath(sys.argv[1]))
try:
    root=root_arg.resolve(strict=True)
except OSError as exc:
    fail('No se pudo resolver el root prefix para las cuentas ficticias: '+str(exc))
if root != root_arg or not root.is_dir():
    fail('El root prefix para las cuentas ficticias debe ser un directorio físico')

directory=os.O_RDONLY | getattr(os,'O_DIRECTORY',0) | getattr(os,'O_CLOEXEC',0) | getattr(os,'O_NOFOLLOW',0)
no_follow=getattr(os,'O_NOFOLLOW',0)
if not no_follow or not getattr(os,'O_DIRECTORY',0):
    fail('La plataforma no permite abrir de forma segura los archivos de cuentas del root prefix')
try:
    root_fd=os.open(root, directory)
except OSError as exc:
    fail('No se pudo abrir el root prefix de forma segura: '+str(exc))
etc_fd=None
try:
    try:
        etc_stat=os.stat('etc',dir_fd=root_fd,follow_symlinks=False)
    except FileNotFoundError:
        os.mkdir('etc',0o755,dir_fd=root_fd)
        etc_stat=os.stat('etc',dir_fd=root_fd,follow_symlinks=False)
    if not stat.S_ISDIR(etc_stat.st_mode):
        fail('La ruta etc del root prefix debe ser un directorio regular, no un enlace')
    etc_path=root/'etc'
    if etc_path.resolve(strict=True) != etc_path:
        fail('La ruta etc escapa del root prefix')
    etc_fd=os.open('etc',directory,dir_fd=root_fd)
    opened_etc=os.fstat(etc_fd)
    if not stat.S_ISDIR(opened_etc.st_mode) or (opened_etc.st_dev,opened_etc.st_ino)!=(etc_stat.st_dev,etc_stat.st_ino):
        fail('La ruta etc del root prefix cambió durante la validación')

    # Validate both account databases before opening either one. This prevents
    # a bad group path from being discovered after passwd has already changed.
    databases={}
    for name in ('passwd','group'):
        try:
            st=os.stat(name,dir_fd=etc_fd,follow_symlinks=False)
        except FileNotFoundError:
            databases[name]=(None,[])
            continue
        if not stat.S_ISREG(st.st_mode):
            fail(f'El archivo etc/{name} del root prefix debe ser regular y no simbólico')
        if st.st_nlink != 1:
            fail(f'El archivo etc/{name} del root prefix no puede tener enlaces duros')
        databases[name]=(st,None)

    def read_database(name, expected):
        if expected is None:
            return []
        fd=os.open(name,os.O_RDONLY|no_follow|getattr(os,'O_CLOEXEC',0),dir_fd=etc_fd)
        try:
            opened=os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                    (opened.st_dev,opened.st_ino)!=(expected.st_dev,expected.st_ino)):
                fail(f'El archivo etc/{name} del root prefix cambió durante la validación')
            chunks=[]
            while True:
                data=os.read(fd,65536)
                if not data: break
                chunks.append(data)
            return b''.join(chunks).decode('utf-8').splitlines()
        finally:
            os.close(fd)

    passwd_stat,passwd_lines=databases['passwd']
    group_stat,group_lines=databases['group']
    if passwd_lines is None: passwd_lines=read_database('passwd',passwd_stat)
    if group_lines is None: group_lines=read_database('group',group_stat)

    user_rows=[line.split(':') for line in passwd_lines if line.split(':',1)[0]==user]
    passwd_changed=False
    if user_rows:
        if (len(user_rows)!=1 or len(user_rows[0])<7 or not user_rows[0][2].isdigit() or
                not user_rows[0][3].isdigit() or int(user_rows[0][2])!=uid or
                int(user_rows[0][3])!=gid or user_rows[0][6] not in ('/usr/sbin/nologin','/bin/false')):
            fail('La cuenta ficticia sep-admin del root prefix no coincide con el modelo del servicio')
    else:
        if any(len(row:=line.split(':'))>2 and row[2].isdigit() and int(row[2])==uid for line in passwd_lines):
            fail('UID de sep-admin duplicado en el root prefix')
        passwd_lines.append(f'{user}:x:{uid}:{gid}::/{home}:/usr/sbin/nologin')
        passwd_changed=True

    group_rows=[line.split(':') for line in group_lines if line.split(':',1)[0]==group]
    group_changed=False
    if group_rows:
        if (len(group_rows)!=1 or len(group_rows[0])<4 or not group_rows[0][2].isdigit() or
                int(group_rows[0][2])!=gid or group_rows[0][3]):
            fail('El grupo ficticio sep-admin del root prefix no coincide con el modelo dedicado')
    else:
        if any(len(row:=line.split(':'))>2 and row[2].isdigit() and int(row[2])==gid for line in group_lines):
            fail('GID de sep-admin duplicado en el root prefix')
        group_lines.append(f'{group}:x:{gid}:')
        group_changed=True

    def write_database(name, expected, lines):
        flags=os.O_WRONLY|no_follow|getattr(os,'O_CLOEXEC',0)
        if expected is None:
            flags |= os.O_CREAT|os.O_EXCL
        else:
            current=os.stat(name,dir_fd=etc_fd,follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or
                    (current.st_dev,current.st_ino)!=(expected.st_dev,expected.st_ino)):
                fail(f'El archivo etc/{name} del root prefix cambió antes de escribir')
        fd=os.open(name,flags,0o644,dir_fd=etc_fd)
        try:
            opened=os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                fail(f'El archivo etc/{name} del root prefix dejó de ser regular y exclusivo')
            if expected is not None and (opened.st_dev,opened.st_ino)!=(expected.st_dev,expected.st_ino):
                fail(f'El archivo etc/{name} del root prefix cambió durante la apertura')
            os.ftruncate(fd,0)
            data=('\n'.join(lines)+'\n').encode('utf-8')
            offset=0
            while offset<len(data): offset += os.write(fd,data[offset:])
        finally:
            os.close(fd)

    # No database is written until both paths have passed lstat-like checks,
    # safe opens, and account/group consistency validation.
    if passwd_changed: write_database('passwd',passwd_stat,passwd_lines)
    if group_changed: write_database('group',group_stat,group_lines)
finally:
    if etc_fd is not None: os.close(etc_fd)
    os.close(root_fd)
PY
  fi
fi
prepare_web_parent() {
  local boundary current_parent parent_uid parent_gid parent_mode
  local -a missing=()
  WEB_PARENT="$(root_path "$(dirname "$WEB_REL")")"
  boundary="${ROOT_PREFIX:-/}"
  boundary="$(cd -- "$boundary" && pwd -P)"
  current_parent="$WEB_PARENT"
  while [[ ! -d "$current_parent" ]]; do
    [[ "$current_parent" != "$boundary" && "$current_parent" != / ]] ||
      fail "No se pudo crear el directorio web accesible: $WEB_PARENT"
    missing+=("$current_parent")
    current_parent="$(dirname -- "$current_parent")"
  done
  for ((index=${#missing[@]}-1; index>=0; index--)); do
    if [[ -z "$ROOT_PREFIX" ]]; then
      install -d -o root -g root -m 0755 "${missing[index]}" ||
        fail "No se pudo crear el directorio web accesible: ${missing[index]}"
    else
      install -d -m 0755 "${missing[index]}" ||
        fail "No se pudo crear el directorio web de la prueba: ${missing[index]}"
    fi
  done
  current_parent="$WEB_PARENT"
  while :; do
    parent_uid="$(stat -c '%u' -- "$current_parent")"
    parent_gid="$(stat -c '%g' -- "$current_parent")"
    parent_mode="$(stat -c '%a' -- "$current_parent")"
    [[ "$parent_uid" == 0 ]] || fail "El directorio padre web no pertenece a root: $current_parent"
    (( (8#$parent_mode & 0022) == 0 )) || fail "El directorio padre web permite escritura no confiable: $current_parent"
    if (( (8#$parent_mode & 0001) == 0 && !(parent_gid == READ_GID && (8#$parent_mode & 0010) != 0) )); then
      fail "www-data y el servicio admin no pueden atravesar el directorio padre web: $current_parent"
    fi
    [[ "$current_parent" == "$boundary" ]] && break
    current_parent="$(dirname -- "$current_parent")"
  done
}
if [[ -z "$ROOT_PREFIX" || "${MIURA_TEST_WEB_PARENT_CHECK:-}" == 1 ]]; then
  prepare_web_parent
fi
WEB="$(root_path "$WEB_REL")"
BIN="$(root_path "$BIN_REL")"
CONF="$(root_path "$CONF_REL")"
STATE="$(root_path "$STATE_REL")"
BACKUPS="$(root_path "$BACKUP_REL")"

if ((ROLLBACK_REQUESTED)); then
  assert_safe_root_prefix_systemd_wants
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
  python3 - "$ARCHIVE" "${ROOT_PREFIX:-/}" "${READ_GID:--1}" <<'PY'
import os, pathlib, shutil, sys, tarfile, tempfile
import grp
archive = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2]).resolve()
read_gid = int(sys.argv[3])
web_rel = pathlib.PurePosixPath('var/www/server-dashboard')
managed = {
    'etc/systemd/system/server-dashboard-web.service',
    'etc/systemd/system/server-dashboard-collector.service',
    'etc/systemd/system/server-dashboard-collector.timer',
    'etc/systemd/system/server-dashboard-admin.service',
    'usr/local/sbin/server-dashboard-collect.py',
    'usr/local/sbin/server-dashboard-live.py',
    'usr/local/sbin/server-dashboard-admin.py',
    'usr/local/sbin/miura-panel-admin-setup',
    'usr/local/sbin/spanish-empire-panel-admin-setup',
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
        if os.geteuid() == 0 and read_gid >= 0:
            for path in [web, *web.rglob('*')]:
                os.chown(path, 0, read_gid)
                if path.is_dir(): path.chmod(0o750)
                elif path.is_file(): path.chmod(0o750 if path.stat().st_mode & 0o111 else 0o640)
    for rel in managed:
        # The Spanish command name is included only to restore the validated
        # inherited symlink case. In ordinary installations it may be an
        # independent, unmanaged command, so preserve it when absent from the
        # backup archive.
        if rel == 'usr/local/sbin/spanish-empire-panel-admin-setup' and rel not in seen:
            continue
        dest = root.joinpath(*pathlib.PurePosixPath(rel).parts)
        if dest.is_symlink() or dest.is_file(): dest.unlink()
        elif dest.is_dir(): shutil.rmtree(dest)
        src = stage.joinpath(*pathlib.PurePosixPath(rel).parts)
        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            mode = src.stat().st_mode & 0o755
            if rel == 'usr/local/sbin/server-dashboard-live.py':
                mode = 0o750
            dest.chmod(mode)
            if os.geteuid() == 0 and read_gid >= 0 and rel in {
                'usr/local/sbin/server-dashboard-live.py',
                'usr/local/sbin/server-dashboard-admin.py',
                'usr/local/sbin/sep-admin-fleet.py',
            }:
                os.chown(dest, 0, read_gid)
                dest.chmod(0o750)
    admin_units = [
        root / 'etc/systemd/system/server-dashboard-admin.service',
        root / 'usr/lib/systemd/system/server-dashboard-admin.service',
        root / 'lib/systemd/system/server-dashboard-admin.service',
    ]
    if not any(path.is_file() and not path.is_symlink() for path in admin_units):
        (root / 'etc/spanish-empire-panel/admin-enabled').unlink(missing_ok=True)
        (root / 'var/www/server-dashboard/admin-status.json').unlink(missing_ok=True)
        enabled = root / 'etc/systemd/system/multi-user.target.wants/server-dashboard-admin.service'
        if enabled.is_symlink() and os.readlink(enabled) in (
                '/etc/systemd/system/server-dashboard-admin.service',
                '../server-dashboard-admin.service'):
            enabled.unlink()
    if os.geteuid() == 0 and read_gid >= 0:
        conf = root / 'etc/server-dashboard.conf'
        if conf.is_file() and not conf.is_symlink():
            os.chown(conf, 0, read_gid)
            conf.chmod(0o640)
    # Keep service sandbox permissions after rolling back to an older backup.
    # The web process still needs its preferences write exception and group;
    # the collector is confined to the two trees it owns as generated output.
    for rel, required in {
        'etc/systemd/system/server-dashboard-web.service': {
            'SupplementaryGroups': 'server-dashboard-prefs server-dashboard-read',
            'ProtectSystem': 'strict',
            'ProtectHome': 'true',
            'ReadWritePaths': '/var/lib/server-dashboard/preferences',
        },
        'etc/systemd/system/server-dashboard-collector.service': {
            'NoNewPrivileges': 'true',
            'PrivateTmp': 'true',
            'ProtectSystem': 'strict',
            'ProtectHome': 'true',
            'ReadWritePaths': '/var/lib/server-dashboard /var/www/server-dashboard',
        },
    }.items():
        unit = root.joinpath(*pathlib.PurePosixPath(rel).parts)
        if unit.is_symlink() or not unit.is_file():
            continue
        lines = unit.read_text(encoding='utf-8').splitlines()
        lines = [line for line in lines if line.split('=', 1)[0] not in required]
        try:
            service_index = lines.index('[Service]')
        except ValueError:
            continue
        lines[service_index + 1:service_index + 1] = [f'{key}={value}' for key, value in required.items()]
        unit.write_text('\n'.join(lines) + '\n', encoding='utf-8')
PY
  if [[ -z "$ROOT_PREFIX" ]] && command -v systemctl >/dev/null; then
    systemctl daemon-reload
    systemctl restart server-dashboard-web.service
    systemctl enable --now server-dashboard-collector.timer
    if { [[ -f "$(root_path "$ADMIN_UNIT_REL")" ]] ||
         [[ -f "$(root_path usr/lib/systemd/system/server-dashboard-admin.service)" ]] ||
         [[ -f "$(root_path lib/systemd/system/server-dashboard-admin.service)" ]]; } &&
       [[ -s /etc/spanish-empire-panel/admin.json &&
          -s /etc/spanish-empire-panel/admin.crt && -s /etc/spanish-empire-panel/admin.key ]]; then
      systemctl enable --now server-dashboard-admin.service
      systemctl restart server-dashboard-admin.service
    else
      systemctl disable --now server-dashboard-admin.service >/dev/null 2>&1 || true
    fi
  elif [[ -n "$ROOT_PREFIX" ]] && command -v systemctl >/dev/null; then
    assert_safe_root_prefix_systemd_wants
    systemctl --root="$ROOT_PREFIX" enable server-dashboard-web.service server-dashboard-collector.timer >/dev/null
    if [[ -f "$(root_path etc/spanish-empire-panel/admin-enabled)" &&
          -s "$(root_path etc/spanish-empire-panel/admin.json)" &&
          -s "$(root_path etc/spanish-empire-panel/admin.crt)" &&
          -s "$(root_path etc/spanish-empire-panel/admin.key)" &&
          -f "$(root_path "$ADMIN_UNIT_REL")" ]]; then
      systemctl --root="$ROOT_PREFIX" enable server-dashboard-admin.service >/dev/null
    else
      systemctl --root="$ROOT_PREFIX" disable server-dashboard-admin.service >/dev/null 2>&1 || true
    fi
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
ADMIN_STATE="$(root_path "$ADMIN_STATE_REL")"
if [[ -z "$ROOT_PREFIX" ]]; then
  if [[ -e "$ADMIN_STATE" && -L "$ADMIN_STATE" ]]; then fail 'El estado administrativo no puede ser un enlace simbólico'; fi
  mkdir -p "$ADMIN_STATE"
  if find "$ADMIN_STATE" -type l -print -quit | grep -q .; then
    fail 'El estado administrativo contiene enlaces simbólicos; no se cambiarán sus permisos'
  fi
  chown -R "$ADMIN_USER:$ADMIN_GROUP" "$ADMIN_STATE"
  find "$ADMIN_STATE" -type d -exec chmod 0700 {} +
  chown "$ADMIN_USER:$ADMIN_GROUP" "$ADMIN_STATE"
  chmod 0700 "$ADMIN_STATE"
else
  mkdir -p "$ADMIN_STATE"
  if (( EUID == 0 )) && getent passwd "$ADMIN_USER" >/dev/null && getent group "$ADMIN_GROUP" >/dev/null; then
    chown -R "$ADMIN_USER:$ADMIN_GROUP" "$ADMIN_STATE"
  fi
  chmod 0700 "$ADMIN_STATE"
fi
find "$ADMIN_STATE" -type f -exec chmod 0600 {} +
PREFS_DIR="$(root_path "$PREFS_REL")"
PREFS_FILE="$(root_path "$PREFS_FILE_REL")"
mkdir -p "$PREFS_DIR"
# The web process can traverse the state root but gets no listing or write
# rights there. Its only persistent writable state is the visual preferences
# subdirectory, owned by a dedicated, non-shared preferences group.
if [[ -z "$ROOT_PREFIX" ]]; then
  chown root:server-dashboard-prefs "$STATE" "$PREFS_DIR"
  [[ ! -f "$PREFS_FILE" ]] || chown root:server-dashboard-prefs "$PREFS_FILE"
elif (( EUID == 0 )) && getent group server-dashboard-prefs >/dev/null; then
  chown root:server-dashboard-prefs "$STATE" "$PREFS_DIR"
  [[ ! -f "$PREFS_FILE" ]] || chown root:server-dashboard-prefs "$PREFS_FILE"
fi
chmod 0710 "$STATE"
chmod 2770 "$PREFS_DIR"
if [[ -f "$PREFS_FILE" ]]; then chmod 0660 "$PREFS_FILE"; fi
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
# The one documented legacy alias is accepted only after the strict check
# above. Snapshot its trusted referent as a regular file under the managed
# alias name so rollback never has to extract a symlink from a backup archive.
LEGACY_ADMIN_ALIAS_PATH="$(root_path "$LEGACY_ADMIN_ALIAS_REL")"
LEGACY_ADMIN_ALIAS_SNAPSHOT=''
if [[ -L "$LEGACY_ADMIN_ALIAS_PATH" ]]; then
  assert_safe_legacy_admin_alias "$LEGACY_ADMIN_ALIAS_PATH"
  LEGACY_ADMIN_ALIAS_SNAPSHOT="$BACKUP/.miura-panel-admin-setup.snapshot"
  cp -p -- "$(root_path "$LEGACY_ADMIN_TARGET_REL")" "$LEGACY_ADMIN_ALIAS_SNAPSHOT" || {
    rm -rf "$BACKUP"
    fail 'No se pudo preparar la copia segura del alias heredado'
  }
fi
# Back up app files, deployed helpers, and service definitions. The config
# (including selected mode) is persistent and is never rewritten on upgrades.
BACKUP_PATHS=(
  "${WEB_REL#/}"
  etc/systemd/system/server-dashboard-web.service
  etc/systemd/system/server-dashboard-collector.service
  etc/systemd/system/server-dashboard-collector.timer
  etc/systemd/system/server-dashboard-admin.service
  usr/local/sbin/server-dashboard-collect.py
  usr/local/sbin/server-dashboard-live.py
  usr/local/sbin/server-dashboard-admin.py
  usr/local/sbin/sep-admin-fleet.py
  usr/local/sbin/miura-panel-fleet
  usr/local/sbin/miura-panel-fleet-docker
  usr/local/sbin/spanish-empire-panel-fleet-receiver
  usr/local/sbin/server-dashboard-docker-action.py
  usr/local/sbin/server-dashboard-docker-updates.py
  usr/local/sbin/spanish-empire-panel-update
)
if [[ -z "$LEGACY_ADMIN_ALIAS_SNAPSHOT" ]]; then
  BACKUP_PATHS+=(usr/local/sbin/miura-panel-admin-setup)
else
  # Preserve both legacy entry points' pre-upgrade contents. The symlink
  # itself is snapshotted as a regular alias member below.
  BACKUP_PATHS+=(usr/local/sbin/spanish-empire-panel-admin-setup)
fi
TAR_ARGS=(-czf "$BACKUP/panel-backup.tar.gz" -C "${ROOT_PREFIX:-/}" --ignore-failed-read "${BACKUP_PATHS[@]}")
if [[ -n "$LEGACY_ADMIN_ALIAS_SNAPSHOT" ]]; then
  TAR_ARGS+=(-C "$BACKUP" '--transform=s|^\.miura-panel-admin-setup\.snapshot$|usr/local/sbin/miura-panel-admin-setup|' .miura-panel-admin-setup.snapshot)
fi
tar "${TAR_ARGS[@]}" 2>/dev/null || {
    rm -rf "$BACKUP"
    fail 'No se pudo crear la copia previa; instalación cancelada'
  }
if [[ -n "$LEGACY_ADMIN_ALIAS_SNAPSHOT" ]]; then
  rm -f -- "$LEGACY_ADMIN_ALIAS_SNAPSHOT"
fi
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
  if [[ "$src" == admin-setup.py && -n "$LEGACY_ADMIN_ALIAS_SNAPSHOT" ]]; then
    # Keep the documented legacy link and update its referent; both the
    # Miura and Spanish command names remain aliases to the same new program.
    install -m 0750 "$SCRIPT_DIR/$src" "$(root_path "$LEGACY_ADMIN_TARGET_REL")"
  elif [[ "$src" == live.py || "$src" == admin-web.py || "$src" == admin-fleet.py ]] &&
       (( EUID == 0 )) && [[ -n "$READ_GID" ]]; then
    install -o root -g "$READ_GID" -m 0750 "$SCRIPT_DIR/$src" "$BIN/${ALIASES[$src]}"
  else
    install -m 0750 "$SCRIPT_DIR/$src" "$BIN/${ALIASES[$src]}"
  fi
done

if [[ ! -f "$CONF" ]]; then
  CONF_CREATED=1
  mkdir -p "$(dirname "$CONF")"
  cat > "$CONF" <<EOF
# Miura Panel configuration. Updates preserve this file.
DASHBOARD_MODE=$MODE
DASHBOARD_BIND_IP=127.0.0.1
DASHBOARD_PORT=8766
DASHBOARD_SMART=1
DASHBOARD_DOCKER_UPDATER=0
EOF
  chmod 0640 "$CONF"
fi
if ((UPGRADE)); then
  CONF_MODIFIED=1
  python3 - "$CONF" <<'PY'
import os, pathlib, stat, sys, tempfile
path=pathlib.Path(sys.argv[1])
original=path.read_bytes()
lines=original.splitlines(keepends=True)
mode_keys=[]; bind_keys=[]
for index,line in enumerate(lines):
    key, sep, value=line.strip().partition(b'=')
    if sep and key.strip()==b'DASHBOARD_MODE': mode_keys.append(index)
    if sep and key.strip()==b'DASHBOARD_BIND_IP': bind_keys.append(index)
if len(mode_keys)>1 or len(bind_keys)>1:
    raise SystemExit('La configuración tiene claves de modo o dirección duplicadas; cancelo')
changed=False
if not mode_keys:
    lines.append((b'' if not original or original.endswith(b'\n') else b'\n')+b'DASHBOARD_MODE=normal\n')
    changed=True
if bind_keys:
    index=bind_keys[0]
    key, sep, value=lines[index].strip().partition(b'=')
    if value.strip().strip(b'"').strip(b"'")==b'0.0.0.0':
        ending=b'\n' if lines[index].endswith(b'\n') else b''
        lines[index]=key+b'=127.0.0.1'+ending
        changed=True
updated=b''.join(lines)
if not changed: raise SystemExit(0)
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
if (( EUID == 0 )) && [[ -n "$READ_GID" ]]; then chown "root:$READ_GID" "$CONF"; fi
chmod 0640 "$CONF"
if (( EUID == 0 )) && [[ -n "$READ_GID" ]]; then
  chown -R "root:$READ_GID" "$WEB"
  python3 - "$WEB" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for path in [root, *root.rglob('*')]:
    if path.is_symlink():
        raise SystemExit('El árbol web contiene un enlace simbólico: '+str(path))
    if path.is_dir(): path.chmod(0o750)
    elif path.is_file(): path.chmod(0o640)
    else: raise SystemExit('El árbol web contiene un archivo especial: '+str(path))
PY
else
  chmod 0750 "$WEB"
fi

mkdir -p "$(root_path etc/systemd/system)"
cat > "$(root_path etc/systemd/system/server-dashboard-web.service)" <<'EOF'
[Unit]
Description=Miura Panel read-only web interface
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
SupplementaryGroups=server-dashboard-prefs server-dashboard-read
ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-live.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
UnsetEnvironment=MIURA_ROOT_PREFIX
ReadWritePaths=/var/lib/server-dashboard/preferences

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
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/server-dashboard /var/www/server-dashboard
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
if (( ! ADMIN_UNIT_PRESENT )); then
cat > "$(root_path "$ADMIN_UNIT_REL")" <<'EOF'
[Unit]
Description=Miura Panel authenticated HTTPS administration
After=network.target

[Service]
Type=simple
User=sep-admin
Group=sep-admin
SupplementaryGroups=server-dashboard-read server-dashboard-prefs
WorkingDirectory=/var/www/server-dashboard
ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.json
ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.crt
ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.key
ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-admin.py
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
UnsetEnvironment=MIURA_ROOT_PREFIX
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
StateDirectory=spanish-empire-panel-admin
StateDirectoryMode=0700
ReadWritePaths=/var/lib/server-dashboard/preferences

[Install]
WantedBy=multi-user.target
EOF
fi

if ((ADMIN_UNIT_PRESENT && ! ADMIN_UNIT_MANAGED)); then
  assert_safe_path "$ADMIN_DROPIN_REL"
  ADMIN_DROPIN_DIR="$(root_path "$ADMIN_DROPIN_REL")"
  ADMIN_DROPIN_FILE="$(root_path "$ADMIN_DROPIN_FILE_REL")"
  assert_safe_leaf "$ADMIN_DROPIN_FILE"
  assert_safe_root_owned_chain "$ADMIN_DROPIN_DIR"
  if [[ -e "$ADMIN_DROPIN_FILE" ]]; then
    expected_dropin_uid=0
    [[ -z "$ROOT_PREFIX" ]] || expected_dropin_uid="$EUID"
    [[ "$(stat -c '%u' -- "$ADMIN_DROPIN_FILE")" == "$expected_dropin_uid" ]] ||
      fail 'El drop-in existente no pertenece al propietario esperado'
    existing_mode="$(stat -c '%a' -- "$ADMIN_DROPIN_FILE")"
    (( (8#$existing_mode & 0022) == 0 )) || fail 'El drop-in existente permite escritura a grupo u otros'
  fi
  mkdir -p "$ADMIN_DROPIN_DIR"
  assert_safe_root_owned_chain "$ADMIN_DROPIN_DIR"
  ADMIN_DROPIN_TMP="$(mktemp "$ADMIN_DROPIN_DIR/.miura-preferences.XXXXXX")" || fail 'No se pudo preparar el drop-in seguro'
  if ! printf '[Service]\nSupplementaryGroups=server-dashboard-prefs\nSupplementaryGroups=%s\nReadWritePaths=/var/lib/server-dashboard/preferences\n' "$READ_GROUP" > "$ADMIN_DROPIN_TMP"; then
    rm -f -- "$ADMIN_DROPIN_TMP"
    fail 'No se pudo escribir el drop-in seguro'
  fi
  if [[ -z "$ROOT_PREFIX" ]]; then chown root:root "$ADMIN_DROPIN_DIR" "$ADMIN_DROPIN_TMP"; fi
  chmod 0755 "$ADMIN_DROPIN_DIR"
  chmod 0644 "$ADMIN_DROPIN_TMP"
  mv -fT "$ADMIN_DROPIN_TMP" "$ADMIN_DROPIN_FILE"
fi

if [[ -z "$ROOT_PREFIX" ]] && command -v systemctl >/dev/null; then
  systemctl daemon-reload
  systemctl enable --now server-dashboard-web.service server-dashboard-collector.timer
  systemctl restart server-dashboard-web.service
  if [[ -f /etc/spanish-empire-panel/admin-enabled && -s /etc/spanish-empire-panel/admin.json &&
        -s /etc/spanish-empire-panel/admin.crt && -s /etc/spanish-empire-panel/admin.key ]]; then
    systemctl enable --now server-dashboard-admin.service
    systemctl restart server-dashboard-admin.service
  else
    systemctl disable --now server-dashboard-admin.service >/dev/null 2>&1 || true
  fi
elif [[ -n "$ROOT_PREFIX" ]] && command -v systemctl >/dev/null; then
  # --root edits only the isolated filesystem's boot wants links; it never
  # contacts or changes the host system manager.
  assert_safe_root_prefix_systemd_wants
  systemctl --root="$ROOT_PREFIX" enable server-dashboard-web.service server-dashboard-collector.timer >/dev/null
  if [[ -f "$(root_path etc/spanish-empire-panel/admin-enabled)" &&
        -s "$(root_path etc/spanish-empire-panel/admin.json)" &&
        -s "$(root_path etc/spanish-empire-panel/admin.crt)" &&
        -s "$(root_path etc/spanish-empire-panel/admin.key)" ]]; then
    systemctl --root="$ROOT_PREFIX" enable server-dashboard-admin.service >/dev/null
  else
    systemctl --root="$ROOT_PREFIX" disable server-dashboard-admin.service >/dev/null 2>&1 || true
  fi
fi
rm -rf "$OLD_HOLDER"
OLD_WEB=''
OLD_HOLDER=''
printf 'Miura Panel v%s instalado en modo %s. Copia previa: %s\n' "$VERSION" "$MODE" "$BACKUP"
if [[ "$MODE" == demo ]]; then
  printf 'La administración HTTPS permanece desactivada en modo demo. Cambia explícitamente a modo normal antes de configurarla.\n'
elif [[ ! -f "$(root_path etc/spanish-empire-panel/admin-enabled)" ]]; then
  printf 'Para activar administración HTTPS por primera vez, ejecuta: sudo miura-panel-admin-setup\n'
  printf 'El asistente configurará credenciales y certificado antes de habilitar el servicio HTTPS.\n'
fi
exit 0
