# Miura Panel v1.0.39

## Administración HTTPS inicial

- El instalador provisiona el grupo y usuario de sistema `sep-admin`, una unidad HTTPS endurecida y el directorio privado de estado.
- La unidad administrativa se instala deshabilitada. El asistente `sudo miura-panel-admin-setup` configura correo, contraseña y certificado, inicia el servicio, comprueba `/login` por HTTPS y solo entonces activa el inicio automático.
- Las contraseñas se almacenan con PBKDF2; no se conserva texto claro. Los certificados y credenciales tienen permisos de lectura limitados a root y `sep-admin`.
- El HTTP existente continúa en modo de solo lectura. En modo demo, la configuración HTTPS no se activa.
- El asistente revierte los archivos nuevos si el inicio del servicio o la comprobación TLS falla; las reconfiguraciones restauran los datos previos.

## Actualizaciones y rollback

- Las actualizaciones conservan configuración, credenciales, certificado y estado administrativo.
- El backup/rollback incluye la unidad HTTPS. Al volver a una versión sin servicio administrativo, el acceso HTTPS queda deshabilitado y se conservan las credenciales y certificados.
- La unidad aplica `ProtectSystem=strict`, `NoNewPrivileges` y permisos de escritura limitados.
- Las actualizaciones conservan una unidad administrativa local ya existente. El asistente rechaza una unidad que no use `sep-admin` y el perfil de sandbox esperado, sin guardar nuevas credenciales ni habilitarla.

## Verificación

- Pruebas aisladas de instalación limpia, usuario/grupo de servicio, TLS real, login válido e inválido, cookie segura, habilitación tras health-check, fallo transaccional, actualización y rollback.
- Validación con `systemd-analyze verify` y enlaces de arranque creados mediante `systemctl --root` en el prefijo de prueba.
