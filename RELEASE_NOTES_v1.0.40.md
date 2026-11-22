# Miura Panel v1.0.40

## Cambios

- Migra de forma segura la pertenencia heredada de `sep-admin` al grupo `server-dashboard`: elimina únicamente esa pertenencia y conserva los demás miembros. La actualización guarda el dato necesario para restaurarla durante rollback.
- El asistente HTTPS reconoce y migra solo la unidad administrada y los overrides heredados aprobados. Guarda una copia restringida antes de modificar systemd y restaura la unidad y los overrides si falla la activación.
- Rechaza configuraciones systemd desconocidas sin sobrescribirlas.
- Elimina la condición circular de primera activación: el servicio ya no depende del marcador `admin-enabled` para arrancar; el asistente crea el marcador después de verificar TLS.
- El login devuelve a `/configuracion`, `/admin` o `/docker-ssh` cuando la sesión se solicitó desde esas páginas. Solo acepta rutas locales conocidas.
- Muestra la cuenta autenticada y los accesos a Configuración y cierre de sesión en la interfaz HTTPS. HTTP continúa siendo de solo lectura.

## Verificación

- La batería local completa, ejecutada con el mismo runner de CI sobre el paquete generado, terminó con 85 pruebas aprobadas, 11 omitidas y 0 fallidas.
- GitHub Actions en el commit de `main` resultante de la PR #8 completó correctamente las pruebas, el empaquetado, SHA256, payload, modo demo y CodeQL.
- Se comprobó en un entorno aislado la compatibilidad con la configuración original de OMV7 v1.0.38. La prueba usó la unidad administrativa y su override `miura-preferences.conf` literales y verificó la migración, la primera activación HTTPS y la restauración de la unidad y los overrides ante un fallo inducido.
- La integración HTTPS con systemd real en Debian16 también se superó en la validación previa.
- El empaquetado local reconstruyó el instalador byte por byte igual a `dist/` y validó el payload, el modo demo y las unidades systemd. Los sidecars SHA256 están verificados. El SHA256 del instalador es `adaaf2d1884e20d2221d882a22061eeb91a7e343e79fe7e18f68f1bb45ee4c98`.

## Estado de publicación

La versión v1.0.40 todavía no se ha publicado. El manifiesto final de release se genera durante el workflow y apunta al asset inmutable de GitHub Releases; el archivo `.example.json` de `dist/` es una plantilla de la rama candidata y no debe publicarse como manifiesto final.
