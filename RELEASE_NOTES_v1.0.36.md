# Miura Panel v1.0.36 (candidata)

## Cambios

- Acepta durante la actualización el alias heredado `miura-panel-admin-setup -> spanish-empire-panel-admin-setup` únicamente cuando el enlace sea relativo y exacto, el destino sea un archivo regular ejecutable propiedad del usuario esperado y ni el destino ni sus directorios permitan escritura a grupo u otros. Actualiza el destino manteniendo el enlace, para conservar ambos nombres de comando.
- Conserva las protecciones contra enlaces simbólicos o físicos no autorizados en los demás archivos gestionados.
- Guarda los contenidos previos de ambos nombres como archivos regulares en la copia de seguridad; el rollback automático o manual los restaura como archivos ejecutables sin extraer enlaces simbólicos.

## Validación

La validación se realiza exclusivamente en raíces temporales. Incluye actualización desde una instalación con el alias heredado, rollback, enlaces maliciosos y artefactos autocontenidos con SHA256. No se ejecuta el instalador en Debian16 ni en producción.

Esta candidata no está publicada. No modifica la release ni la etiqueta v1.0.35.
