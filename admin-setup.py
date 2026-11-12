#!/usr/bin/env python3
"""Activación/reconfiguración LOCAL del modo administración HTTPS de SEP.

Sin valores por defecto de contraseña. No abre firewall ni modifica el router.
"""
import getpass
import grp
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

ROOT_PREFIX=Path(os.environ['MIURA_ROOT_PREFIX']).resolve() if os.environ.get('MIURA_ROOT_PREFIX') else None
if ROOT_PREFIX == Path('/'):
    raise SystemExit('MIURA_ROOT_PREFIX no puede ser /; usa una raíz temporal aislada')
def rooted(path):
    path=Path(path)
    return ROOT_PREFIX / str(path).lstrip('/') if ROOT_PREFIX else path

ROOT=rooted('/etc/spanish-empire-panel')
STATE=rooted('/var/lib/spanish-empire-panel-admin')
CONF=rooted('/etc/server-dashboard.conf')
MARK=ROOT/'admin-enabled'
ADMIN=ROOT/'admin.json'
CERT=ROOT/'admin.crt'
KEY=ROOT/'admin.key'
SERVICE='server-dashboard-admin.service'
WEB=rooted('/var/www/server-dashboard')
STATUS=WEB/'admin-status.json'
ADMIN_UNIT=rooted('/etc/systemd/system/server-dashboard-admin.service')
ADMIN_UNIT_FALLBACKS=(
    rooted('/usr/lib/systemd/system/server-dashboard-admin.service'),
    rooted('/lib/systemd/system/server-dashboard-admin.service'),
)
ACCOUNT_STATE=STATE/'account.json'


def die(s):
    raise SystemExit('ERROR: '+s)


def run(*cmd,timeout=90):
    subprocess.run(cmd,check=True,timeout=timeout)


def validate_admin_unit(path):
    try:
        lines=Path(path).read_text(encoding='utf-8').splitlines()
    except (OSError,UnicodeError):
        die('No se pudo leer server-dashboard-admin.service de forma segura')
    expected_service_lines=[
        'Type=simple',
        'User=sep-admin',
        'Group=sep-admin',
        'SupplementaryGroups=server-dashboard-read server-dashboard-prefs',
        'WorkingDirectory=/var/www/server-dashboard',
        'ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.json',
        'ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.crt',
        'ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.key',
        'ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-admin.py',
        'Restart=on-failure',
        'RestartSec=3',
        'UMask=0077',
        'NoNewPrivileges=true',
        'PrivateTmp=true',
        'ProtectSystem=strict',
        'ProtectHome=true',
        'UnsetEnvironment=MIURA_ROOT_PREFIX',
        'ProtectKernelTunables=true',
        'ProtectKernelModules=true',
        'ProtectControlGroups=true',
        'StateDirectory=spanish-empire-panel-admin',
        'StateDirectoryMode=0700',
        'ReadWritePaths=/var/lib/server-dashboard/preferences',
    ]
    service_lines=[]
    service_sections=0
    in_service=False
    for line in lines:
        if line.strip().startswith('['):
            in_service=line.strip()=='[Service]'
            service_sections+=int(in_service)
            continue
        if in_service and line.strip() and not line.lstrip().startswith('#'):
            service_lines.append(line.strip())
    if service_sections!=1 or service_lines!=expected_service_lines:
        die('La unidad administrativa existente no coincide exactamente con el perfil seguro requerido; no se activarán credenciales')
    unit_name='server-dashboard-admin.service.d'
    for directory in (
        rooted('/etc/systemd/system')/unit_name,
        rooted('/run/systemd/system')/unit_name,
        rooted('/usr/lib/systemd/system')/unit_name,
        rooted('/lib/systemd/system')/unit_name,
    ):
        if directory.is_symlink():
            die(f'No se permite un directorio de overrides administrativo enlazado: {directory}')
        if directory.exists():
            if not directory.is_dir() or any(directory.iterdir()):
                die(f'No se permiten overrides de la unidad administrativa: {directory}')


def find_admin_unit():
    for path in (ADMIN_UNIT,*ADMIN_UNIT_FALLBACKS):
        if path.is_symlink():
            die(f'La unidad administrativa no puede ser un enlace simbólico: {path}')
        if path.is_file():
            return path
    return None


def ensure_prerequisites():
    unit_path=find_admin_unit()
    if ROOT_PREFIX:
        if unit_path is None:
            die('Falta la unidad administrativa en la raíz aislada; instala primero Miura Panel')
        validate_admin_unit(unit_path)
        try:
            admin_group=grp.getgrnam('sep-admin')
            account=__import__('pwd').getpwnam('sep-admin')
            return admin_group.gr_gid,account.pw_uid
        except KeyError:
            return os.getgid(),os.getuid()
    try:
        admin_group=grp.getgrnam('sep-admin')
    except KeyError:
        die('Falta el grupo de servicio sep-admin. Reinstala Miura Panel v1.0.39 para provisionarlo')
    try:
        account=__import__('pwd').getpwnam('sep-admin')
    except KeyError:
        die('Falta la cuenta de servicio sep-admin. Reinstala Miura Panel v1.0.39 para provisionarla')
    if account.pw_gid != admin_group.gr_gid or account.pw_shell not in ('/usr/sbin/nologin','/bin/false'):
        die('La cuenta sep-admin no tiene la configuración dedicada esperada; revisa el sistema')
    if admin_group.gr_mem:
        die('El grupo sep-admin tiene miembros adicionales; revisa el sistema antes de activar HTTPS')
    if unit_path is None:
        die('Falta server-dashboard-admin.service; reinstala Miura Panel v1.0.39')
    st=unit_path.stat()
    if st.st_uid!=0 or st.st_mode&0o022:
        die('server-dashboard-admin.service debe pertenecer a root y no permitir escritura a grupo u otros')
    validate_admin_unit(unit_path)
    return admin_group.gr_gid,account.pw_uid


def check_https_ready(bind,port,attempts=30):
    target='127.0.0.1' if bind in ('0.0.0.0','::') else bind
    context=ssl._create_unverified_context()
    try:expected_cert=ssl.PEM_cert_to_DER_cert(CERT.read_text(encoding='ascii'))
    except (OSError,ValueError) as exc:raise RuntimeError(f'No se pudo leer el certificado instalado: {exc}') from None
    last=None
    for _ in range(attempts):
        connection=None
        try:
            class PeerHTTPSConnection(http.client.HTTPSConnection):
                def connect(self):
                    super().connect()
                    self.peer_cert=self.sock.getpeercert(binary_form=True)
            connection=PeerHTTPSConnection(target,port,context=context,timeout=1)
            connection.request('GET','/login',headers={'Connection':'close'})
            response=connection.getresponse()
            response.read()
            if response.status==200 and hmac.compare_digest(connection.peer_cert,expected_cert):
                return True
            last=f'HTTP {response.status} o certificado TLS inesperado'
        except (OSError,ValueError,TimeoutError,http.client.HTTPException) as exc:
            last=str(exc)
        finally:
            if connection is not None:connection.close()
        time.sleep(0.25)
    raise RuntimeError(f'No se pudo validar HTTPS en {target}:{port}/login con el certificado configurado: {last or "sin respuesta"}')


def activate_admin(bind,port):
    """Enable only after the configured credentials and TLS endpoint work."""
    if ROOT_PREFIX:
        check_https_ready(bind,port)
        run('/usr/bin/systemctl','--root',str(ROOT_PREFIX),'enable',SERVICE,timeout=30)
        return
    run('/usr/bin/systemctl','daemon-reload')
    run('/usr/bin/systemctl','restart' if MARK.exists() else 'start',SERVICE)
    try:
        check_https_ready(bind,port)
        run('/usr/bin/systemctl','enable',SERVICE)
    except Exception:
        run('/usr/bin/systemctl','disable','--now',SERVICE)
        raise


def complete_activation(gid=None):
    can_chown=(os.getuid()==0 and not ROOT_PREFIX)
    atomic_write(MARK,b'enabled\n',0o640,0 if can_chown else None,gid if can_chown else None)
    atomic_write(STATUS,b'{"enabled":true}\n',0o644,0 if can_chown else None,
                 None if ROOT_PREFIX or not can_chown else -1)


def current_service_state():
    if ROOT_PREFIX:
        enabled=ROOT_PREFIX/'etc/systemd/system/multi-user.target.wants'/SERVICE
        return False,enabled.is_symlink()
    def active(property_name):
        result=subprocess.run(['/usr/bin/systemctl','show',SERVICE,
                               f'--property={property_name}','--value'],
                              capture_output=True,text=True,timeout=10,check=False)
        return result.returncode==0 and (result.stdout or '').strip() in ('active','enabled','enabled-runtime')
    return active('ActiveState'),active('UnitFileState')


def restore_service_state(was_active,was_enabled):
    if ROOT_PREFIX:
        operation='enable' if was_enabled else 'disable'
        subprocess.run(['/usr/bin/systemctl','--root',str(ROOT_PREFIX),operation,SERVICE],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        return
    operation='enable' if was_enabled else 'disable'
    subprocess.run(['/usr/bin/systemctl',operation,SERVICE],
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
    operation='restart' if was_active else 'stop'
    subprocess.run(['/usr/bin/systemctl',operation,SERVICE],
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)


def atomic_write(path,data,mode,uid=None,gid=None):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.pending-'+secrets.token_hex(5))
    try:
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
        with os.fdopen(fd,'wb') as out:
            out.write(data);out.flush();os.fsync(out.fileno())
        os.chmod(tmp,mode)
        if uid is not None and gid is not None:
            os.chown(tmp,uid,gid)
        os.replace(tmp,path)
    finally:
        try:tmp.unlink()
        except FileNotFoundError:pass


def ensure_managed_file_paths():
    """Reject aliases/special files before reading or replacing privileged state."""
    for path in (ADMIN,CERT,KEY,MARK,STATUS,ACCOUNT_STATE):
        try:
            st=path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink() or not path.is_file():
            die(f'Archivo administrativo inseguro (se requiere archivo regular sin enlaces): {path}')
        if st.st_nlink != 1:
            die(f'Archivo administrativo inseguro (no se permiten hardlinks): {path}')


def config():
    data={}
    for line in CONF.read_text().splitlines():
        if line.strip().startswith('#') or '=' not in line:continue
        k,v=line.split('=',1)
        data[k.strip()]=v.strip().strip('"').strip("'")
    return data


def yes(prompt):
    return input(prompt+' [s/N]: ').strip().lower() in ('s','si','sí')


def cert_same_key(cert,key):
    try:
        a=subprocess.check_output(['openssl','x509','-in',str(cert),'-pubkey','-noout'],timeout=6)
        b=subprocess.check_output(['openssl','pkey','-in',str(key),'-pubout'],timeout=6,stderr=subprocess.DEVNULL)
    except (OSError,subprocess.CalledProcessError,subprocess.TimeoutExpired):return False
    return a.strip()==b.strip()


def persist_cert(cert,key):
    if not cert_same_key(cert,key):die('La clave privada no corresponde al certificado')
    # Copia atómica; nunca exponer privada al servidor HTTP público.
    gid,uid=ensure_prerequisites()
    for path,target,perm in ((cert,CERT,0o640),(key,KEY,0o640)):
        can_chown=(os.getuid()==0 and not ROOT_PREFIX)
        atomic_write(target,Path(path).read_bytes(),perm,
                     0 if can_chown else None,
                     gid if can_chown else None)


def generate_selfsigned(host):
    print('Certificado local: el navegador solicitará confiar en él la primera vez.')
    if host=='0.0.0.0':
        host=input('IP real que utilizarás en el navegador: ').strip()
    try:ipaddress.IPv4Address(host)
    except ValueError:die('Se necesita una IPv4 válida para el certificado local')
    tempcrt=ROOT/'local-temp.crt';tempkey=ROOT/'local-temp.key'
    try:
        subprocess.run(['openssl','req','-x509','-newkey','rsa:3072','-nodes',
            '-keyout',str(tempkey),'-out',str(tempcrt),'-days','365','-sha256',
            '-subj','/CN='+host,'-addext','subjectAltName=IP:'+host],
            check=True,timeout=90,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        persist_cert(tempcrt,tempkey)
    finally:
        tempcrt.unlink(missing_ok=True);tempkey.unlink(missing_ok=True)
    return ''


def use_existing():
    cert=Path(input('Ruta ABSOLUTA del certificado PEM/cadena: ').strip())
    key=Path(input('Ruta ABSOLUTA de la clave privada PEM: ').strip())
    if not (cert.is_absolute() and key.is_absolute() and cert.is_file() and key.is_file()):
        die('Faltan archivos de certificado/clave')
    persist_cert(cert,key)
    domain=input('Nombre DNS del certificado (vacío si solo tiene IP): ').strip().lower()
    if domain and not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{1,250}[a-z0-9])?',domain):
        die('Nombre DNS inválido')
    return domain


def letsencrypt():
    if ROOT_PREFIX:
        die('Let’s Encrypt no se ejecuta dentro de una raíz aislada; prueba con certificado local')
    print('Let\'s Encrypt HTTP-01: exige dominio real apuntando a este servidor y TCP/80 accesible temporalmente.')
    print('No se abrirán puertos en router/firewall automáticamente.')
    domain=input('Dominio completo (ej.: sep.midominio.es): ').strip().lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{1,250}[a-z0-9])?',domain) or '.' not in domain:
        die('Dominio inválido')
    email=input('Email para notificaciones de Let\'s Encrypt: ').strip()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):die('Email inválido')
    if not shutil.which('certbot'):
        print('Se instalará únicamente certbot desde los repositorios de Debian/Ubuntu.')
        if not yes('¿Permitir instalar certbot?'):die('Cancelado')
        run('apt-get','update',timeout=180)
        run('apt-get','install','-y','--no-install-recommends','certbot',timeout=180)
    # Reusar un certificado ya obtenido sin intentar abrir el puerto 80.
    path=Path('/etc/letsencrypt/live')/domain
    if not (path/'fullchain.pem').exists():
        print('Confirma que 80/TCP está libre, accesible externamente y que el dominio resuelve a este equipo.')
        if not yes('¿Solicitar certificado mediante desafío HTTP-01?'):die('Cancelado')
        with socket.socket() as s:
            try:s.bind(('0.0.0.0',80))
            except OSError:die('TCP/80 ya está ocupado; usa certificado existente/validación DNS externa')
        run('certbot','certonly','--standalone','--preferred-challenges','http',
            '--non-interactive','--agree-tos','--email',email,'-d',domain,timeout=300)
    persist_cert(path/'fullchain.pem',path/'privkey.pem')
    try:run('systemctl','enable','--now','certbot.timer',timeout=15)
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired):
        print('AVISO: confirma la renovación periódica de certbot en este equipo.')
    # Hook renovación: se ejecuta por certbot como root, solo copia el par firmado.
    hook=Path('/etc/letsencrypt/renewal-hooks/deploy/spanish-empire-panel.sh')
    hook.parent.mkdir(parents=True,exist_ok=True)
    hook.write_text('#!/bin/sh\nset -eu\n'
       f'[ "${{RENEWED_LINEAGE:-}}" = "/etc/letsencrypt/live/{domain}" ] || exit 0\n'
       f'install -o root -g sep-admin -m 0640 "${{RENEWED_LINEAGE}}/fullchain.pem" "{CERT}"\n'
       f'install -o root -g sep-admin -m 0640 "${{RENEWED_LINEAGE}}/privkey.pem" "{KEY}"\n'
       f'/usr/bin/systemctl restart {SERVICE}\n')
    hook.chmod(0o700)
    return domain


def snapshot(path):
    path=Path(path)
    if not path.is_file() or path.is_symlink():return None
    st=path.stat()
    return (path.read_bytes(),st.st_mode&0o777,st.st_uid,st.st_gid)


def restore_snapshot(path,value):
    path=Path(path)
    if value is None:
        path.unlink(missing_ok=True)
        return
    data,mode,uid,gid=value
    can_chown=(os.getuid()==0 and not ROOT_PREFIX)
    atomic_write(path,data,mode,uid if can_chown else None,gid if can_chown else None)


def main():
    if os.geteuid() and not ROOT_PREFIX:die('Ejecuta: sudo miura-panel-admin-setup')
    if CONF.is_symlink() or not CONF.is_file():die('Instala Miura Panel antes de activar administración')
    if not shutil.which('openssl'):die('Falta openssl, necesario para TLS')
    if not ROOT_PREFIX and not shutil.which('systemctl'):die('Falta systemctl; se requiere systemd')
    gid,uid=ensure_prerequisites()
    cfg=config()
    mode=cfg.get('DASHBOARD_MODE','normal').strip().lower()
    if mode!='normal':
        die('La administración HTTPS requiere DASHBOARD_MODE=normal. El asistente no cambia el modo demo ni corrige una configuración no válida')
    try:port=int(cfg.get('DASHBOARD_PORT','8766'))+1
    except ValueError:die('DASHBOARD_PORT no es válido')
    if not 1<=port<=65535:die('Puerto de administración inválido')
    bind=cfg.get('DASHBOARD_BIND_IP','127.0.0.1')
    if bind!='0.0.0.0':
        try:ipaddress.IPv4Address(bind)
        except ValueError:die('DASHBOARD_BIND_IP debe ser una IPv4 o 0.0.0.0')
    for directory in (ROOT,STATE,WEB):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            die(f'Ruta administrativa insegura: {directory}')
    ensure_managed_file_paths()
    ROOT.mkdir(parents=True,exist_ok=True)
    if os.getuid()==0 and not ROOT_PREFIX:os.chown(ROOT,0,gid)
    ROOT.chmod(0o750)
    print(f'\nMiura Panel — administración HTTPS en {bind}:{port}')
    print('HTTP seguirá siendo solo lectura; el acceso administrativo exige HTTPS y credenciales.')
    print('El instalador no abrirá puertos del firewall. Con bind local solo se accede desde este equipo.')
    if not MARK.exists():
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as test_socket:
            try:test_socket.bind((bind,port))
            except OSError as exc:die(f'El puerto {bind}:{port} no está disponible: {exc}')
    if MARK.exists() and not yes('Administración existente. ¿Reconfigurar credenciales o certificado?'):
        print('Sin cambios.');return

    tracked=[ADMIN,CERT,KEY,MARK,STATUS,ACCOUNT_STATE]
    before={path:snapshot(path) for path in tracked}
    was_marked=MARK.exists()
    was_active,was_enabled=current_service_state()
    change_credentials = not ADMIN.is_file() or yes('¿Cambiar correo o contraseña? (N conserva las actuales)')
    try:
        if not change_credentials:
            auth=json.loads(ADMIN.read_text())
        else:
            username=input('Correo electrónico del administrador: ').strip().lower()
            again_email=input('Repite el correo electrónico: ').strip().lower()
            if username != again_email:die('Los correos electrónicos no coinciden')
            if len(username)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',username):die('Introduce un correo electrónico válido')
            password=getpass.getpass('Nueva contraseña (mínimo 12 caracteres): ')
            again=getpass.getpass('Repite la contraseña: ')
            if len(password)<12 or len(password)>128 or password!=again:
                die('Las contraseñas no coinciden o su longitud no es válida')
            salt=secrets.token_hex(24)
            hashed=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),510000).hex()
            old=json.loads(ADMIN.read_text()) if ADMIN.is_file() else {}
            auth={'username':username,'hash':hashed,'salt':salt,'port':port,'domain':old.get('domain','')}
            del password,again
        print('\nCERTIFICADO HTTPS:')
        print('  1) Certificado local automático (acceso local por defecto; el navegador pedirá confiar en él)')
        print('  2) Let’s Encrypt (dominio público y TCP/80 accesible durante la validación)')
        print('  3) Certificado PEM existente')
        if CERT.is_file() and KEY.is_file() and yes('¿Conservar certificado actual?'):
            domain=auth.get('domain','')
        else:
            method=input('Elige 1, 2 o 3 [1]: ').strip() or '1'
            if method=='1':domain=generate_selfsigned(bind)
            elif method=='2':domain=letsencrypt()
            elif method=='3':domain=use_existing()
            else:die('Opción inválida')
        if not cert_same_key(CERT,KEY):die('No se pudo verificar el certificado final')
        auth['domain']=domain;auth['port']=port
        can_chown=(os.getuid()==0 and not ROOT_PREFIX)
        atomic_write(ADMIN,(json.dumps(auth,ensure_ascii=False,indent=2)+'\n').encode(),0o640,
                     0 if can_chown else None,gid if can_chown else None)
        STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
        if can_chown:os.chown(STATE,uid,gid)
        STATE.chmod(0o700)
        if change_credentials or not ACCOUNT_STATE.exists():
            account_state={'email':auth['username'],'hash':auth['hash'],'salt':auth['salt']}
            atomic_write(ACCOUNT_STATE,(json.dumps(account_state)+'\n').encode(),0o600,
                         uid if can_chown else None,gid if can_chown else None)
        if ROOT_PREFIX:
            print('Credenciales y certificado preparados en la raíz aislada; no se ha iniciado ni habilitado systemd.')
            return
        activate_admin(bind,port)
        complete_activation(gid)
        if not ROOT_PREFIX:
            run('/usr/bin/systemctl','restart','server-dashboard-web.service')
        host=domain or (bind if bind!='0.0.0.0' else 'IP-del-servidor')
        print(f'CONFIGURACIÓN HTTPS ACTIVADA: https://{host}:{port}/')
        print('Inicia sesión con el correo y la contraseña configurados.')
        print('Huella SHA256 del certificado (compruébala antes de confiar en él):')
        run('openssl','x509','-noout','-fingerprint','-sha256','-in',str(CERT))
        print('Autoriza solo dispositivos de confianza; Miura no cambia el firewall.')
    except BaseException:
        if not ROOT_PREFIX:
            subprocess.run(['/usr/bin/systemctl','disable','--now',SERVICE],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        for path,value in before.items():
            try:restore_snapshot(path,value)
            except OSError:pass
        restore_service_state(was_active,was_enabled)
        if was_marked and not ROOT_PREFIX:
            subprocess.run(['/usr/bin/systemctl','restart','server-dashboard-web.service'],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        raise

def recover_locally():
    # Recuperación sin SMTP: requiere acceso root local/SSH, nunca por HTTP.
    if os.geteuid():die('Se requiere sudo para recuperar la cuenta')
    if not ADMIN.is_file() or not MARK.exists():die('No existe cuenta administradora activa')
    gid,uid=ensure_prerequisites()
    state_account=STATE/'account.json'
    old=json.loads(ADMIN.read_text(encoding='utf-8'))
    try:old_account=json.loads(state_account.read_text(encoding='utf-8'))
    except (OSError,ValueError):old_account={'email':old['username'],'salt':old['salt'],'hash':old['hash']}
    current=old_account.get('email','')
    print('Recuperación local SEP: no cambia el certificado ni el puerto')
    email=input(f'Correo electrónico [{current}]: ').strip().lower() or current
    again_email=input('Repite el correo electrónico: ').strip().lower()
    if email!=again_email:die('Los correos electrónicos no coinciden')
    print('Correo introducido: '+email)
    if not yes('¿Confirmas que es correcto?'):die('Correo no confirmado')
    if len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):die('Correo inválido')
    password=getpass.getpass('Nueva contraseña (mínimo 12 caracteres): ')
    check=getpass.getpass('Repite la contraseña: ')
    if len(password)<12 or len(password)>128 or password!=check:die('Contraseña inválida')
    salt=secrets.token_hex(24)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),510000).hex()
    # Mantener en sincronía el respaldo de root y la cuenta efectiva.
    old.update(username=email,salt=salt,hash=digest)
    tmp=ADMIN.with_name('admin.json.recover-tmp')
    tmp.write_text(json.dumps(old,ensure_ascii=False,indent=2)+'\n')
    atomic_write(tmp,(json.dumps(old,ensure_ascii=False,indent=2)+'\n').encode(),0o640,
                 None if ROOT_PREFIX else 0,None if ROOT_PREFIX else gid)
    st={'email':email,'hash':digest,'salt':salt}
    tmp=STATE/'account.json.recover-tmp'
    atomic_write(tmp,(json.dumps(st)+'\n').encode(),0o600,
                 None if ROOT_PREFIX else uid,None if ROOT_PREFIX else gid)
    (STATE/'password-reset.json').unlink(missing_ok=True)
    (STATE/'pending-email-change.json').unlink(missing_ok=True)
    if not ROOT_PREFIX:run('/usr/bin/systemctl','restart',SERVICE)
    print('Credenciales recuperadas. Sesiones anteriores cerradas.')


if __name__=='__main__':
    if len(sys.argv)==2 and sys.argv[1]=='--recover':recover_locally()
    elif len(sys.argv)==1:main()
    else:die('Uso: sudo miura-panel-admin-setup [--recover]')
