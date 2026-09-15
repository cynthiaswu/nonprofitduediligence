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

  # Peer percentiles. Driven by variables rather than a shell command, because
  # the Railway dashboard has no web terminal -- requiring `railway ssh` would
  # mean this feature was unreachable for anyone not using the CLI.
  if [ -n "$GRANTSIGHT_PEERS_SOI" ] && [ -n "$GRANTSIGHT_PEERS_BMF" ]; then
    if [ ! -f "$DATA/peers.sqlite3" ]; then
      echo "[grantsight] building peer percentile index..."
      ARGS=""
      for u in $GRANTSIGHT_PEERS_SOI; do ARGS="$ARGS --soi $u"; done
      for u in $GRANTSIGHT_PEERS_BMF; do ARGS="$ARGS --bmf $u"; done
      # shellcheck disable=SC2086
      if python -m diligence.peers --build $ARGS; then
        echo "[grantsight] peer index ready."
      else
        echo "[grantsight] peer index build failed; the peer section will not" >&2
        echo "[grantsight] render. It never estimates from a partial population." >&2
      fi
    else
      echo "[grantsight] peer index present; skipping build."
    fi
  fi


}

refresh_xml_if_needed() {
  XML_DB="$DATA/xml_index.sqlite3"
  XML_REFRESH_DAYS="${GRANTSIGHT_XML_REFRESH_DAYS:-7}"
  if [ ! -f "$XML_DB" ] || [ -n "$(find "$XML_DB" -mtime +"$XML_REFRESH_DAYS" 2>/dev/null)" ]; then
    LOCK="$DATA/.xml-index-build.lock"
    if mkdir "$LOCK" 2>/dev/null; then
      echo "[grantsight] refreshing Form 990 XML index..."
      if [ -n "$GRANTSIGHT_XML_INDEX_URLS" ]; then
        # shellcheck disable=SC2086
        if python -m diligence.xml990 --build-index $GRANTSIGHT_XML_INDEX_URLS; then
          echo "[grantsight] XML index ready."
        else
          echo "[grantsight] XML index refresh failed; retaining any previous index." >&2
        fi
      elif python -m diligence.xml990 --build-index; then
        echo "[grantsight] XML index ready."
      else
        echo "[grantsight] XML index refresh failed; retaining any previous index." >&2
      fi
      rmdir "$LOCK" 2>/dev/null || true
    else
      echo "[grantsight] another process is already refreshing the XML index; skipping."
    fi
  else
    echo "[grantsight] XML index present and fresh; skipping build."
  fi
}

xml_refresh_loop() {
  # Check several times a day so the service stays current without requiring
  # a redeploy or a human to run a CLI command. The index itself is refreshed
  # only when it is older than XML_REFRESH_DAYS.
  while true; do
    refresh_xml_if_needed
    sleep "${GRANTSIGHT_XML_CHECK_INTERVAL_SECONDS:-21600}"
  done
}

if [ "${GRANTSIGHT_AUTO_BUILD:-1}" = "1" ]; then
  build_if_needed &
  xml_refresh_loop &
fi

echo "[grantsight] serving on :$PORT"
exec uvicorn app:app --host 0.0.0.0 --port "$PORT" --workers "${WEB_CONCURRENCY:-2}"
