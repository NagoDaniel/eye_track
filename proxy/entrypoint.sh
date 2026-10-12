#!/bin/sh
# Writes the TLS directive for TLS_MODE, then runs Caddy (or `validate`). Fails loudly on bad config.
set -eu

fail() { echo "error: $*" >&2; exit 1; }

: "${DOMAIN:?DOMAIN must be set}"
: "${MAX_UPLOAD_CHUNK_MB:=10}"
: "${MAX_VIDEO_UPLOAD_MB:=2048}"
: "${MAX_ADMIN_REQUEST_MB:=1}"
export MAX_UPLOAD_CHUNK_MB MAX_VIDEO_UPLOAD_MB MAX_ADMIN_REQUEST_MB

case "${TLS_MODE:-}" in
  acme)
    # The contact e-mail is optional: Caddy's automatic HTTPS works without it, it only adds
    # expiry reminders. The example value from .env.example counts as "not set".
    if [ -n "${ACME_EMAIL:-}" ] && [ "${ACME_EMAIL}" != "you@example.org" ]; then
      printf '%s' "${ACME_EMAIL}" | grep -Eq '^[^[:space:]@{}"]+@[^[:space:]@{}"]+$' \
        || fail "ACME_EMAIL is not a valid e-mail address: ${ACME_EMAIL}"
      echo "tls ${ACME_EMAIL}" > /etc/caddy/tls.caddy
    else
      echo "note: ACME_EMAIL is not set; certificates are requested without a contact address" >&2
      echo "# TLS_MODE=acme without a contact e-mail: Caddy's automatic HTTPS" > /etc/caddy/tls.caddy
    fi
    ;;
  internal)
    echo "tls internal" > /etc/caddy/tls.caddy
    ;;
  files)
    [ -n "${TLS_CERT_FILE:-}" ] && [ -n "${TLS_KEY_FILE:-}" ] \
      || fail "TLS_MODE=files requires TLS_CERT_FILE and TLS_KEY_FILE"
    [ -s "${TLS_CERT_FILE}" ] || fail "TLS_CERT_FILE not readable or empty: ${TLS_CERT_FILE}"
    [ -s "${TLS_KEY_FILE}" ] || fail "TLS_KEY_FILE not readable or empty: ${TLS_KEY_FILE}"
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
