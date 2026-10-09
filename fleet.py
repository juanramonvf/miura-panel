#!/usr/bin/env python3
"""Miura Panel · administrador SSH opcional, exclusivo para consola.

Sin API web, sin contraseñas almacenadas y sin cambios de cortafuegos.
Los destinos deben autorizar explícitamente la clave pública del gestor.
"""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UPDATER = '/usr/local/sbin/spanish-empire-panel-update'
RECEIVER = '/usr/local/sbin/spanish-empire-panel-fleet-receiver'
SERVICE_ACCOUNT = 'sep-fleet'
ROLE_FILE = Path('/var/www/server-dashboard/fleet-role.json')
SUDOERS = Path('/etc/sudoers.d/spanish-empire-panel-fleet')
DOCKER_SUDO = Path('/etc/sudoers.d/miura-panel-fleet-docker')
DOCKER_MARK = Path('/etc/spanish-empire-panel/fleet-docker-enabled')
DOCKER_HELPER = '/usr/local/sbin/miura-panel-fleet-docker'
DOCKER_SUDO_CONTENT = ('# Miura Panel: solo broker Docker restringido, requiere opt-in local.\n'
                       'Defaults:sep-fleet !requiretty\n'
                       'sep-fleet ALL=(root) NOPASSWD: '+DOCKER_HELPER+' ""\n')
MARKER = Path('/etc/spanish-empire-panel/fleet-owned-user')
SSH_KEY_TYPE = 'ssh-ed25519'
SSH_ID = Path.home() / '.ssh' / 'sep_fleet_ed25519'
FLEET = Path.home() / '.config' / 'spanish-empire-panel' / 'fleet.json'


def die(message):
    print('ERROR:', message, file=sys.stderr)
    raise SystemExit(1)


def root_only():
    if os.geteuid() != 0:
        die('Esta operación requiere sudo; no ejecuta ninguna actualización por sí sola.')


def ensure_user_mode():
    if os.geteuid() == 0:
        die('Ejecuta esta operación con tu usuario normal, sin sudo.')


def get_key(pub):
    """Acepta una línea OpenSSH ed25519, nunca opciones ni otros tipos."""
    if len(pub) > 2048 or any(ord(c) < 32 for c in pub):
        die('Clave pública inválida.')
    fields = pub.strip().split()
    if len(fields) not in (2, 3) or fields[0] != SSH_KEY_TYPE:
        die('Solo se admiten claves públicas ssh-ed25519.')
    if not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', fields[1]):
        die('Clave pública con codificación inválida.')
    try:
        decoded = base64.b64decode(fields[1], validate=True)
    except Exception:
        die('Clave pública no válida.')
    if not (32 <= len(decoded) <= 256):
        die('Longitud inválida de clave pública.')
    if fields.__len__() == 3 and not re.fullmatch(r'[A-Za-z0-9._@+-]{1,80}', fields[2]):
        die('Comentario inválido de clave pública.')
    return fields[0] + ' ' + fields[1]


def load_state():
    try:
        data = json.loads(FLEET.read_text())
        assert isinstance(data.get('targets'), list)
        return data
    except FileNotFoundError:
        return {'targets': []}
    except Exception:
        die('Configuración del gestor inválida; no se modifica.')


def store_state(state):
    FLEET.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(FLEET.parent, 0o700)
    fd, temp = tempfile.mkstemp(prefix='.fleet-', dir=FLEET.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f, indent=2)
            f.write('\n')
        os.chmod(temp, 0o600)
        os.replace(temp, FLEET)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def validate_host(host):
    if not host or host.startswith('-') or len(host) > 253 or any(c in host for c in ('@', '/', ':', chr(92))):
        die('Host inválido; introduce una IP o nombre DNS, sin puerto ni usuario.')
    try:
        ipaddress.IPv4Address(host)
        return host
    except ValueError:
        pass
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', host):
        die('Nombre de servidor inválido.')
    return host


def check_name(name):
    if not re.fullmatch(r'[A-Za-z0-9À-ÿ_. -]{1,64}', name):
        die('Nombre inválido (máximo 64 caracteres).')
    return name


def require_initialized():
    ensure_user_mode()
    if not SSH_ID.is_file() or not SSH_ID.with_suffix('.pub').is_file():
        die('Primero ejecuta: miura-panel-fleet init')


def init():
    ensure_user_mode()
    if not shutil.which('ssh-keygen') or not shutil.which('ssh'):
        die('Falta cliente OpenSSH (openssh-client).')
    SSH_ID.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(SSH_ID.parent, 0o700)
    if not SSH_ID.exists():
        print('Creando clave exclusiva para SEP. Recomendable protegerla con frase de paso y usar ssh-agent.')
        subprocess.run(['ssh-keygen', '-t', 'ed25519', '-f', str(SSH_ID), '-C', 'sep-fleet'], check=True)
    elif not SSH_ID.with_suffix('.pub').exists():
        die('Existe clave privada sin clave pública; no se reemplaza.')
    store_state(load_state())
    pub = get_key(SSH_ID.with_suffix('.pub').read_text().strip())
    print('\nEn cada destino ya actualizado a SEP, autoriza esta clave (una sola vez):')
    print("sudo miura-panel-fleet allow '" + pub + "'")
    print('\nLuego registra el destino en este equipo:')
    print('miura-panel-fleet add "Mi servidor" 192.0.2.10 --port 22')
    print('Comprueba previamente la huella SSH del destino y añádela a ~/.ssh/known_hosts.')
    if not ROLE_FILE.is_file() or json.loads(ROLE_FILE.read_text()).get('role') != 'principal':
        print('Para identificar este panel como principal: sudo miura-panel-fleet primary')


def allow(pub):
    root_only()
    key = get_key(pub)
    if not shutil.which('visudo'):
        die('No existe visudo. Instala/configura sudo antes de autorizar conexiones.')
    if not shutil.which('sshd') and not Path('/usr/sbin/sshd').exists():
        die('No hay OpenSSH Server. SEP no lo instala ni abre puertos automáticamente.')
    try:
        account = pwd.getpwnam(SERVICE_ACCOUNT)
        if not MARKER.is_file() or MARKER.read_text().strip() != str(account.pw_uid):
            die('Ya existe un usuario sep-fleet no gestionado por SEP. No se tocará.')
    except KeyError:
        if MARKER.exists():
            die('Existe registro previo de sep-fleet, pero no el usuario. Revisión manual necesaria.')
        subprocess.run(['useradd', '--system', '--create-home', '--home-dir', '/var/lib/sep-fleet',
                        '--shell', '/bin/sh', '--user-group', SERVICE_ACCOUNT], check=True)
        account = pwd.getpwnam(SERVICE_ACCOUNT)
        MARKER.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        MARKER.write_text(str(account.pw_uid) + '\n')
        os.chmod(MARKER, 0o600)
    home = Path(account.pw_dir)
    if home != Path('/var/lib/sep-fleet') or home.is_symlink():
        die('Directorio SEP SSH inesperado; no se modifica.')
    ssh_dir = home / '.ssh'
    if ssh_dir.is_symlink():
        die('Directorio .ssh simbólico; no se modifica.')
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(ssh_dir, account.pw_uid, account.pw_gid)
    os.chmod(ssh_dir, 0o700)
    authorized = ssh_dir / 'authorized_keys'
    if authorized.exists() and authorized.is_symlink():
        die('authorized_keys es enlace simbólico; no se modifica.')
    line = f'restrict,command="{RECEIVER}" {key} sep-fleet\n'
    old = authorized.read_text() if authorized.exists() else ''
    if not any(row.split(' ssh-ed25519 ', 1)[-1].split(' ', 1)[0] == key.split(' ')[1]
               for row in old.splitlines() if ' ssh-ed25519 ' in row):
        with authorized.open('a') as f:
            f.write(line)
    os.chown(authorized, account.pw_uid, account.pw_gid)
    os.chmod(authorized, 0o600)
    sudoers_content = (
        '# SEP: cuenta SSH restringida; sin shell remota libre ni privilegios generales.\n'
        'Defaults:sep-fleet !requiretty\n'
        'sep-fleet ALL=(root) NOPASSWD: '
        f'{UPDATER} --fleet-apply, {UPDATER} --check\n'
    )
    with tempfile.NamedTemporaryFile(mode='w', prefix='sep-sudo-', delete=False) as f:
        f.write(sudoers_content)
        path = f.name
    try:
        os.chmod(path, 0o440)
        subprocess.run(['visudo', '-cf', path], check=True, stdout=subprocess.DEVNULL)
        shutil.copyfile(path, SUDOERS)
        os.chown(SUDOERS, 0, 0)
        os.chmod(SUDOERS, 0o440)
    finally:
        Path(path).unlink(missing_ok=True)
    print('Clave autorizada exclusivamente para comprobar/actualizar SEP mediante SSH.')
    print('No se ha abierto ningún puerto ni modificado el resto de SSH.')


def docker_permission(enable):
    """Autorización independiente por destino; nunca se activa al instalar."""
    root_only()
    import socket
    try:
        account=pwd.getpwnam(SERVICE_ACCOUNT)
        assert MARKER.read_text().strip()==str(account.pw_uid)
        authorized=Path(account.pw_dir)/'.ssh'/'authorized_keys'
        assert authorized.is_file() and 'restrict,command="'+RECEIVER+'"' in authorized.read_text()
    except (AssertionError,FileNotFoundError,KeyError):
        die('Vincula primero el servidor mediante miura-panel-fleet allow')
    if not enable:
        if DOCKER_SUDO.exists() and DOCKER_SUDO.read_text()!=DOCKER_SUDO_CONTENT:
            die('Permisos sudo modificados manualmente: revisión necesaria')
        DOCKER_SUDO.unlink(missing_ok=True)
        DOCKER_MARK.unlink(missing_ok=True)
        print('Administración Docker SSH desactivada. Las otras autorizaciones SSH se conservan.')
        return
    if DOCKER_SUDO.is_symlink() or DOCKER_MARK.is_symlink():
        die('No se modifican enlaces simbólicos de permisos')
    if DOCKER_SUDO.exists() and DOCKER_SUDO.read_text()!=DOCKER_SUDO_CONTENT:
        die('Existe un sudoers no gestionado; revisión manual necesaria')
    if not Path(DOCKER_HELPER).is_file() or not Path('/run/server-dashboard/docker-action.sock').is_socket():
        die('El broker Docker del panel no está activo en este servidor')
    conf=Path('/etc/server-dashboard.conf').read_text(encoding='utf-8')
    if not any(line.strip().startswith('DASHBOARD_DOCKER_UPDATER=') and line.split('=',1)[1].strip().strip(chr(34)+chr(39))=='1' for line in conf.splitlines()):
        die('El actualizador Docker local está desactivado; no se conceden permisos.')
    with tempfile.NamedTemporaryFile(mode='w',prefix='.miura-docker-',dir=DOCKER_SUDO.parent,delete=False) as f:
        f.write(DOCKER_SUDO_CONTENT);tmp=f.name
    try:
        os.chmod(tmp,0o440)
        subprocess.run(['visudo','-cf',tmp],check=True,stdout=subprocess.DEVNULL)
        os.chown(tmp,0,0)
        os.replace(tmp,DOCKER_SUDO)
    finally:
        Path(tmp).unlink(missing_ok=True)
    DOCKER_MARK.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    DOCKER_MARK.write_text('enabled\n')
    os.chown(DOCKER_MARK,0,0)
    os.chmod(DOCKER_MARK,0o600)
    print('Docker SSH autorizado exclusivamente mediante broker restringido. No se cambia Docker, SSH ni el firewall.')


def revoke(pub):
    root_only()
    key = get_key(pub)
    account = pwd.getpwnam(SERVICE_ACCOUNT)
    auth = Path(account.pw_dir) / '.ssh' / 'authorized_keys'
    if not auth.exists():
        die('No hay claves autorizadas para SEP.')
    lines = auth.read_text().splitlines()
    value = key.split()[1]
    kept = [row for row in lines if not (row.startswith('restrict,command="' + RECEIVER + '"')
                                      and (' ssh-ed25519 ' + value) in row)]
    if len(kept) == len(lines):
        die('No se encontró la clave SEP especificada.')
    auth.write_text('\n'.join(kept) + ('\n' if kept else ''))
    os.chown(auth, account.pw_uid, account.pw_gid)
    os.chmod(auth, 0o600)
    print('Clave revocada.')


def primary():
    root_only()
    ROLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROLE_FILE.write_text('{"role":"principal"}\n')
    ROLE_FILE.chmod(0o644)
    print('Panel designado como principal. No se habilita ninguna API administrativa web.')


def secondary():
    root_only()
    ROLE_FILE.unlink(missing_ok=True)
    print('Eliminada la identificación de servidor principal.')


def ssh_command(target, action, timeout=30):
    host = validate_host(target['host'])
    port = int(target['port'])
    if not 1 <= port <= 65535:
        die('Puerto SSH no válido.')
    command = ['ssh', '-T', '-p', str(port), '-i', str(SSH_ID), '-o', 'IdentitiesOnly=yes',
               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=8', '-o', 'NumberOfPasswordPrompts=0',
               f'{SERVICE_ACCOUNT}@{host}', action]
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout)


def add(name, host, port):
    require_initialized()
    check_name(name)
    validate_host(host)
    data = load_state()
    if len(data['targets']) >= 100:
        die('Por seguridad, máximo 100 equipos por gestor.')
    if any(t['name'].lower() == name.lower() for t in data['targets']):
        die('Ya existe un equipo con ese nombre.')
    if any(t['host'] == host and int(t['port']) == port for t in data['targets']):
        die('Ese destino ya está registrado.')
    target = {'name': name, 'host': host, 'port': port}
    print(f'Validando conexión SSH con {name} ({host}:{port})...')
    try:
        result = ssh_command(target, 'check')
    except subprocess.TimeoutExpired:
        die('La conexión SSH ha agotado el tiempo de espera.')
    if result.returncode != 0:
        die('SSH no autorizado o clave de servidor sin verificar.\n' + (result.stderr or result.stdout)[-700:])
    try:
        check = json.loads(result.stdout.strip())
        assert 'current' in check and 'status' in check
    except Exception:
        die('El destino SSH no respondió con la comprobación SEP esperada.')
    data['targets'].append(target)
    store_state(data)
    print(f'Registrado: {name}, SEP v{check["current"]}')


def remove(name):
    ensure_user_mode()
    data = load_state()
    new = [t for t in data['targets'] if t['name'] != name]
    if len(new) == len(data['targets']):
        die('Equipo no encontrado.')
    data['targets'] = new
    store_state(data)
    print('Destino eliminado de la lista local. La autorización SSH remota sigue vigente; revócala en el destino si procede.')


def list_targets():
    ensure_user_mode()
    for t in load_state()['targets']:
        print(f"{t['name']:24} {t['host']}:{t['port']} (SSH)")


def remote_check(target):
    try:
        result = ssh_command(target, 'check', timeout=35)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, str(e)
    if result.returncode:
        return None, (result.stderr or result.stdout or 'Fallo SSH').strip()[-300:]
    try:
        data = json.loads(result.stdout.strip())
        if not isinstance(data.get('current'), str) or data.get('status') not in ('ok', 'not_configured'):
            raise ValueError('comprobación incompleta')
        return data, None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None, 'Respuesta del actualizador inválida'


def local_check():
    installed = json.loads(Path('/var/www/server-dashboard/version.json').read_text())['version']
    import urllib.request
    with urllib.request.urlopen('https://sep.mundopeke.es/manifest.json', timeout=12) as f:
        manifest = json.load(f)
    latest = manifest['version']
    version = lambda s: tuple(map(int, s.split('.')))
    return {'current': installed, 'latest': latest, 'update_available': version(latest) > version(installed), 'status': 'ok'}


def execute(update=False):
    require_initialized()
    targets = load_state()['targets']
    print('\nComprobando destinos SSH (sin actualizar)...')
    statuses = []
    for t in targets:
        status, error = remote_check(t)
        statuses.append((t, status, error))
        if error:
            print(f"{t['name']}: ERROR: {error}")
        else:
            print(f"{t['name']}: v{status['current']} → v{status.get('latest', 'N/D')}" + (' (pendiente)' if status.get('update_available') else ''))
    try:
        local = local_check()
        print(f"Principal (local): v{local['current']} → v{local['latest']}")
    except Exception as e:
        local = None
        print(f'Principal (local): ERROR: {e}')
    if not update:
        return 1 if any(err for _, _, err in statuses) else 0
    bad = [(t, err) for t, status, err in statuses if err or status['status'] != 'ok']
    if bad or local is None:
        die('Hay destinos inaccesibles o sin manifiesto; se cancela la actualización completa.')
    pending = [t for t, status, _ in statuses if status.get('update_available')]
    if not pending and not local.get('update_available'):
        print('Todos los paneles están actualizados.')
        return 0
    print('\nSe actualizarán secuencialmente los destinos y, al final, este equipo.')
    if input('¿Autorizar las actualizaciones en todos los pendientes? [s/N]: ').strip().lower() not in ('s', 'si', 'sí'):
        print('Cancelado.')
        return 0
    for t in pending:
        print(f"\n=== Actualizando {t['name']} ===", flush=True)
        try:
            result = ssh_command(t, 'update', timeout=1200)
        except (OSError, subprocess.TimeoutExpired) as e:
            die(f'Fallo en {t["name"]}: {e}. Detenidas las demás actualizaciones.')
        print(result.stdout[-6000:])
        if result.returncode:
            die(f'Fallo en {t["name"]}: {result.stderr[-600:]}. Detenidas las demás actualizaciones.')
        check, error = remote_check(t)
        if error or check.get('update_available'):
            die(f'Verificación fallida en {t["name"]}: {error or check}. Detenidas las demás actualizaciones.')
        print(f"Verificado: {t['name']} v{check['current']}")
    if local.get('update_available'):
        print('\n=== Actualizando principal ===', flush=True)
        result = subprocess.run(['sudo', UPDATER, '--fleet-apply'], timeout=1200)
        if result.returncode:
            die('Falló la actualización local. Consulta el registro de instalación.')
        local_after = local_check()
        if local_after.get('update_available'):
            die('La actualización local sigue pendiente.')
    print('\nActualizaciones terminadas y verificadas.')
    return 0


def main():
    p = argparse.ArgumentParser(description='Gestor SSH opcional de Miura Panel, solo por consola.')
    s = p.add_subparsers(dest='action', required=True)
    for cmd in ('init', 'list', 'check', 'update', 'primary', 'secondary'):
        s.add_parser(cmd)
    a = s.add_parser('add')
    a.add_argument('name')
    a.add_argument('host')
    a.add_argument('--port', type=int, default=22, help='Puerto SSH, no el puerto web SEP')
    r = s.add_parser('remove')
    r.add_argument('name')
    al = s.add_parser('allow')
    al.add_argument('public_key', help='Clave pública ed25519 completa, con comillas simples')
    s.add_parser('docker-enable')
    s.add_parser('docker-disable')
    revo = s.add_parser('revoke')
    revo.add_argument('public_key')
    args = p.parse_args()
    if args.action == 'init': return init()
    if args.action == 'primary': return primary()
    if args.action == 'secondary': return secondary()
    if args.action == 'allow': return allow(args.public_key)
    if args.action == 'docker-enable': return docker_permission(True)
    if args.action == 'docker-disable': return docker_permission(False)
    if args.action == 'revoke': return revoke(args.public_key)
    if args.action == 'add': return add(args.name, args.host, args.port)
    if args.action == 'remove': return remove(args.name)
    if args.action == 'list': return list_targets()
    if args.action == 'check': return execute()
    if args.action == 'update': return execute(True)


if __name__ == '__main__':
    sys.exit(main())
