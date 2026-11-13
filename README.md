# Miura Panel

Miura Panel presenta información del sistema mediante HTTP de solo lectura y permite configurar el acceso administrativo mediante HTTPS autenticado.

## v1.0.39 candidate

El instalador crea las cuentas y unidades necesarias. HTTP escucha en `127.0.0.1:8766` por defecto. El servicio administrativo HTTPS queda deshabilitado hasta completar la configuración inicial.

Verifica y ejecuta el instalador autocontenido:

```sh
(cd dist && sha256sum -c install-spanish-empire-panel-v1.0.39.sh.sha256)
sudo ./dist/install-spanish-empire-panel-v1.0.39.sh --normal
```

Al terminar, configura el administrador desde la consola local:

```sh
sudo miura-panel-admin-setup
```

El asistente pide el correo y la contraseña, y después configura el certificado. El certificado local es la opción predeterminada; el navegador mostrará una advertencia que requiere aceptar o importar el certificado. Con la configuración inicial, el panel HTTPS está en `https://127.0.0.1:8767/` y solo es accesible desde el servidor. Para permitir acceso desde una red privada, configura `DASHBOARD_BIND_IP` con la dirección LAN del servidor antes de ejecutar el asistente y limita el acceso con tu firewall. Miura Panel no abre puertos.

La unidad administrativa se habilita únicamente después de guardar las credenciales y comprobar que el login HTTPS responde. El instalador no activa administración en modo demo; `DASHBOARD_MODE=demo` se conserva en las actualizaciones. Cambia deliberadamente a modo normal antes de configurar HTTPS.

En instalaciones que ya tienen una unidad administrativa local, el instalador la conserva. Antes de escribir credenciales, el asistente comprueba que la unidad use `sep-admin`, ejecute el helper de Miura y mantenga el sandbox de escritura limitado. Una unidad local incompatible se deja intacta y el asistente se niega a activarla.

```sh
sudo ./install-spanish-empire-panel-v1.0.39.sh --upgrade
sudo ./install-spanish-empire-panel-v1.0.39.sh --rollback
```

Las actualizaciones conservan `/etc/server-dashboard.conf`, las credenciales cifradas mediante hash, los certificados y el estado administrativo. Las copias se guardan bajo `/var/backups/server-dashboard/`. El rollback restaura la unidad de la versión anterior; si esa versión no incluía administración HTTPS, conserva los datos y deja el acceso desactivado.

## Pruebas aisladas

Para instalar en un prefijo temporal sin iniciar servicios del anfitrión:

```sh
MIURA_ROOT_PREFIX=/tmp/miura-test-root bash dist/install-spanish-empire-panel-v1.0.39.sh --normal
```

En ese modo, el instalador usa `systemctl --root` solo para preparar enlaces de arranque bajo el prefijo. No arranca ni modifica el gestor systemd del anfitrión.

Requisitos del destino: Debian o Ubuntu con systemd, Python 3, `python3-psutil`, `openssl`, `groupadd` y `useradd`. El instalador no descarga paquetes ni cambia el firewall. La cuenta de servicio `sep-admin` no tiene shell interactiva; HTTP sigue siendo de solo lectura.

## Licencia y marca

Código fuente: GNU GPL v3. Consulta [LICENSE](LICENSE). El nombre y logotipo Miura tienen condiciones independientes en [TRADEMARKS.md](TRADEMARKS.md).
