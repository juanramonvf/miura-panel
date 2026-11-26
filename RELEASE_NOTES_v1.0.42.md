# Miura Panel v1.0.42

## Cambios

- Las instalaciones nuevas usan GitHub Pages como feed de actualizaciones por defecto en el updater y el gestor fleet.
- Las configuraciones existentes mantienen su URL explícita, compatible con el redirect HTTPS del feed legacy.
- La publicación de `stable.json` se automatiza después de releases inmutables mediante un workflow separado que se ejecuta desde la rama predeterminada y conserva las aprobaciones del environment `github-pages`.

## Verificación

- Verificador estricto del workflow de release, tag y commit.
- Validación de inmutabilidad y SHA256 de todos los assets antes de actualizar Pages.
- Selección monotónica de la versión estable más alta.
