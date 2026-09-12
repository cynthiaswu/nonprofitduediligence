#!/bin/sh
# Start the server first, build the indexes second.
#
# Railway mounts volumes when the container STARTS, not during the build, and
# not during a pre-deploy command. So the IRS indexes cannot be built at image
# build time -- they would be written into the image layer and then shadowed by
# the volume mount.
#
# Building them in the foreground before starting would also fail: it takes
# about ten minutes, and the platform health check would kill the container
# long before the port opened.
#
# So: bind the port immediately, then build in the background if needed. The
# app is designed for exactly this state. Until the index exists, every brief
# reports exemption status as unverified rather than implying good standing,
# and /healthz reports irs_index_built:false. Nothing silently reads as clean.

set -e

PORT="${PORT:-8000}"
DATA="${GRANTSIGHT_DATA:-/data}"
mkdir -p "$DATA" "$DATA/cache" "$DATA/xml"

build_if_needed() {
  # Rebuild when missing, or when older than the refresh window. The IRS
  # republishes monthly.
  DB="$DATA/irs.sqlite3"
  AGE_DAYS="${GRANTSIGHT_REFRESH_DAYS:-30}"
  if [ -f "$DB" ] && [ -z "$(find "$DB" -mtime +"$AGE_DAYS" 2>/dev/null)" ]; then
    echo "[grantsight] IRS index present and fresh; skipping build."
  else
    echo "[grantsight] building IRS index in background (about 10 minutes)..."
    if python -m diligence.irs_status --build; then
      echo "[grantsight] IRS index ready."
    else
      echo "[grantsight] IRS index build FAILED. Status checks will report" >&2
      echo "[grantsight] 'not verified' until this is resolved." >&2
    fi
  fi

  if [ -n "$GRANTSIGHT_XML_INDEX_URLS" ]; then
    if [ ! -f "$DATA/xml_index.sqlite3" ]; then
      echo "[grantsight] building Form 990 XML index..."
      # shellcheck disable=SC2086
      python -m diligence.xml990 --build-index $GRANTSIGHT_XML_INDEX_URLS \
        && echo "[grantsight] XML index ready." \
        || echo "[grantsight] XML index build failed." >&2
    fi
  fi
}

if [ "${GRANTSIGHT_AUTO_BUILD:-1}" = "1" ]; then
  build_if_needed &
fi

echo "[grantsight] serving on :$PORT"
exec uvicorn app:app --host 0.0.0.0 --port "$PORT" --workers "${WEB_CONCURRENCY:-2}"
