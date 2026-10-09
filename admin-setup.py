#!/usr/bin/env python3
"""Activación/reconfiguración LOCAL del modo administración HTTPS de SEP.

Sin valores por defecto de contraseña. No abre firewall ni modifica el router.
"""
import getpass
import grp
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT=Path('/etc/spanish-empire-panel')
STATE=Path('/var/lib/spanish-empire-panel-admin')
CONF=Path('/etc/server-dashboard.conf')
MARK=ROOT/'admin-enabled'
ADMIN=ROOT/'admin.json'
CERT=ROOT/'admin.crt'
KEY=ROOT/'admin.key'
SERVICE='server-dashboard-admin.service'


def die(s):
    raise SystemExit('ERROR: '+s)


def run(*cmd,timeout=90):
    subprocess.run(cmd,check=True,timeout=timeout)


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
    for path,target,perm in ((cert,CERT,0o640),(key,KEY,0o640)):
        tmp=target.with_name(target.name+'.pending')
        shutil.copyfile(path,tmp)
        os.chown(tmp,0,grp.getgrnam('sep-admin').gr_gid)
        os.chmod(tmp,perm)
        os.replace(tmp,target)


def generate_selfsigned(host):
    print('Certificado local: el navegador solicitará confiar en él la primera vez.')
    if host=='0.0.0.0':
        host=input('IP real que utilizarás en el navegador: ').strip()
    try:ipaddress.IPv4Address(host)
    except ValueError:die('Se necesita una IPv4 válida para el certificado local')
    tempcrt=ROOT/'local-temp.crt';tempkey=ROOT/'local-temp.key'
    try:
        run('openssl','req','-x509','-newkey','rsa:3072','-nodes',
            '-keyout',str(tempkey),'-out',str(tempcrt),'-days','365','-sha256',
            '-subj','/CN='+host,'-addext','subjectAltName=IP:'+host)
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


def main():
    if os.geteuid():die('Ejecuta: sudo miura-panel-admin-setup')
    if not CONF.exists():die('Instala SEP antes de activar administración')
    if not shutil.which('openssl'):die('Falta openssl, necesario para TLS')
    if not shutil.which('sudo') or not shutil.which('ssh'):
        die('Faltan sudo u openssh-client')
    cfg=config();port=int(cfg.get('DASHBOARD_PORT','8766'))+1
    if port>65535:die('Puerto de administración inválido')
    bind=cfg.get('DASHBOARD_BIND_IP','127.0.0.1')
    ROOT.mkdir(parents=True,exist_ok=True)
    gid=grp.getgrnam('sep-admin').gr_gid
    os.chown(ROOT,0,gid);ROOT.chmod(0o750)
    print(f'\nMiura Panel — administración HTTPS en {bind}:{port}')
    print('Las funciones administrativas SOLO funcionarán por HTTPS y con sesión.')
    print('El panel HTTP existente continuará en modo lectura.')
    print('No se abrirán puertos en el firewall ni se cambiará SSH.')
    if not MARK.exists():
        # No activar HTTPS ni bloquear escrituras HTTP si el puerto ya se usa.
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as test_socket:
            try:test_socket.bind((bind,port))
            except OSError as exc:die(f'El puerto {bind}:{port} no está disponible: {exc}')
    if MARK.exists() and not yes('Administración existente. ¿Reconfigurar credenciales o certificado?'):
        print('Sin cambios.');return
    change_credentials = not ADMIN.is_file() or yes('¿Cambiar correo o contraseña? (N conserva las actuales)')
    if not change_credentials:
        auth=json.loads(ADMIN.read_text())
    else:
        username=input('Correo electrónico del administrador: ').strip().lower()
        again_email=input('Repite el correo electrónico: ').strip().lower()
        if username != again_email:die('Los correos electrónicos no coinciden')
        print('Correo introducido: '+username)
        if not yes('¿Confirmas que es correcto?'):die('Correo no confirmado')
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
    print('  1) Certificado LOCAL automático (IP privada, requiere confiar en el certificado)')
    print('  2) LET\'S ENCRYPT (dominio propio, debe poder validarse)')
    print('  3) Certificado PROPIO PEM (certificado y clave existentes)')
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
    path=ROOT/'admin.json.new'
    path.write_text(json.dumps(auth,ensure_ascii=False,indent=2)+'\n')
    os.chown(path,0,gid);path.chmod(0o640)
    os.replace(path,ADMIN)
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    account=__import__('pwd').getpwnam('sep-admin')
    os.chown(STATE,account.pw_uid,account.pw_gid)
    os.chmod(STATE,0o700)
    acc_path=STATE/'account.json'
    # Compatibilidad: el gestor web mantiene esta copia privada editable por sep-admin.
    if not acc_path.exists() or change_credentials:
        tmp=STATE/'account.json.pending'
        tmp.write_text(json.dumps({'email':auth['username'],'hash':auth['hash'],'salt':auth['salt']})+'\n')
        os.chown(tmp,account.pw_uid,account.pw_gid)
        tmp.chmod(0o600)
        os.replace(tmp,acc_path)
    was_enabled=MARK.exists()
    MARK.write_text('enabled\n');MARK.chmod(0o644)
    # Indicador público SIN credenciales: el proceso HTTP no puede leer /etc privado.
    status=Path('/var/www/server-dashboard/admin-status.json')
    status.write_text('{"enabled":true}\n');status.chmod(0o644)
    # El panel HTTP pasa a modo lectura; TLS administrativo se activa después.
    try:
        run('systemctl','restart','server-dashboard-web.service')
        run('systemctl','enable','--now',SERVICE)
        run('systemctl','restart',SERVICE)
        run('systemctl','is-active','--quiet',SERVICE)
    except Exception:
        if not was_enabled:
            MARK.unlink(missing_ok=True)
            status.unlink(missing_ok=True)
            run('systemctl','restart','server-dashboard-web.service')
        raise
    host=domain or (bind if bind!='0.0.0.0' else 'IP-del-servidor')
    print(f'CONFIGURACIÓN HTTPS ACTIVADA: https://{host}:{port}/')
    print('El navegador puede solicitar confiar en el certificado local.')
    print('Huella SHA256 del certificado (compruébala en el navegador):')
    run('openssl','x509','-noout','-fingerprint','-sha256','-in',str(CERT))
    print('Autoriza únicamente a dispositivos de confianza: SEP no modifica el firewall.')

def recover_locally():
    # Recuperación sin SMTP: requiere acceso root local/SSH, nunca por HTTP.
    if os.geteuid():die('Se requiere sudo para recuperar la cuenta')
    if not ADMIN.is_file() or not MARK.exists():die('No existe cuenta administradora activa')
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
    gid=grp.getgrnam('sep-admin').gr_gid
    old.update(username=email,salt=salt,hash=digest)
    tmp=ADMIN.with_name('admin.json.recover-tmp')
    tmp.write_text(json.dumps(old,ensure_ascii=False,indent=2)+'\n')
    os.chown(tmp,0,gid);tmp.chmod(0o640);os.replace(tmp,ADMIN)
    st={'email':email,'hash':digest,'salt':salt}
    account=__import__('pwd').getpwnam('sep-admin')
    tmp=STATE/'account.json.recover-tmp'
    tmp.write_text(json.dumps(st)+'\n')
    os.chown(tmp,account.pw_uid,account.pw_gid)
    tmp.chmod(0o600);os.replace(tmp,state_account)
    (STATE/'password-reset.json').unlink(missing_ok=True)
    (STATE/'pending-email-change.json').unlink(missing_ok=True)
    run('systemctl','restart',SERVICE)
    print('Credenciales recuperadas. Sesiones anteriores cerradas.')


if __name__=='__main__':
    if len(sys.argv)==2 and sys.argv[1]=='--recover':recover_locally()
    elif len(sys.argv)==1:main()
    else:die('Uso: sudo miura-panel-admin-setup [--recover]')
