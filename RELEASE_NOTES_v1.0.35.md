# Miura Panel v1.0.35 (candidata)

## Cambios

- Añade un instalador autocontenido y copias de seguridad con rollback automático y manual.
- Conserva el modo demo y la configuración al actualizar desde v1.0.34; migra configuraciones antiguas que no tenían `DASHBOARD_MODE` a modo normal.
- Liga el panel HTTP de solo lectura a `127.0.0.1` por defecto. Durante la actualización, el valor comodín antiguo `DASHBOARD_BIND_IP=0.0.0.0` también se cambia a loopback para evitar servir datos del servidor en una red sin autenticación. Las direcciones configuradas explícitamente que no sean el comodín se conservan.
- No instala paquetes, no abre el firewall y no activa la interfaz administrativa HTTPS, SSH/fleet ni Docker.

## Validación de esta candidata

La auditoría se ejecuta en raíces temporales. Incluye instalación nueva, actualización simulada desde v1.0.34, persistencia de configuración, migración del bind, rollback manual y tras un fallo inducido, validación de unidades con `systemd-analyze verify`, pruebas HTTP locales de demo y bloqueo de escrituras, y verificación SHA256.

No se ejecutó systemd real ni se probó en el servidor de producción. Esta candidata no es una release publicada.

## Artefactos

- `dist/install-spanish-empire-panel-v1.0.35.sh`: instalador autocontenido.
- `dist/install-spanish-empire-panel-v1.0.35.sh.sha256`: SHA256 del instalador.
- `dist/miura-panel-update-manifest.example.json`: ejemplo del manifiesto que consume el actualizador v1.0.34.
- `dist/miura-panel-update-manifest.example.json.sha256`: SHA256 del ejemplo de manifiesto.
