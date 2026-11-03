#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
OUT_DIR="${MIURA_OUTPUT_DIR:-$ROOT/dist}"
VERSION="$(python3 - "$ROOT/version.json" <<'PY'
import json, re, sys
try:
    value=json.loads(open(sys.argv[1], encoding='utf-8').read())['version']
except Exception as exc:
    raise SystemExit('version.json inválido: '+str(exc))
if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
    raise SystemExit('versión inválida en version.json')
print(value)
PY
)"
OUT="$OUT_DIR/install-spanish-empire-panel-v${VERSION}.sh"
TEMP_DIR="$(mktemp -d -t miura-panel-build.XXXXXX)"
trap 'rm -rf "$TEMP_DIR"' EXIT
mkdir -p "$OUT_DIR" "$TEMP_DIR/source"

FILES=(version.json index.html admin.html admin-login.html admin-reset.html sep-logo.png
  collect.py live.py admin-web.py admin-setup.py admin-fleet.py fleet.py fleet-docker.py
  fleet-receiver.py docker-action.py docker-updates.py update.py)
for file in "${FILES[@]}"; do
  [[ -f "$ROOT/$file" ]] || { echo "ERROR: falta $file" >&2; exit 1; }
  install -m 0644 "$ROOT/$file" "$TEMP_DIR/source/$file"
done

# Stable ordering/timestamps and gzip -n make the source archive reproducible.
# The installer verifies the downloaded script's
# SHA256 in update.py before executing it.
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner \
  -cf - -C "$TEMP_DIR" source | gzip -n > "$TEMP_DIR/source.tar.gz"
{
  cat "$ROOT/install-spanish-empire-panel.sh"
  printf '\n__MIURA_PANEL_SOURCE_ARCHIVE_BELOW__\n'
  base64 -w 76 "$TEMP_DIR/source.tar.gz"
} > "$OUT"
chmod 0755 "$OUT"
(cd "$OUT_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
printf 'Built %s\n' "$OUT"
cat "$OUT.sha256"
