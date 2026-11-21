# Miura Panel v1.0.40

## Cambios

- Migra de forma segura la pertenencia heredada de `sep-admin` al grupo `server-dashboard`: elimina únicamente esa pertenencia y conserva los demás miembros. La actualización guarda el dato necesario para restaurarla durante rollback.
- El asistente HTTPS reconoce y migra solo la unidad administrada y los overrides heredados aprobados. Guarda una copia restringida antes de modificar systemd y restaura la unidad y los overrides si falla la activación.
- Rechaza configuraciones systemd desconocidas sin sobrescribirlas.
- Elimina la condición circular de primera activación: el servicio ya no depende del marcador `admin-enabled` para arrancar; el asistente crea el marcador después de verificar TLS.
- El login devuelve a `/configuracion`, `/admin` o `/docker-ssh` cuando la sesión se solicitó desde esas páginas. Solo acepta rutas locales conocidas.
- Muestra la cuenta autenticada y los accesos a Configuración y cierre de sesión en la interfaz HTTPS. HTTP continúa siendo de solo lectura.

## Verificación

- La suite local terminó con 73 pruebas aprobadas, 11 omitidas y 0 fallidas.
- Se comprobó en un entorno aislado la compatibilidad con la configuración original de OMV7 v1.0.38. La prueba usó la unidad administrativa y su override `miura-preferences.conf` literales y verificó la migración, la primera activación HTTPS y la restauración de la unidad y los overrides ante un fallo inducido.
- Se verificaron el empaquetado local del instalador autocontenido y las sumas SHA256 del instalador y del manifiesto. El SHA256 del instalador probado es `adaaf2d1884e20d2221d882a22061eeb91a7e343e79fe7e18f68f1bb45ee4c98`.

## Pendiente de publicación

Quedan pendientes las pruebas de integración con un gestor systemd real y la validación en CI, incluido CodeQL. La versión v1.0.40 todavía no se ha publicado.
