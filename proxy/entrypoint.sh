#!/bin/sh
# Writes the TLS directive for TLS_MODE, then runs Caddy (or `validate`). Fails loudly on bad config.
set -eu

fail() { echo "error: $*" >&2; exit 1; }

: "${DOMAIN:?DOMAIN must be set}"
: "${MAX_UPLOAD_CHUNK_MB:=10}"
export MAX_UPLOAD_CHUNK_MB

case "${TLS_MODE:-}" in
  acme)
    [ -n "${ACME_EMAIL:-}" ] || fail "TLS_MODE=acme requires ACME_EMAIL"
    echo "tls ${ACME_EMAIL}" > /etc/caddy/tls.caddy
    ;;
  internal)
    echo "tls internal" > /etc/caddy/tls.caddy
    ;;
  files)
    [ -n "${TLS_CERT_FILE:-}" ] && [ -n "${TLS_KEY_FILE:-}" ] \
      || fail "TLS_MODE=files requires TLS_CERT_FILE and TLS_KEY_FILE"
    [ -r "${TLS_CERT_FILE}" ] || fail "TLS_CERT_FILE not readable: ${TLS_CERT_FILE}"
    [ -r "${TLS_KEY_FILE}" ] || fail "TLS_KEY_FILE not readable: ${TLS_KEY_FILE}"
    echo "tls ${TLS_CERT_FILE} ${TLS_KEY_FILE}" > /etc/caddy/tls.caddy
    ;;
  *)
    fail "TLS_MODE must be one of: acme, internal, files (got '${TLS_MODE:-}')"
    ;;
esac

if [ "${1:-}" = "validate" ]; then
  exec caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
fi
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
