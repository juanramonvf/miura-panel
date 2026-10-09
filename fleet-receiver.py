#!/usr/bin/env python3
"""Puerta SSH restringida; SOLO accesible por claves autorizadas explícitamente."""
import os
import subprocess
import sys
import json
import shutil
from pathlib import Path

UPDATER = '/usr/local/sbin/spanish-empire-panel-update'
DOCKER = '/usr/local/sbin/miura-panel-fleet-docker'
CONFIG = Path('/etc/server-dashboard.conf')


def main():
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    if command == 'check':
        result = subprocess.run(['sudo', '-n', UPDATER, '--check'], timeout=50)
        return result.returncode
    if command == 'update':
        # La autorización central se produce al empezar el lote; no se aceptan
        # parámetros adicionales ni se ofrece una shell al operador.
        result = subprocess.run(['sudo', '-n', UPDATER, '--fleet-apply'], timeout=1200)
        return result.returncode
    if command == 'docker-probe':
        # Diagnóstico de solo lectura: permite que el principal muestre un estado
        # comprensible sin conceder permisos Docker adicionales.
        if shutil.which('docker') is None:
            print(json.dumps({'ok':True,'state':'no_docker','label':'Sin Docker',
                              'detail':'No se detecta Docker en este servidor.'}, ensure_ascii=False))
            return 0
        if not Path(DOCKER).is_file():
            print(json.dumps({'ok':True,'state':'unavailable','label':'No disponible',
                              'detail':'Este Miura Panel no dispone del broker Docker remoto. Actualiza el panel.'}, ensure_ascii=False))
            return 0
        try:
            conf=CONFIG.read_text(encoding='utf-8')
            updater=any(line.strip().startswith('DASHBOARD_DOCKER_UPDATER=') and line.split('=',1)[1].strip().strip(chr(34)+chr(39))=='1' for line in conf.splitlines())
        except OSError:
            updater=False
        if not updater:
            print(json.dumps({'ok':True,'state':'not_authorized','label':'No autorizado',
                              'detail':'Docker está instalado, pero el actualizador Docker local está desactivado.',
                              'command':'sudo miura-panel-fleet docker-enable'}, ensure_ascii=False))
            return 0
        raw=json.dumps({'action':'probe'}).encode('utf-8')
        p=subprocess.run(['sudo','-n',DOCKER],input=raw,capture_output=True,timeout=10)
        if p.stdout:
            try:
                doc=json.loads(p.stdout[:65536])
                if isinstance(doc,dict) and doc.get('ok') and doc.get('state') in ('authorized','not_authorized','no_docker','unavailable'):
                    print(json.dumps(doc,ensure_ascii=False))
                    return 0
            except (ValueError,TypeError):
                pass
        print(json.dumps({'ok':True,'state':'not_authorized','label':'No autorizado',
                          'detail':'Falta la autorización Docker SSH en este destino.',
                          'command':'sudo miura-panel-fleet docker-enable'}, ensure_ascii=False))
        return 0
    if command == 'docker':
        # La cuenta sep-fleet tiene una orden forzada; sin shell ni Docker CLI.
        # El helper root exige autorización local explícita y aplica las
        # mismas restricciones Compose/Portainer que el broker local.
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            print('{"ok":false,"error":"Petición demasiado grande"}')
            return 1
        try:
            obj = json.loads(raw)
            if not isinstance(obj,dict) or obj.get('action') not in ('list','update_one','update_group','update_all','status'):
                raise ValueError('Acción no permitida')
        except (ValueError,TypeError):
            print('{"ok":false,"error":"Petición Docker inválida"}')
            return 1
        p = subprocess.run(['sudo','-n',DOCKER],input=raw,
                           capture_output=True,timeout=16)
        if p.stdout:
            sys.stdout.buffer.write(p.stdout[:65536])
        if p.returncode and not p.stdout:
            print(json.dumps({'ok':False,'error':'Docker SSH no autorizado en el destino. Ejecuta sudo miura-panel-fleet docker-enable.'}))
        return p.returncode
    print('Operación SSH no permitida: únicamente check/update/docker-probe/docker.', file=sys.stderr)
    return 126


if __name__ == '__main__':
    sys.exit(main())
