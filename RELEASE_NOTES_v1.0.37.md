# Miura Panel v1.0.37 (candidata)

## Cambios

- Instala `server-dashboard-live.py` como `root:www-data` con permisos `0750`, de modo que el servicio web pueda leerlo sin hacerlo legible para otros usuarios.
- Crea y usa el grupo dedicado `server-dashboard-prefs`; migra el directorio de estado y `preferences/ui.json` conservando el contenido y limitando el acceso a preferencias visuales.
- Mantiene `ProtectSystem=strict` y permite escrituras del servicio HTTP únicamente en `/var/lib/server-dashboard/preferences`. El colector conserva sus escrituras en sus rutas de salida de Miura Panel.
- Permite por HTTP únicamente el guardado de preferencias con origen propio, marcador y esquema validados. Las acciones administrativas siguen requiriendo la ruta HTTPS autenticada.
- Si existe `server-dashboard-admin.service`, añade un drop-in limitado al grupo y directorio de preferencias, sin sustituir la unidad base ni sus controles de autenticación.
- Conserva los permisos y el acceso a preferencias durante actualizaciones y rollback.

## Validación

Las pruebas de instalación, actualización desde estados v1.0.34/v1.0.36, rollback, permisos efectivos y escritura atómica se ejecutan en raíces temporales. La candidata no está publicada; no se modifica Debian16, producción ni la release v1.0.35.
