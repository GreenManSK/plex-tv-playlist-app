#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/var/www/html"

# 1) .env: create from template if missing
if [ ! -f "$APP_ROOT/.env" ]; then
  if [ -f "$APP_ROOT/.env.template" ] || [ -f "$APP_ROOT/.env.example" ]; then
    cp "$APP_ROOT/.env.template" "$APP_ROOT/.env" 2>/dev/null || true
    cp "$APP_ROOT/.env.example"  "$APP_ROOT/.env" 2>/dev/null || true
    echo "[entrypoint] Created .env from template"
  else
    echo "[entrypoint] No .env template found; creating a minimal one"
    cat > "$APP_ROOT/.env" <<EOF
# Minimal defaults; replace as needed
PLEX_URL=
PLEX_TOKEN=
EOF
  fi
fi

# 2) Make sure .env is readable by the web user
#    www-data inside this image is uid 33, gid 33
chown www-data:www-data "$APP_ROOT/.env" || true
chmod 0644 "$APP_ROOT/.env" || true

# 3) Ensure writeable dirs
mkdir -p "$APP_ROOT/database" "$APP_ROOT/logs"
chown -R www-data:www-data "$APP_ROOT/database" "$APP_ROOT/logs" || true
chmod -R u+rwX,g+rwX "$APP_ROOT/database" "$APP_ROOT/logs" || true

# 4) Apply DB schema migrations (adds any new columns to existing databases)
if [ -x "${PYTHON_EXEC:-/usr/local/bin/python3}" ]; then
  su -s /bin/bash -c "${PYTHON_EXEC:-/usr/local/bin/python3} $APP_ROOT/scripts/db_migrate.py" www-data \
    || echo "[entrypoint] WARN: db_migrate.py failed"
fi

# 5) Hand off to the base image’s default CMD
exec apache2-foreground
