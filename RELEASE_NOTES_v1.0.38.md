# Miura Panel v1.0.38 (candidata)

## Correcciones

- Se evita reconstruir las tarjetas de métricas y discos cuando la respuesta periódica no cambia; los gráficos mantienen un historial de 300 muestras, se actualizan al cambiar las métricas y se refrescan cada 60 segundos en periodos estables.
- Se alinea la versión embebida del comprobador de recarga con `version.json`; las versiones anteriores .36 y .37 podían recargar repetidamente la página por conservar el identificador .35.
- El instalador concede lectura del árbol web, configuración y helpers requeridos a los servicios mediante el grupo dedicado `server-dashboard-read`, sin escritura general ni ampliación de `ProtectSystem=strict`.
- El instalador repara los permisos heredados de `/var/www/server-dashboard`, `/etc/server-dashboard.conf` y helpers usados por `sep-admin`; conserva la escritura limitada de preferencias visuales.
- El rollback reaplica los permisos de lectura y la pertenencia suplementaria requeridos por los servicios.

## Compatibilidad y verificación

- Conserva `DASHBOARD_MODE=demo` y la configuración durante actualizaciones.
- Incluye pruebas de instalación aislada, actualización desde .34/.36, rollback, acceso efectivo como UID de servicio, escritura de preferencias HTTP restringidas y regresión del refresco frontend.
- Candidata de desarrollo. No publicar hasta completar las verificaciones automatizadas y revisión.
