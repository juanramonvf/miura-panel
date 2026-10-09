#!/usr/bin/env python3
"""Pasarela local Docker de Miura Panel: acciones cerradas y opt-in explícito.

Se ejecuta como root únicamente desde la cuenta SSH forzada sep-fleet.
No acepta argumentos de shell, rutas de Compose ni peticiones HTTP.
"""
import json
import os
import re
import socket
import sys
import shutil
import time
from pathlib import Path

SOCK = Path('/run/server-dashboard/docker-action.sock')
UPDATES = Path('/var/www/server-dashboard/docker-updates.json')
AUDIT = Path('/var/lib/server-dashboard/fleet-docker-audit.log')
MARKER = Path('/etc/spanish-empire-panel/fleet-docker-enabled')
CONFIG = Path('/etc/server-dashboard.conf')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
JOB = re.compile(r'[0-9a-f]{12}\Z')


def fail(reason):
    print(json.dumps({'ok': False, 'error': str(reason)[:350]}, ensure_ascii=False))
    return 1



def updater_enabled():
    try:
        text = CONFIG.read_text(encoding='utf-8')
    except OSError:
        return False
    return any(line.strip().startswith('DASHBOARD_DOCKER_UPDATER=') and line.split('=',1)[1].strip().strip(chr(34)+chr(39))=='1' for line in text.splitlines())


def probe_status():
    if shutil.which('docker') is None:
        return {'ok': True, 'state': 'no_docker', 'label': 'Sin Docker',
                'detail': 'No se detecta Docker en este servidor.'}
    if not MARKER.is_file():
        return {'ok': True, 'state': 'not_authorized', 'label': 'No autorizado',
                'detail': 'Docker está instalado, pero Miura Panel no tiene permiso Docker SSH en este destino.',
                'command': 'sudo miura-panel-fleet docker-enable'}
    if not updater_enabled():
        return {'ok': True, 'state': 'not_authorized', 'label': 'No autorizado',
                'detail': 'El actualizador Docker local está desactivado en este servidor.',
                'command': 'sudo miura-panel-fleet docker-enable'}
    if not SOCK.is_socket():
        return {'ok': True, 'state': 'unavailable', 'label': 'No disponible',
                'detail': 'El broker Docker local no está disponible. Revisa server-dashboard-docker-action.service.'}
    return {'ok': True, 'state': 'authorized', 'label': 'Autorizado',
            'detail': 'Docker SSH está autorizado mediante el broker restringido de Miura Panel.'}

def config_allowed():
    if not MARKER.is_file():
        raise ValueError('Falta autorización Docker SSH. Ejecuta en este destino: sudo miura-panel-fleet docker-enable')
    if not updater_enabled():
        raise ValueError('Este servidor mantiene Docker en solo lectura (actualizador local desactivado).')
    if not SOCK.is_socket():
        raise ValueError('Broker Docker local no disponible. Comprueba server-dashboard-docker-action.service.')


def read_updates():
    raw = json.loads(UPDATES.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('Inventario Docker inválido')
    containers = []
    for c in raw.get('containers', []):
        if not isinstance(c, dict):
            continue
        n = c.get('name')
        if not isinstance(n, str) or not NAME.fullmatch(n):
            continue
        containers.append({k: c.get(k) for k in ('name', 'installed', 'latest', 'update', 'actionable', 'bulk_allowed', 'group', 'check', 'reason')})
    groups = []
    for g in raw.get('groups', []):
        if isinstance(g, dict) and g.get('id') == 'immich':
            groups.append({k: g.get(k) for k in ('id','name','installed','latest','update','actionable','reason')})
    return {'ok': True, 'containers': containers[:400], 'groups': groups,
            'updated_at': raw.get('timestamp')}


def broker(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(12)
        s.connect(str(SOCK))
        s.sendall((json.dumps(request, separators=(',', ':')) + '\n').encode())
        stream = s.makefile('rb')
        payload = stream.readline(65537)
    if not payload or len(payload) > 65536:
        raise ValueError('Respuesta del broker Docker no válida')
    result = json.loads(payload)
    if not isinstance(result, dict):
        raise ValueError('Respuesta Docker inválida')
    return result


def record(action, target, result):
    AUDIT.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    with AUDIT.open('a', encoding='utf-8') as f:
        f.write(f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")} {action} {target} {"ok" if result.get("ok") else "error"}\n')
    os.chmod(AUDIT, 0o600)


def run():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        return fail('Uso no autorizado')
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        return fail('Petición demasiado grande')
    try:
        req = json.loads(raw)
        if not isinstance(req, dict) or set(req) - {'action','container','group','job_id'}:
            raise ValueError('Campos de petición no permitidos')
        action = req.get('action')
        if action not in ('probe','list','update_one','update_group','update_all','status'):
            raise ValueError('Operación Docker no permitida')
        if action == 'probe':
            result = probe_status()
        else:
            config_allowed()
        if action == 'probe':
            pass
        elif action == 'list':
            result = read_updates()
        elif action == 'status':
            job_id = req.get('job_id')
            if not isinstance(job_id, str) or not JOB.fullmatch(job_id):
                raise ValueError('Identificador de trabajo inválido')
            result = broker({'action':'status', 'job_id':job_id})
        elif action == 'update_one':
            name = req.get('container')
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise ValueError('Nombre de contenedor inválido')
            record('requested:update_one',name,{'ok':True})
            result = broker({'action':'update_one', 'container':name})
        elif action == 'update_group':
            if req.get('group') != 'immich':
                raise ValueError('Grupo no autorizado')
            record('requested:update_group','immich',{'ok':True})
            result = broker({'action':'update_group','group':'immich'})
        else:
            record('requested:update_all','',{'ok':True})
            result = broker({'action':'update_all'})
        if action in ('update_one','update_group','update_all'):
            try: record(action, str(req.get('container',req.get('group',''))), result)
            except OSError: pass  # No ocultar un trabajo que el broker ya aceptó.
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result.get('ok') else 1
    except (OSError, ValueError, TypeError, KeyError, socket.timeout, json.JSONDecodeError) as exc:
        return fail(str(exc))


if __name__ == '__main__':
    sys.exit(run())
