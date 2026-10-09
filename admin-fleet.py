#!/usr/bin/env python3
"""Tareas SSH de SEP; sin comandos arbitrarios, ejecutadas como sep-admin.

El trabajo se ejecuta desde una unidad systemd separada del proceso web.
"""
import json
import os
import re
import ipaddress
import subprocess
import sys
import time
import tempfile
import fcntl
from pathlib import Path

STATE = Path('/var/lib/spanish-empire-panel-admin')
TARGETS = STATE / 'targets.json'
JOB = STATE / 'job.json'
HISTORY = STATE / 'job-history.json'
KEY = STATE / 'id_ed25519'
KNOWN = STATE / 'known_hosts'
LOCK = STATE / 'job.lock'
SSH = '/usr/bin/ssh'


def safe_target(t):
    assert isinstance(t, dict)
    name, host, port = t['name'], t['host'], t['port']
    assert isinstance(name,str) and re.fullmatch(r'[\wÀ-ÿ .-]{1,64}',name)
    assert isinstance(host,str) and 0 < len(host) < 254 and not host.startswith('-')
    assert '@' not in host and '/' not in host and ':' not in host and '\\' not in host
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        assert re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?',host)
    assert type(port) is int and 1 <= port <= 65535
    return {'name': name, 'host': host, 'port': port}


def targets():
    try:
        arr=json.loads(TARGETS.read_text(encoding='utf-8'))
        assert isinstance(arr,list) and len(arr)<=100
        return [safe_target(t) for t in arr]
    except FileNotFoundError:
        return []


def write_targets(arr):
    assert len(arr)<=100
    for t in arr: safe_target(t)
    fd, fn=tempfile.mkstemp(prefix='.targets-',dir=STATE)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(arr,f,ensure_ascii=False,indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(fn,0o600)
        os.replace(fn,TARGETS)
    finally:
        if os.path.exists(fn): os.unlink(fn)


def ssh_run(t, action, timeout):
    t=safe_target(t)
    assert action in ('check','update')
    if not KEY.is_file() or not KNOWN.is_file():
        return None,'Clave/huella SSH no configuradas'
    cmd=[SSH,'-T','-p',str(t['port']),'-i',str(KEY),
         '-o','BatchMode=yes','-o','IdentitiesOnly=yes',
         '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(KNOWN),
         '-o','ConnectTimeout=8','-o','NumberOfPasswordPrompts=0',
         'sep-fleet@'+t['host'],action]
    try:
        proc=subprocess.run(cmd, capture_output=True,text=True,timeout=timeout)
        if proc.returncode: return None,(proc.stderr or proc.stdout or 'Error SSH')[-700:]
        return proc.stdout,None
    except (OSError,subprocess.TimeoutExpired) as e:
        return None,str(e)


def remote_check(t):
    raw,err=ssh_run(t,'check',40)
    if err: return None,err
    try:
        doc=json.loads(raw.strip())
        if (doc.get('status') not in ('ok','not_configured') or
                not re.fullmatch(r'\d+\.\d+\.\d+',doc['current'])):
            raise ValueError('respuesta no reconocida')
        return doc,None
    except (TypeError,ValueError,KeyError):
        return None,'El destino no devolvió una versión SEP válida'


def write_job(obj):
    fd,fn=tempfile.mkstemp(prefix='.job-',dir=STATE)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(obj,f,ensure_ascii=False)
            f.flush();os.fsync(f.fileno())
        os.chmod(fn,0o600)
        os.replace(fn,JOB)
    finally:
        if os.path.exists(fn): os.unlink(fn)


def latest_local():
    # Misma fuente HTTPS y validaciones que el actualizador oficial, incluso
    # cuando el administrador ha configurado un manifiesto distinto.
    import importlib.util
    source='/usr/local/sbin/spanish-empire-panel-update'
    spec=importlib.util.spec_from_file_location('sep_updater',source)
    # El actualizador no termina en .py: cargar como código Python verificado.
    from importlib.machinery import SourceFileLoader
    loader=SourceFileLoader('sep_updater',source)
    mod=importlib.util.module_from_spec(importlib.util.spec_from_loader('sep_updater',loader))
    loader.exec_module(mod)
    check=mod.check()
    if check.get('status')!='ok':
        raise ValueError('Comprobación HTTPS local fallida: '+str(check.get('error','sin manifiesto')))
    return check['current'],check['latest'],check['update_available']


def history():
    try:
        data = json.loads(HISTORY.read_text(encoding='utf-8'))
        return data[:15] if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def save_history(doc):
    # Solo se conservan nombres, estados y resultados: no claves ni comandos SSH.
    summary = {k: doc.get(k) for k in
               ('run_id', 'mode', 'state', 'started', 'finished', 'message')}
    summary['items'] = [{k: row.get(k) for k in
                         ('name', 'host', 'state', 'current', 'latest', 'message')}
                        for row in doc.get('items', [])]
    summary['local'] = doc.get('local')
    entries = [summary] + [r for r in history() if r.get('run_id') != doc.get('run_id')]
    fd, fn = tempfile.mkstemp(prefix='.history-', dir=STATE)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(entries[:15], handle, ensure_ascii=False)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.chmod(fn, 0o600)
        os.replace(fn, HISTORY)
    finally:
        if os.path.exists(fn): os.unlink(fn)


def add_event(doc, message, name='', level='info'):
    doc.setdefault('events', []).append({
        'at': int(time.time()), 'name': name, 'text': message[:350], 'level': level})
    doc['events'] = doc['events'][-80:]
    write_job(doc)


def run(mode):
    assert mode in ('check', 'update')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK.open('a+') as lf:
        try: fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit('Ya hay una operación en curso')
        doc = {'run_id': str(time.time_ns()), 'state': 'running', 'mode': mode,
               'phase': 'checking', 'started': int(time.time()), 'items': [],
               'events': [], 'message': 'Verificando las conexiones SSH'}
        write_job(doc)
        add_event(doc, 'Iniciada ' + ('comprobación' if mode == 'check' else 'actualización') + ' de la flota')
        try:
            ts = targets()
            if mode == 'update' and not json.loads(Path('/var/www/server-dashboard/fleet-role.json').read_text()).get('role') == 'principal':
                raise ValueError('Este servidor no está designado principal')
            doc['items'] = [{'name': t['name'], 'host': t['host'], 'state': 'queued'} for t in ts]
            doc['total_targets'] = len(ts)
            write_job(doc)
            has_error = False
            for row, t in zip(doc['items'], ts):
                row['state'] = 'checking'
                doc['message'] = 'Comprobando ' + t['name']
                write_job(doc)
                check, err = remote_check(t)
                if err or not check or check.get('status') != 'ok':
                    row.update(state='error', message=(err or 'Canal de actualización no configurado')[:600])
                    has_error = True
                    add_event(doc, 'Error de comprobación: ' + row['message'], t['name'], 'error')
                else:
                    row.update(state='pending' if check.get('update_available') else 'updated',
                               current=check['current'], latest=check.get('latest'))
                    add_event(doc, 'Actualización pendiente' if check.get('update_available') else 'Ya está actualizado', t['name'])
            doc['message'] = 'Comprobando este servidor'
            write_job(doc)
            current, latest, pending = latest_local()
            doc['local'] = {'current': current, 'latest': latest, 'pending': pending,
                            'state': 'pending' if pending else 'updated'}
            add_event(doc, 'Versión local comprobada')
            if has_error:
                if mode == 'update':
                    raise ValueError('Algún destino está inaccesible: canceladas TODAS las actualizaciones')
                raise ValueError('La comprobación ha terminado con errores de conexión SSH')
            if mode == 'update':
                doc['phase'] = 'updating'
                doc['message'] = 'Actualizando los servidores pendientes'
                write_job(doc)
                for row, t in zip(doc['items'], ts):
                    if row['state'] != 'pending':
                        continue
                    row.update(state='updating')
                    doc['message'] = 'Actualizando ' + t['name']
                    add_event(doc, 'Instalando actualización', t['name'])
                    raw, err = ssh_run(t, 'update', 1200)
                    if err:
                        row.update(state='error', message=err[:600])
                        add_event(doc, 'Actualización fallida: ' + err[:220], t['name'], 'error')
                        raise ValueError('Actualización fallida en ' + t['name'] + '; detenido el resto')
                    row['state'] = 'verifying'
                    doc['message'] = 'Verificando ' + t['name']
                    write_job(doc)
                    checked, err = remote_check(t)
                    if err or not checked or checked.get('update_available') or checked.get('status') != 'ok':
                        row.update(state='error', message=(err or 'Verificación posterior fallida')[:600])
                        add_event(doc, row['message'], t['name'], 'error')
                        raise ValueError('Verificación fallida en ' + t['name'] + '; detenido el resto')
                    row.update(state='updated', current=checked['current'])
                    add_event(doc, 'Actualizado y verificado: v' + checked['current'], t['name'], 'ok')
                if pending:
                    doc['phase'] = 'local'
                    doc['local']['state'] = 'updating'
                    doc['message'] = 'Servidores remotos verificados; iniciando actualización local'
                    write_job(doc)
                    proc = subprocess.run(['sudo', '-n', '/usr/bin/systemctl', 'start', '--no-block',
                                           'server-dashboard-admin-local-update.service'],
                                          capture_output=True, text=True, timeout=12)
                    if proc.returncode:
                        raise ValueError('No se pudo iniciar actualización local: ' + proc.stderr[-300:])
                    doc['state'] = 'local_pending'
                    doc['message'] = 'Actualización local iniciada; esperando confirmación del servicio'
                    add_event(doc, 'Actualización del servidor principal iniciada')
                    # El servicio web puede reiniciarse. /api/admin/job comprueba después el resultado real.
                    write_job(doc)
                    return 0
            doc.update(state='done', phase='finished',
                       message=('Comprobación finalizada correctamente' if mode == 'check' else 'Actualización finalizada correctamente'),
                       finished=int(time.time()))
            add_event(doc, doc['message'], level='ok')
        except Exception as e:
            doc.update(state='error', phase='finished', failed_phase=doc.get('phase'), message=str(e)[:600],
                       finished=int(time.time()))
            add_event(doc, 'Operación detenida: ' + doc['message'], level='error')
        write_job(doc)
        save_history(doc)
        return 1 if doc['state'] == 'error' else 0


if __name__=='__main__':
    if len(sys.argv)!=2 or sys.argv[1] not in ('check','update'):
        sys.exit('Uso: sep-admin-fleet.py check|update')
    sys.exit(run(sys.argv[1]))



def remote_docker_probe(t):
    """Consulta de estado Docker remota sin habilitar permisos ni ejecutar acciones."""
    t = safe_target(t)
    if not KEY.is_file() or not KNOWN.is_file():
        return {'ok': True, 'state': 'unavailable', 'label': 'No disponible',
                'detail': 'Clave o huella SSH no configuradas en el principal.'}
    args=[SSH,'-T','-p',str(t['port']),'-i',str(KEY),
          '-o','BatchMode=yes','-o','IdentitiesOnly=yes',
          '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(KNOWN),
          '-o','ConnectTimeout=8','-o','NumberOfPasswordPrompts=0',
          'sep-fleet@'+t['host'],'docker-probe']
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=14)
    except (subprocess.TimeoutExpired,OSError) as exc:
        return {'ok': True, 'state': 'unavailable', 'label': 'No disponible',
                'detail': ('No se puede contactar por SSH: '+str(exc))[:300]}
    try:
        doc=json.loads(result.stdout)
    except (ValueError,TypeError):
        detail=(result.stderr or result.stdout or 'sin respuesta SSH')[-300:]
        return {'ok': True, 'state': 'unavailable', 'label': 'No disponible',
                'detail': 'El destino no respondió al diagnóstico Docker: '+detail}
    if not isinstance(doc,dict) or not doc.get('ok') or doc.get('state') not in ('authorized','not_authorized','no_docker','unavailable'):
        return {'ok': True, 'state': 'unavailable', 'label': 'No disponible',
                'detail': str(doc.get('error','Respuesta Docker no válida'))[:300] if isinstance(doc,dict) else 'Respuesta Docker no válida'}
    return {k:doc[k] for k in ('ok','state','label','detail','command') if k in doc}

def remote_docker(t, request):
    """RPC limitado a una orden SSH forzada y huella Ed25519 validada."""
    t = safe_target(t)
    if not isinstance(request, dict) or request.get('action') not in (
            'list', 'update_one', 'update_group', 'update_all', 'status'):
        raise ValueError('Operación Docker no permitida')
    if not KEY.is_file() or not KNOWN.is_file():
        raise ValueError('Clave o huella SSH no configuradas')
    args=[SSH,'-T','-p',str(t['port']),'-i',str(KEY),
          '-o','BatchMode=yes','-o','IdentitiesOnly=yes',
          '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(KNOWN),
          '-o','ConnectTimeout=8','-o','NumberOfPasswordPrompts=0',
          'sep-fleet@'+t['host'],'docker']
    raw=json.dumps(request,ensure_ascii=False,separators=(',',':'))
    if len(raw.encode('utf-8'))>4096:
        raise ValueError('Petición demasiado grande')
    try:
        result=subprocess.run(args,input=raw,capture_output=True,text=True,timeout=24)
    except (subprocess.TimeoutExpired,OSError) as exc:
        raise ValueError('Error SSH: '+str(exc)) from exc
    if len(result.stdout)>70000:
        raise ValueError('Respuesta Docker excesiva')
    try:
        doc=json.loads(result.stdout)
    except (ValueError,TypeError) as exc:
        raise ValueError('Docker SSH sin autorización o inaccesible: '+(result.stderr or result.stdout or 'sin respuesta')[-300:]) from exc
    if not isinstance(doc,dict) or not doc.get('ok'):
        raise ValueError(str(doc.get('error','Operación Docker rechazada'))[:350] if isinstance(doc,dict) else 'Respuesta Docker inválida')
    if result.returncode:
        raise ValueError('Fallo SSH del destino: '+(result.stderr or '')[-350:])
    return doc
