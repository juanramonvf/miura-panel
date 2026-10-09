#!/usr/bin/env python3
"""Acceso administrativo exclusivamente por HTTPS y con sesión autenticada.

Las operaciones de alto privilegio solo están disponibles mediante unidades
systemd y entradas sudoers con argumentos EXACTOS. Nunca ejecuta shell arbitraria.
"""
import base64
import collections
from concurrent.futures import ThreadPoolExecutor, as_completed
import email.message
import smtplib
import contextlib
from http.cookies import SimpleCookie
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, '/usr/local/sbin')
import importlib.util
spec=importlib.util.spec_from_file_location('sep_live','/usr/local/sbin/server-dashboard-live.py')
live=importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
live.ADMIN_MODE=True
fleet_spec=importlib.util.spec_from_file_location('sep_admin_fleet','/usr/local/sbin/sep-admin-fleet.py')
fleet=importlib.util.module_from_spec(fleet_spec)
fleet_spec.loader.exec_module(fleet)

CONF=Path('/etc/spanish-empire-panel/admin.json')
CONFIG=Path('/etc/server-dashboard.conf')
ROOT=Path('/var/lib/spanish-empire-panel-admin')
KEY=ROOT/'id_ed25519'
KNOWN=ROOT/'known_hosts'
WEB=Path('/var/www/server-dashboard')
ACCOUNT=ROOT/'account.json'
SMTP=ROOT/'smtp.json'
RESET=ROOT/'password-reset.json'
PENDING_EMAIL=ROOT/'pending-email-change.json'
SETTINGS=ROOT/'settings.json'
PREF=Path('/var/lib/server-dashboard/preferences/ui.json')
ROLE=WEB/'fleet-role.json'

config=json.loads(CONF.read_text(encoding='utf-8'))
web_conf=live.load_dashboard_config()
BIND=web_conf.get('DASHBOARD_BIND_IP','127.0.0.1')
PORT=int(config.get('port',int(web_conf.get('DASHBOARD_PORT','8766'))+1))
ALLOWED_DOMAIN=config.get('domain') or ''
SS=collections.defaultdict(list)
sessions={}
scans={}
lock=threading.RLock()
last_job_launch=[0.0]
RESET_RATE=collections.defaultdict(list)
# Solo equipos SSH previamente vinculados y con huella conocida, sin sondeos arbitrarios.
VERSION_SCAN={'state':'idle','items':{},'hosts':[],'at':0.0}



def allowed_origin(headers):
    host=headers.get('Host','')
    origin=headers.get('Origin','')
    try:
        parts=urlsplit('https://'+host)
        if parts.port!=PORT or parts.username or parts.password: return False
        hostname=parts.hostname or ''
        if hostname!=ALLOWED_DOMAIN:
            ip=ipaddress.ip_address(hostname)
            if ip.version!=4 or (BIND!='0.0.0.0' and str(ip)!=BIND):return False
    except (ValueError,TypeError):return False
    return origin=='https://'+host and headers.get('Sec-Fetch-Site','same-origin') in ('same-origin','none')


def secure_hash(password,salt):
    return hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),bytes.fromhex(salt),510000).hex()


def current_account():
    # En actualizaciones se conservan las credenciales ya configuradas en 1.0.13.
    if ACCOUNT.is_file():
        return json.loads(ACCOUNT.read_text(encoding='utf-8'))
    return {'email':config.get('username',''), 'hash':config['hash'], 'salt':config['salt']}


def valid_email(value):
    value=str(value).strip().lower()
    if (len(value)>254 or not re.fullmatch(r"[^@\s]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}",value)
            or '..' in value or '\n' in value or '\r' in value):
        raise ValueError('Dirección de correo electrónico no válida')
    return value


def private_json(path, obj):
    # STATE pertenece exclusivamente al usuario sep-admin, y no se publica por HTTP.
    pending=path.with_name(path.name+'.new-'+secrets.token_hex(6))
    try:
        with pending.open('x',encoding='utf-8') as f:
            os.chmod(pending,0o600)
            json.dump(obj,f,ensure_ascii=False,indent=2)
            f.write('\n')
            f.flush();os.fsync(f.fileno())
        os.replace(pending,path)
    finally:
        pending.unlink(missing_ok=True)


def verify_password(username,password):
    acc=current_account()
    if len(username)>254 or len(password)>512:return False
    got=secure_hash(password,acc['salt'])
    return hmac.compare_digest(got,acc['hash']) and hmac.compare_digest(username.lower().strip(),acc['email'].lower())


def smtp_info():
    if not SMTP.is_file():return None
    return json.loads(SMTP.read_text(encoding='utf-8'))


def mail_send(conf, recipient, subject, text):
    msg=email.message.EmailMessage()
    msg['From']=conf['from']
    msg['To']=valid_email(recipient)
    msg['Subject']=subject
    msg.set_content(text)
    context=ssl.create_default_context()
    if conf['tls']=='ssl':
        client=smtplib.SMTP_SSL(conf['host'],conf['port'],timeout=15,context=context)
    else:
        client=smtplib.SMTP(conf['host'],conf['port'],timeout=15)
    with client as c:
        if conf['tls']=='starttls':
            c.ehlo()
            c.starttls(context=context)
            c.ehlo()
        if conf.get('username'):c.login(conf['username'],conf.get('password',''))
        c.send_message(msg)


def validate_smtp(d):
    host=str(d.get('host','')).strip()
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',host) or host.startswith('-'):
        raise ValueError('Servidor SMTP inválido')
    port=d.get('port')
    if type(port) is not int or not 1<=port<=65535:raise ValueError('Puerto SMTP inválido')
    tls=d.get('tls')
    if tls not in ('ssl','starttls'):raise ValueError('SMTP requiere TLS (SSL o STARTTLS)')
    user=str(d.get('username','')).strip()
    password=str(d.get('password',''))
    if len(user)>254 or len(password)>512 or (user and not password):
        raise ValueError('Credenciales SMTP inválidas')
    return {'host':host,'port':port,'tls':tls,'username':user,'password':password,'from':valid_email(d.get('from',''))}


def reset_link(token):
    host=ALLOWED_DOMAIN or (BIND if BIND!='0.0.0.0' else '')
    if not host:raise ValueError('Falta dirección HTTPS de recuperación')
    return f'https://{host}:{PORT}/reset?token={token}'


def email_change_link(token):
    host=ALLOWED_DOMAIN or (BIND if BIND!='0.0.0.0' else '')
    if not host:raise ValueError('Configura una dirección HTTPS válida para verificar el correo')
    return f'https://{host}:{PORT}/configuracion?email_token={token}'


def update_version_scan():
    # Un máximo de 20 destinos registrados por consulta: SSH con StrictHostKeyChecking.
    # Solo se usan destinos admitidos en targets.json, nunca valores recibidos del navegador.
    try:
        targets=fleet.targets()[:20]
        result={}
        with lock:
            VERSION_SCAN['hosts']=[t['host'] for t in targets]
        def probe(t):
            value,error=fleet.remote_check(t)
            if error:return t['host'], {'status':'error'}
            return t['host'], {'status':'ok','version':value.get('current','')} if value else {'status':'error'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(probe,t) for t in targets]
            for future in as_completed(futures):
                host,value=future.result()
                result[host]=value
                with lock:VERSION_SCAN['items']=dict(result)
    except Exception:
        pass
    finally:
        with lock:
            VERSION_SCAN['state']='done'
            VERSION_SCAN['at']=time.monotonic()


def session_hours():
    try:
        v=json.loads(SETTINGS.read_text(encoding='utf-8')).get('hours',12)
        return v if v in (1,4,12,24,168) else 12
    except (OSError,ValueError,TypeError):return 12


def rate_limit(ip,seconds=900,limit=3):
    now=time.time()
    with lock:
        vals=[v for v in RESET_RATE[ip] if now-v<seconds]
        if len(vals)>=limit:return False
        vals.append(now);RESET_RATE[ip]=vals
    return True


def role():
    try:return json.loads(ROLE.read_text()).get('role')=='principal'
    except (OSError,ValueError):return False


def status_of_unit(unit):
    try:
        p=subprocess.run(['/usr/bin/systemctl','show',unit,'--property=ActiveState,Result'],
                          capture_output=True,text=True,timeout=6)
        return dict(x.split('=',1) for x in p.stdout.splitlines() if '=' in x)
    except (OSError,subprocess.TimeoutExpired):return {}


def sudo_exact(*args):
    p=subprocess.run(['/usr/bin/sudo','-n',*args],capture_output=True,text=True,timeout=15)
    if p.returncode:raise ValueError((p.stderr or p.stdout)[-450:] or 'Acción no autorizada')


class Handler(live.Handler):
    def end_headers(self):
        self.send_header('Strict-Transport-Security','max-age=86400')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Permissions-Policy','camera=(),microphone=(),geolocation=()')
        self.send_header('Content-Security-Policy',"frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def _session(self):
        c=SimpleCookie()
        try:c.load(self.headers.get('Cookie',''))
        except Exception:return None,None
        token=c.get('sep_adm')
        if token is None:return None,None
        val=token.value
        with lock:
            s=sessions.get(val)
            if not s or s['until']<time.time():
                sessions.pop(val,None)
                return None,None
            return val,s

    def _auth_required(self):
        token,session=self._session()
        if not session:
            self.send_json(401,{'ok':False,'error':'Sesión requerida'})
            return None
        return session

    def _same_origin(self,csrf=True):
        if not allowed_origin(self.headers):
            self.send_json(403,{'ok':False,'error':'Origen rechazado'})
            return None
        s=self._auth_required()
        if s is None:return None
        if csrf and not hmac.compare_digest(self.headers.get('X-SEP-CSRF',''),s['csrf']):
            self.send_json(403,{'ok':False,'error':'Token CSRF inválido'})
            return None
        return s

    def _request_body(self,limit=4096):
        if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
            raise ValueError('Se exige JSON')
        n=int(self.headers.get('Content-Length','0'))
        if not 0<n<=limit:raise ValueError('Longitud JSON inválida')
        data=json.loads(self.rfile.read(n))
        if not isinstance(data,dict):raise ValueError('Objeto JSON inválido')
        return data

    def _file(self,name,ctype):
        p=WEB/name
        data=p.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)

    def _saved_servers(self):
        try:
            doc=json.loads(PREF.read_text(encoding='utf-8'))
            entries=doc.get('servers',[])
            return [{'name':str(v.get('label',v.get('host',''))),
                'host':str(v.get('host','')),'webport':v.get('port',8766)}
                for v in entries if isinstance(v,dict)][:100]
        except (OSError,ValueError,TypeError):return []

    def _guest_cookie(self):
        c=SimpleCookie()
        try:c.load(self.headers.get('Cookie',''))
        except Exception:return False
        return bool(c.get('sep_guest') and c['sep_guest'].value=='1')

    def do_GET(self):
        path=self.path.split('?',1)[0]
        token,s=self._session()
        if path=='/login':
            if s:
                self.send_response(303);self.send_header('Location','/')
                self.send_header('Content-Length','0');self.end_headers();return
            return self._file('admin-login.html','text/html; charset=utf-8')
        if path=='/guest':
            self.send_response(303)
            self.send_header('Set-Cookie','sep_guest=1; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000')
            self.send_header('Location','/')
            self.send_header('Content-Length','0');self.end_headers();return
        if path=='/reset':
            return self._file('admin-reset.html','text/html; charset=utf-8')
        if path=='/api/admin/status':
            return self.send_json(200,{'ok':True,'authenticated':bool(s),'account':current_account()['email'] if s else '', 'smtp_enabled':SMTP.is_file()})
        if path in ('/configuracion','/admin','/docker-ssh'):
            if not s:
                login_target='/login?next=%2Fdocker-ssh' if path=='/docker-ssh' else '/login'
                self.send_response(303);self.send_header('Location',login_target)
                self.send_header('Content-Length','0');self.end_headers();return
            return self._file('admin.html','text/html; charset=utf-8')
        if not s:
            # Invitado HTTPS: únicamente métricas y recursos estáticos de monitorización.
            # Ni claves, ni credenciales, ni preferencias administrativas.
            public=path in ('/','/index.html','/api/live','/api/preferences',
                '/data.json','/docker-updates.json','/version.json','/fleet-role.json',
                '/admin-status.json','/panel-update.json','/favicon.ico') or path.startswith('/assets/')
            # El panel HTTPS siempre abre en lectura sin exigir login.
            # Las operaciones privilegiadas y Configuración mantienen sesión y CSRF.
            if not public:
                return self.send_json(401,{'ok':False,'error':'Acceso restringido'})
            if path=='/api/preferences':
                # La lista guardada no incluye secretos; no se permite modificarla.
                pass
            if path in ('/','/index.html'):
                return self._file('index.html','text/html; charset=utf-8')
            return super().do_GET()
        if path=='/api/admin/state':
            try:items=fleet.targets()
            except Exception:items=[]
            pub=KEY.with_suffix('.pub').read_text().strip() if KEY.with_suffix('.pub').is_file() else ''
            return self.send_json(200,{'ok':True,'csrf':s['csrf'],'principal':role(),
                'targets':items,'public_key':pub,'admin_port':PORT,
                'local':status_of_unit('server-dashboard-admin-local-update.service'),
                'account':current_account()['email'],
                'smtp':({k:v for k,v in (smtp_info() or {}).items() if k!='password'}),
                'saved_servers':self._saved_servers(), 'sessions':len(sessions),
                'certificate': {'domain':ALLOWED_DOMAIN,'port':PORT},
                'session_hours':session_hours()})
        if path=='/api/admin/job':
            with lock:
                try:job=json.loads(fleet.JOB.read_text(encoding='utf-8'))
                except (ValueError,OSError):job={'state':'idle'}
                local_unit=status_of_unit('server-dashboard-admin-local-update.service')
                if job.get('state')=='local_pending':
                    try: actual=json.loads((WEB/'version.json').read_text())['version']
                    except (ValueError,OSError,KeyError):actual=''
                    expected=(job.get('local') or {}).get('latest','')
                    service_result=local_unit.get('Result','')
                    inactive=local_unit.get('ActiveState') in ('inactive','failed')
                    if inactive and actual==expected and service_result=='success':
                        job['local'].update(current=actual,state='updated',pending=False)
                        job.update(state='done',phase='finished',finished=int(time.time()),
                                   message='Actualización finalizada correctamente')
                        fleet.add_event(job,'Servidor principal actualizado y verificado',level='ok')
                        fleet.save_history(job)
                    elif inactive and service_result not in ('success',''):
                        job['local']['state']='error'
                        job.update(state='error',phase='finished',finished=int(time.time()),
                                   message='La actualización local ha fallado: '+service_result)
                        fleet.add_event(job,job['message'],level='error')
                        fleet.save_history(job)
                history=fleet.history()
            return self.send_json(200,{'ok':True,'job':job,'history':history,
                'local':local_unit})
        if path=='/api/admin/versions':
            with lock:
                now=time.monotonic()
                if VERSION_SCAN['state']!='running' and (VERSION_SCAN['state']=='idle' or now-VERSION_SCAN['at']>180):
                    VERSION_SCAN.update(state='running',items={},at=now)
                    threading.Thread(target=update_version_scan,daemon=True,name='sep-versions').start()
                result={'state':VERSION_SCAN['state'], 'hosts':list(VERSION_SCAN['hosts']),
                        'items':dict(VERSION_SCAN['items'])}
            return self.send_json(200,{'ok':True,**result})
        if path=='/api/admin/me':
            return self.send_json(200,{'ok':True,'email':current_account()['email']})
        return super().do_GET()

    def do_POST(self):
        path=self.path.split('?',1)[0]
        if path=='/api/admin/forgot':
            if not allowed_origin(self.headers):return self.send_json(403,{'ok':False,'error':'Origen rechazado'})
            generic={'ok':True,'message':'Si existe una cuenta con recuperación configurada, recibirás un correo.'}
            if not rate_limit('forgot:'+self.client_address[0]):return self.send_json(200,generic)
            try:
                email=valid_email(self._request_body(512).get('email',''))
                acc=current_account()
                smtp=smtp_info()
                if smtp and hmac.compare_digest(email,acc['email'].lower()) and '@' in acc['email']:
                    token=secrets.token_urlsafe(40)
                    reset={'hash':hashlib.sha256(token.encode()).hexdigest(),'expires':int(time.time()+1200)}
                    with lock:
                        private_json(RESET,reset)
                    try:
                        mail_send(smtp,email,'Recuperación de contraseña - Miura Panel',
                            'Se ha solicitado cambiar tu contraseña de Miura Panel.\n\n'+reset_link(token)+
                            '\n\nEste enlace caduca en 20 minutos y solo puede utilizarse una vez. Si no lo solicitaste, ignora este mensaje.')
                    except (OSError,smtplib.SMTPException,ValueError):
                        with lock:RESET.unlink(missing_ok=True)
                return self.send_json(200,generic)
            except (ValueError,TypeError,OSError):return self.send_json(200,generic)
        if path=='/api/admin/reset-password':
            if not allowed_origin(self.headers):return self.send_json(403,{'ok':False,'error':'Origen rechazado'})
            if not rate_limit('reset:'+self.client_address[0],900,6):return self.send_json(429,{'ok':False,'error':'Demasiados intentos'})
            try:
                data=self._request_body(1536)
                token=str(data.get('token',''));password=str(data.get('password',''))
                if not 12<=len(password)<=128 or len(token)>256:raise ValueError('Contraseña inválida')
                with lock:
                    r=json.loads(RESET.read_text(encoding='utf-8'))
                    if int(r.get('expires',0))<time.time() or not hmac.compare_digest(r.get('hash',''),hashlib.sha256(token.encode()).hexdigest()):
                        raise ValueError('Enlace caducado o inválido')
                    RESET.unlink(missing_ok=True)
                    acc=current_account();acc['salt']=secrets.token_hex(24)
                    acc['hash']=secure_hash(password,acc['salt'])
                    private_json(ACCOUNT,acc)
                    PENDING_EMAIL.unlink(missing_ok=True)
                    sessions.clear()
                return self.send_json(200,{'ok':True,'message':'Contraseña actualizada. Vuelve a iniciar sesión.'})
            except (OSError,ValueError,TypeError,KeyError):
                return self.send_json(400,{'ok':False,'error':'Enlace inválido, caducado o ya utilizado'})
        if path=='/api/admin/login':
            if not allowed_origin(self.headers):return self.send_json(403,{'ok':False,'error':'Origen inválido'})
            try:data=self._request_body(limit=1536)
            except (ValueError,TypeError) as e:return self.send_json(400,{'ok':False,'error':str(e)})
            key=self.client_address[0]
            with lock:
                SS[key]=[t for t in SS[key] if time.time()-t<300]
                if len(SS[key])>=5:return self.send_json(429,{'ok':False,'error':'Demasiados intentos. Espera 5 minutos.'})
                SS[key].append(time.time())
            if not verify_password(str(data.get('email',data.get('username',''))),str(data.get('password',''))):
                time.sleep(0.3)
                return self.send_json(403,{'ok':False,'error':'Credenciales incorrectas'})
            with lock:
                SS.pop(key,None)
                token=secrets.token_urlsafe(40)
                sessions[token]={'csrf':secrets.token_urlsafe(32),'until':time.time()+session_hours()*3600}
            body=b'{"ok":true}'
            self.send_response(200)
            self.send_header('Set-Cookie',f'sep_adm={token}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age={session_hours()*3600}')
            self.send_header('Set-Cookie','sep_guest=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
            return
        s=self._same_origin()
        if s is None:return
        if path=='/api/admin/logout':
            token,_=self._session()
            with lock:sessions.pop(token,None)
            self.send_response(200)
            self.send_header('Set-Cookie','sep_adm=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            self.send_header('Set-Cookie','sep_guest=1; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000')
            self.send_header('Content-Length','0');self.end_headers();return
        if not path.startswith('/api/admin/'):
            # Autenticar también las antiguas acciones Docker y preferencias.
            live.is_own_origin=lambda headers: allowed_origin(headers) and headers.get('X-SEP-Preferences')=='1'
            return super().do_POST()
        try:
            data=self._request_body()
            action=path.removeprefix('/api/admin/')
            if action=='docker-remote':
                if not role():
                    raise ValueError('Solo el servidor principal puede administrar Docker por SSH')
                host=data.get('host');port=data.get('port')
                known=next((t for t in fleet.targets() if t['host']==host and t['port']==port),None)
                if not known:
                    raise ValueError('El destino no está vinculado por SSH al principal')
                op=data.get('op')
                if op not in ('probe','list','update_one','update_group','update_all','status'):
                    raise ValueError('Acción Docker desconocida')
                if op=='probe':
                    result=fleet.remote_docker_probe(known)
                    return self.send_json(200,{'ok':True,**result})
                req={'action':op}
                if op=='update_one':
                    value=data.get('container')
                    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value):
                        raise ValueError('Nombre de contenedor inválido')
                    req['container']=value
                elif op=='update_group':
                    if data.get('group')!='immich':
                        raise ValueError('Grupo no permitido')
                    req['group']='immich'
                elif op=='status':
                    value=data.get('job_id')
                    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{12}',value):
                        raise ValueError('Trabajo inválido')
                    req['job_id']=value
                if op.startswith('update_'):
                    if data.get('confirm')!='ACTUALIZAR':
                        raise ValueError('Confirmación de actualización obligatoria')
                result=fleet.remote_docker(known,req)
                return self.send_json(200,{'ok':True,**result})
            if action=='account':
                new_email=valid_email(data.get('email',''))
                repeat=str(data.get('email_confirm','')).strip().lower()
                current=str(data.get('current_password',''))
                password=str(data.get('new_password',''))
                password_repeat=str(data.get('new_password_confirm',''))
                old=current_account()
                if not verify_password(old['email'],current):
                    raise ValueError('Contraseña actual incorrecta')
                if password:
                    if not 12<=len(password)<=128 or password!=password_repeat:
                        raise ValueError('La nueva contraseña no coincide o no es válida')
                elif password_repeat:
                    raise ValueError('Repite la nueva contraseña correctamente')
                changed=new_email!=old['email'].lower()
                if changed:
                    if not hmac.compare_digest(new_email,repeat):
                        raise ValueError('Los dos correos no coinciden')
                    if data.get('confirmed_email') is not True:
                        raise ValueError('Confirma el correo mostrado antes de guardarlo')
                    smtp=smtp_info()
                    if smtp:
                        if password:
                            raise ValueError('Por seguridad, cambia el correo y la contraseña por separado')
                        if not rate_limit('email-change:'+self.client_address[0],120,3):
                            raise ValueError('Demasiados intentos; espera unos minutos')
                        token=secrets.token_urlsafe(40)
                        # Si no llega el mensaje, el correo antiguo seguirá vigente.
                        mail_send(smtp,new_email,'Verifica tu nuevo correo - Miura Panel',
                            'Confirma el cambio de dirección de la cuenta SEP. '
                            'Este enlace caduca en 20 minutos y solo funciona una vez.\n\n'+
                            email_change_link(token)+'\n\nSi no has solicitado el cambio, ignora este mensaje.')
                        private_json(PENDING_EMAIL,{'email':new_email,'previous':old['email'],
                            'hash':hashlib.sha256(token.encode()).hexdigest(),
                            'expires':int(time.time()+1200)})
                        result={'message':'Te hemos enviado un enlace para verificar el correo nuevo. Hasta confirmarlo, se conserva el anterior.', 'pending_email':True}
                    else:
                        old['email']=new_email
                        if password:
                            old['salt']=secrets.token_hex(24)
                            old['hash']=secure_hash(password,old['salt'])
                        private_json(ACCOUNT,old)
                        PENDING_EMAIL.unlink(missing_ok=True)
                        RESET.unlink(missing_ok=True)
                        with lock:sessions.clear()
                        result={'message':'Cuenta actualizada. Inicia sesión de nuevo.'}
                else:
                    if not password:raise ValueError('No hay cambios que guardar')
                    old['salt']=secrets.token_hex(24)
                    old['hash']=secure_hash(password,old['salt'])
                    private_json(ACCOUNT,old)
                    RESET.unlink(missing_ok=True)
                    PENDING_EMAIL.unlink(missing_ok=True)
                    with lock:sessions.clear()
                    result={'message':'Contraseña actualizada. Inicia sesión de nuevo.'}
            elif action=='confirm-email':
                token=str(data.get('token',''))
                if len(token)>256 or len(token)<30:raise ValueError('Enlace de verificación inválido')
                with lock:
                    if not PENDING_EMAIL.is_file():raise ValueError('No hay cambios pendientes')
                    pending=json.loads(PENDING_EMAIL.read_text(encoding='utf-8'))
                    if (pending['expires']<int(time.time()) or
                        pending['previous']!=current_account()['email'] or
                        not hmac.compare_digest(pending['hash'],hashlib.sha256(token.encode()).hexdigest())):
                        raise ValueError('Enlace caducado o inválido')
                    account=current_account()
                    account['email']=valid_email(pending['email'])
                    private_json(ACCOUNT,account)
                    PENDING_EMAIL.unlink(missing_ok=True)
                    RESET.unlink(missing_ok=True)
                    sessions.clear()
                result={'message':'Correo verificado y actualizado. Vuelve a iniciar sesión.'}
            elif action=='smtp':
                old=smtp_info() or {}
                if data.get('disable') is True:
                    SMTP.unlink(missing_ok=True)
                    RESET.unlink(missing_ok=True)
                    PENDING_EMAIL.unlink(missing_ok=True)
                    result={'smtp_configured':False}
                else:
                    fields=dict(data)
                    if not fields.get('password') and fields.get('username')==old.get('username'):
                        fields['password']=old.get('password','')
                    conf=validate_smtp(fields)
                    if data.get('send_test') is True:
                        # No guardar una configuración que no pudo entregar el mensaje.
                        mail_send(conf,valid_email(current_account()['email']),
                            'Prueba SMTP - Miura Panel',
                            'Prueba correcta del servidor SMTP para recuperación de cuenta SEP.')
                    private_json(SMTP,conf)
                    result={'smtp_configured':True}
            elif action=='session-hours':
                value=data.get('hours')
                if type(value) is not int or value not in (1,4,12,24,168):
                    raise ValueError('Caducidad de sesión inválida')
                private_json(SETTINGS,{'hours':value})
                result={'session_hours':value}
            elif action=='logout-all':
                with lock:sessions.clear()
                result={'message':'Se han cerrado todas las sesiones'}
            elif action=='primary':
                target=data.get('enable')
                if type(target) is not bool:raise ValueError('enable debe ser booleano')
                sudo_exact('/usr/bin/systemctl','start','--no-block', 'server-dashboard-admin-primary.service' if target else 'server-dashboard-admin-secondary.service')
                result={'started':'role-change'}
            elif action=='key':
                if not KEY.is_file():
                    if KEY.with_suffix('.pub').exists():raise ValueError('Existe clave pública sin privada')
                    cmd=['/usr/bin/ssh-keygen','-t','ed25519','-N','','-f',str(KEY),'-C','sep-web-admin']
                    subprocess.run(cmd,check=True,timeout=12,capture_output=True)
                    KEY.chmod(0o600)
                    KEY.with_suffix('.pub').chmod(0o600)
                result={'public_key':KEY.with_suffix('.pub').read_text().strip()}
            elif action=='scan':
                t=fleet.safe_target({'name':'temporal','host':data.get('host'),'port':data.get('port')})
                p=subprocess.run(['/usr/bin/ssh-keyscan','-T','5','-p',str(t['port']),'-t','ed25519',t['host']],
                    capture_output=True,text=True,timeout=13)
                lines=[x for x in p.stdout.splitlines() if x and not x.startswith('#') and ' ssh-ed25519 ' in x]
                if not lines:raise ValueError('No se ha obtenido clave SSH ed25519 del destino')
                parts=lines[0].split()
                if len(parts)!=3:raise ValueError('Clave SSH inválida')
                raw=base64.b64decode(parts[2],validate=True)
                fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')
                # La comprobación permanece vinculada a esta sesión y a la IP/puerto.
                # No se consume hasta terminar correctamente la autorización.
                with lock:
                    now=time.time()
                    for key,value in list(scans.items()):
                        if value['until']<now:scans.pop(key,None)
                    scans[(s['csrf'],t['host'],t['port'])]={'fingerprint':fingerprint,'line':lines[0],'until':now+300}
                result={'fingerprint':fingerprint,
                    'verify_command':'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub'}
            elif action=='add':
                t=fleet.safe_target({'name':data.get('name'),'host':data.get('host'),'port':data.get('port')})
                scan_key=(s['csrf'],t['host'],t['port'])
                with lock:scanned=scans.get(scan_key)
                if not scanned or scanned['until']<time.time():
                    raise ValueError('La comprobación SSH no existe o ha caducado. Consulta la huella de nuevo.')
                if not hmac.compare_digest(scanned['fingerprint'],str(data.get('fingerprint',''))):
                    raise ValueError('La huella no coincide')
                if not KEY.is_file():raise ValueError('Genera y autoriza primero la clave SEP')
                if KNOWN.exists():
                    existing=KNOWN.read_text()
                else:existing=''
                record=scanned['line']+'\n'
                # No reemplazar silenciosamente una clave anterior: detectar y rechazar host duplicado.
                if any(line.split(' ',1)[0]==record.split(' ',1)[0] for line in existing.splitlines()):
                    if record.strip() not in existing.splitlines():
                        raise ValueError('Ya existe otra huella SSH: verifica antes de cambiar claves')
                else:
                    with KNOWN.open('a') as f:f.write(record)
                    KNOWN.chmod(0o600)
                checked,err=fleet.remote_check(t)
                if err or not checked:raise ValueError('No se ha autorizado la clave en destino: '+(err or 'Sin respuesta'))
                arr=fleet.targets()
                if len(arr)>=100 or any(x['host']==t['host'] and x['port']==t['port'] for x in arr):
                    raise ValueError('Equipo duplicado o lista llena')
                arr.append(t);fleet.write_targets(arr)
                with lock:scans.pop(scan_key,None)  # Solo consumir tras guardar con éxito.
                result={'targets':arr}
            elif action=='remove':
                host=data.get('host');port=data.get('port')
                arr=fleet.targets()
                new=[t for t in arr if not(t['host']==host and t['port']==port)]
                if len(new)==len(arr):raise ValueError('Destino no encontrado')
                fleet.write_targets(new)
                result={'targets':new,'note':'La clave remota no se revoca automáticamente'}
            elif action in ('check','update-all','update-local','update-check'):
                if action=='update-all' and not role():raise ValueError('Activa el modo servidor principal')
                units={'check':'server-dashboard-admin-fleet-check.service',
                       'update-all':'server-dashboard-admin-fleet-update.service',
                       'update-local':'server-dashboard-admin-local-update.service',
                       'update-check':'server-dashboard-update-check.service'}
                with lock:
                    if time.monotonic()-last_job_launch[0]<3:
                        raise ValueError('Ya se ha iniciado otra operación; espera unos segundos')
                    for unit in units.values():
                        if status_of_unit(unit).get('ActiveState') in ('activating','active','reloading'):
                            raise ValueError('Ya existe una comprobación o actualización en curso')
                    sudo_exact('/usr/bin/systemctl','start','--no-block',units[action])
                    # Una comprobación global debe refrescar también el estado usado por la cabecera principal.
                    if action=='check':
                        try:sudo_exact('/usr/bin/systemctl','start','--no-block','server-dashboard-update-check.service')
                        except subprocess.CalledProcessError:pass
                    last_job_launch[0]=time.monotonic()
                result={'started':action}
            else:raise ValueError('Operación desconocida')
            self.send_json(200,{'ok':True,**result})
        except (ValueError,KeyError,AssertionError,subprocess.TimeoutExpired,subprocess.CalledProcessError,OSError,smtplib.SMTPException) as e:
            self.send_json(400,{'ok':False,'error':str(e)[:500]})


if __name__=='__main__':
    # Solo usar el certificado y clave privados suministrados desde setup.
    if BIND=='0.0.0.0':
        print('AVISO: servidor HTTPS administrativo escuchando en todas las interfaces',file=sys.stderr)
    httpd=ThreadingHTTPServer((BIND,PORT),Handler)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version=ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain('/etc/spanish-empire-panel/admin.crt','/etc/spanish-empire-panel/admin.key')
    httpd.socket=ctx.wrap_socket(httpd.socket,server_side=True)
    os.chdir(WEB)
    httpd.serve_forever()
