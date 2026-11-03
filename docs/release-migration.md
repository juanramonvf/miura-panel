# Migración de actualizaciones a GitHub Releases y GitHub Pages

Este documento prepara la transición de `sep.mundopeke.es` sin cambiar el
manifiesto público, DNS, hosting ni configuración de ningún servidor. No
publicar v1.0.35 durante esta preparación.

## Contrato que ya consume v1.0.34

El actualizador incluido en v1.0.34 lee `DASHBOARD_UPDATE_MANIFEST` desde
`/etc/server-dashboard.conf` y acepta un objeto JSON con estos campos:

```json
{
  "version": "1.2.3",
  "installer": "https://github.com/juanramonvf/miura-panel/releases/download/v1.2.3/install-spanish-empire-panel-v1.2.3.sh",
  "sha256": "<64 caracteres hexadecimales minúsculos>"
}
```

`version` debe ser `MAJOR.MINOR.PATCH`; el actualizador solo aplica una versión
mayor que la local. El manifiesto debe ocupar como máximo 64 KiB y el instalador
como máximo 2 MiB. Las URL del manifiesto, el instalador y cualquier redirección
deben usar HTTPS. Antes de ejecutar el instalador, v1.0.34 compara su SHA256 con
el valor del manifiesto y valida la sintaxis Bash. Mantener el instalador por
debajo de 2 MiB es obligatorio mientras haya paneles v1.0.34.

## Secuencia de migración

1. Mantener disponible la URL antigua de `sep.mundopeke.es` con el mismo
   contrato JSON. Durante la transición, su `installer` puede apuntar al asset
   de una GitHub Release inmutable y el hash debe ser el del asset exacto.
   Esto permite que paneles v1.0.34 sigan consultando su URL configurada; no
   requieren conocer GitHub Pages.
2. Publicar el manifiesto de canal para instalaciones nuevas y paneles cuya
   configuración se haya migrado, por ejemplo
   `https://juanramonvf.github.io/miura-panel/updates/stable.json`. Pages debe
   servir exactamente el mismo esquema compatible.
3. Migrar `DASHBOARD_UPDATE_MANIFEST` de cada panel existente mediante un
   procedimiento de administración separado y autorizado. El actualizador
   v1.0.34 no descubre ni cambia por sí mismo su URL de manifiesto.
4. Mantener la URL antigua activa hasta que se haya comprobado que los paneles
   administrados ya usan Pages o que el endpoint antiguo siga redirigiendo el
   feed de manera segura. No cambiar DNS ni retirar el sitio durante esta
   preparación.

La URL de Pages para `stable.json` es un puntero mutable de canal. Los assets
de cada release deben quedar inmutables: no reemplace un tag ni vuelva a subir
un instalador con la misma versión. Habilite la opción **Release immutability**
en la configuración de GitHub del repositorio; se aplica a releases futuras.
El workflow debe comprobar esta configuración al comienzo del job de release
con `GET /repos/juanramonvf/miura-panel/immutable-releases` y continuar solo
si recibe HTTP 200 y JSON con `enabled: true`. Debe fallar cerrado ante 404,
cualquier otro estado, error de red, JSON inválido o `enabled` falso. GitHub
requiere acceso de lectura de administración para este endpoint; el
`GITHUB_TOKEN` estándar del workflow no basta.

Guarde un secreto de entorno, por ejemplo
`IMMUTABILITY_CHECK_TOKEN`, como token fine-grained con acceso solo a este
repositorio y permisos **Administration: read** y **Contents: read**. Use ese
secreto para consultar tanto la política de inmutabilidad como si ya existe
una release o borrador con el tag. No pase el `GITHUB_TOKEN` con
`contents: write` al helper de preflight. Alternativamente, emita un token de
instalación de GitHub App administrado de forma segura con solo esos permisos
de lectura. Ese secreto de lectura también puede autorizar el checkout del tag
y la comprobación de que el borrador creado contiene el conjunto exacto de
assets; el helper solo debe recibir el token de lectura. No otorgue
`contents: write` ni `Administration: write` al token de comprobación y no
reutilice el token de publicación. Si el secreto falta, expiró o carece de
permiso, el release job debe detenerse antes de crear tags/releases. No
imprima el token en logs.

El workflow debe crear una sola release por tag con todos los assets finales
(instalador, suma, manifiesto versionado y notas) antes de marcarla publicada.
Solo después de confirmar la release puede actualizarse `stable.json` en Pages.
La publicación de Pages debe depender de la finalización correcta de la release,
no de un push arbitrario a una rama.

La suma protege al panel contra descargas truncadas o inconsistentes con el
manifiesto recibido. En v1.0.34 el manifiesto se autentica mediante HTTPS, no
con firma criptográfica propia; por tanto, proteja el repositorio, los permisos
del workflow, las reglas de ramas y el entorno de publicación. Limite
`GITHUB_TOKEN` al permiso mínimo por job, use acciones fijadas a SHA completo y
no ejecute el workflow de publicación desde pull requests no confiables.

## Barreras de versión y compatibilidad

- El disparador de release debe aceptar solo un tag `vMAJOR.MINOR.PATCH` recién
  creado. Antes de empaquetar o publicar, valida que tag, `version.json` y notas
  coincidan, y que el commit del tag sea ancestro de `main`. Antes de crear el
  borrador, comprueba que no exista ya una Release o borrador con ese tag; nunca
  sobrescribe una versión publicada.
- El release job debe depender del CI y del paquete validado; no debe poder
  publicar cuando una prueba, auditoría, verificación de hash o prueba demo
  falle.
- Pruebe el instalador autocontenido en raíz temporal: instalación demo,
  actualización desde una fixture v1.0.34 configurada en modo demo, y
  comprobación de que `DASHBOARD_MODE=demo` y los datos ficticios persisten.
- Compruebe que el instalador embebido contiene todos los archivos esperados,
  solo rutas permitidas, sin miembros duplicados o especiales, y que el hash del
  artefacto coincide con el manifiesto generado.
- Rechace una publicación si el instalador supera 2 MiB hasta que el soporte
  para v1.0.34 termine explícitamente. El pipeline tampoco debe alterar ni
  desplegar el manifiesto público durante pruebas de PR o builds candidatos.

## Preparación y activación

Antes de activar el flujo, habilite GitHub Actions y la inmutabilidad de
releases en la configuración del repositorio. Configure GitHub Pages para usar
GitHub Actions y proteja su entorno de despliegue. Revise los permisos de
`contents: write` para el job que publica releases y `pages: write` más
`id-token: write` para el job de Pages. Configure también el secreto de
comprobación de inmutabilidad anterior. No se necesitan credenciales de DNS ni
del hosting antiguo en GitHub Actions.

La automatización se puede probar con workflows de CI, builds y simulaciones en
raíz temporal. No se debe probar el job de publicación con v1.0.35 ni con una
versión ya publicada; la primera activación de una release real requiere un
nuevo tag semántico no utilizado y pasar todas las barreras anteriores.
