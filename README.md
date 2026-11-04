# Miura Panel

Miura Panel es una herramienta gratuita y de código abierto para consultar y administrar servidores propios.

**Alcance de esta candidata:** el instalador activa únicamente el panel web de solo lectura y el recolector. Los módulos de administración HTTPS, SSH/fleet y Docker permanecen en el código fuente, pero esta candidata no configura sus servicios ni privilegios.

## Funciones

- Panel de estado del servidor.
- Administración autenticada mediante HTTPS.
- Gestión de equipos remotos mediante SSH restringido.
- Funciones de supervisión y administración de Docker.
- Sistema de actualizaciones versionadas.

## Miura Panel v1.0.36 (candidata)

La candidata instala el panel de solo lectura en modo normal o demo. El instalador incluido puede ejecutarse sin red desde este árbol de fuentes. `build-installer.sh` crea un instalador shell autocontenido en `dist/`, junto con su suma SHA256.

Para ejecutar el instalador autocontenido desde `dist/`, verifica y ejecuta `install-spanish-empire-panel-v1.0.36.sh`:

```sh
(cd dist && sha256sum -c install-spanish-empire-panel-v1.0.36.sh.sha256)
sudo ./dist/install-spanish-empire-panel-v1.0.36.sh --demo
```

```sh
sudo ./install-spanish-empire-panel.sh --normal
sudo ./install-spanish-empire-panel.sh --demo
sudo ./install-spanish-empire-panel.sh --upgrade
sudo ./install-spanish-empire-panel.sh --rollback
```

`--upgrade` conserva `DASHBOARD_MODE` y el resto de `/etc/server-dashboard.conf`. Las copias anteriores se guardan bajo `/var/backups/server-dashboard/`. La configuración y el modo demo persisten aunque se actualice el código.

Requisitos del destino: Debian o Ubuntu con systemd, `/usr/bin/python3` y el módulo del sistema `psutil` (`python3-psutil`). El instalador comprueba estos requisitos antes de cambiar el sistema; no descarga paquetes ni abre puertos de firewall. El panel HTTP de lectura se sirve en el puerto 8766 y, por defecto, solo acepta conexiones locales (`127.0.0.1`). En una actualización, el instalador conserva el resto de la configuración y cambia el antiguo valor comodín `DASHBOARD_BIND_IP=0.0.0.0` a loopback, porque el panel HTTP no requiere autenticación y muestra datos del servidor. Para permitir acceso desde otra interfaz, configura explícitamente una dirección adecuada y protege el acceso en la red.

Para validar el instalador sin tocar el sistema anfitrión, define `MIURA_ROOT_PREFIX` con una ruta temporal. Ese modo instala archivos y unidades en la raíz indicada, pero no ejecuta systemd.

## Licencia

Código fuente: GNU General Public License v3.0. Consulta [LICENSE](LICENSE).

El nombre y el logotipo Miura tienen condiciones de uso independientes. Consulta [TRADEMARKS.md](TRADEMARKS.md).

## Seguridad

No publiques contraseñas, claves SSH, tokens ni archivos de configuración personales.

## Colaboraciones

Las mejoras y correcciones podrán proponerse mediante Pull Requests una vez abierto el repositorio oficial.
