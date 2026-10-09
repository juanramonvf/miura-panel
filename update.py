#!/usr/bin/env python3
"""Miura Panel: comprobar la fuente HTTPS y actualizar de modo controlado.

Se usa por consola o desde un servicio systemd separado que solo puede iniciar
la interfaz administrativa HTTPS después de autenticación y confirmación.

La URL de un manifiesto de versiones debe configurarse por el administrador
como DASHBOARD_UPDATE_MANIFEST en /etc/server-dashboard.conf.
No escucha conexiones de red ni acepta comandos o instaladores arbitrarios por HTTP.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CONFIG = Path('/etc/server-dashboard.conf')
WEB = Path('/var/www/server-dashboard')
def local_version():
    """Leer y validar la versión realmente instalada; nunca fijarla en el instalador."""
    try:
        info = json.loads((WEB / 'version.json').read_text(encoding='utf-8'))
        version = info['version']
        version_tuple(version)
        return version
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('No se puede identificar la versión local desde version.json') from exc
MAX_MANIFEST = 65536
MAX_INSTALLER = 2 * 1024 * 1024


def cfg_get(key):
    if not CONFIG.exists():
        return ''
    for raw in CONFIG.read_text(encoding='utf-8').splitlines():
        k, eq, v = raw.strip().partition('=')
        if eq and k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ''


def version_tuple(v):
    if not isinstance(v, str) or not re.fullmatch(r'\d+\.\d+\.\d+', v):
        raise ValueError('Versión de manifiesto inválida')
    return tuple(map(int, v.split('.')))


def validated_https(url):
    p = urlparse(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.fragment:
        raise ValueError('La descarga exige HTTPS válido sin credenciales en URL')
    return url


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validated_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get_limited(url, limit):
    validated_https(url)
    req=urllib.request.Request(url, headers={'User-Agent':'MiuraPanel/updater'})
    # No seguir redirecciones a HTTP ni otros orígenes sin HTTPS
    with urllib.request.build_opener(HttpsOnlyRedirect()).open(req, timeout=15) as resp:
        validated_https(resp.geturl())
        data=resp.read(limit+1)
    if len(data)>limit:
        raise ValueError('Descarga demasiado grande')
    return data


def fetch_manifest():
    feed=cfg_get('DASHBOARD_UPDATE_MANIFEST')
    if not feed:
        return None
    raw=get_limited(feed,MAX_MANIFEST)
    info=json.loads(raw.decode('utf-8'))
    if not isinstance(info,dict):
        raise ValueError('Manifiesto inválido')
    ver=info.get('version')
    version_tuple(ver)
    url=validated_https(info.get('installer',''))
    sha=info.get('sha256','')
    if not isinstance(sha,str) or re.fullmatch('[0-9a-f]{64}',sha) is None:
        raise ValueError('SHA256 de manifiesto inválido')
    return ver,url,sha


def check():
    current='desconocida'
    try:
        current=local_version()
        release=fetch_manifest()
        if release is None:
            return {'current':current,'status':'not_configured','update_available':False,'checked_at':int(time.time())}
        ver,url,sha=release
        return {'current':current,'latest':ver,'status':'ok',
                'update_available':version_tuple(ver)>version_tuple(current),'checked_at':int(time.time())}
    except Exception as e:
        return {'current':current,'status':'error','error':str(e),
                'update_available':False,'checked_at':int(time.time())}


def write_status(status):
    WEB.mkdir(parents=True,exist_ok=True)
    dst=WEB/'panel-update.json'
    tmp=WEB/'.panel-update.tmp'
    tmp.write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.chmod(0o644)
    os.replace(tmp,dst)


def apply(fleet=False):
    if os.geteuid()!=0:
        raise SystemExit('Ejecuta con sudo: sudo miura-panel-update')
    current=local_version()
    release=fetch_manifest()
    if release is None:
        raise SystemExit('Sin fuente online configurada todavía. No se ha modificado nada.')
    ver,url,sha=release
    if version_tuple(ver)<=version_tuple(current):
        print('Miura Panel ya está actualizado: v'+current)
        return
    print(f'Nueva versión: {ver} (actual: {current})')
    print('Origen:',url)
    confirm='s' if fleet else input('¿Descargar y verificar la actualización? [s/N]: ').strip().lower()
    if confirm not in ('s','si','sí'):
        print('Cancelado sin cambios')
        return
    content=get_limited(url,MAX_INSTALLER)
    if hashlib.sha256(content).hexdigest()!=sha:
        raise SystemExit('ERROR: SHA256 incorrecto. No se ejecuta el instalador.')
    with tempfile.TemporaryDirectory(prefix='sep-update-') as temp:
        installer=Path(temp)/'install-spanish-empire-panel.sh'
        installer.write_bytes(content)
        subprocess.run(['bash','-n',str(installer)],check=True)
        # El instalador presenta resumen y confirmación y realiza copia previa.
        if fleet:
            # Una sola autorización explícita del administrador del gestor SSH.
            # No se abre ninguna interfaz privilegiada en el servidor web.
            subprocess.run(['/bin/bash',str(installer),'--upgrade'],input=b's\n',check=True)
        else:
            subprocess.run(['/bin/bash',str(installer),'--upgrade'],check=True)


def main():
    if len(sys.argv)>2 or (len(sys.argv)==2 and sys.argv[1] not in ('--check','--fleet-apply')):
        raise SystemExit('Uso: sudo miura-panel-update [--check|--fleet-apply]')
    if len(sys.argv)==2 and sys.argv[1]=='--check':
        status=check()
        if os.geteuid()==0:
            write_status(status)
        print(json.dumps(status,ensure_ascii=False))
    else:
        apply(fleet=(len(sys.argv)==2 and sys.argv[1]=='--fleet-apply'))

if __name__=='__main__':
    main()
