#!/usr/bin/env bash
# TLS checks against a running DocuMind web server (default: the smoke stack).
#   deploy/production/smoke/tls-check.sh [host] [https_port] [http_port] [ca_file]
# Exit code = number of failed checks. Uses only curl and openssl.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
HOST=${1:-localhost} HTTPS=${2:-8443} HTTP=${3:-8080} CA=${4:-$here/work/ca.pem}
failed=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; failed=$((failed + 1)); }
check() { if eval "$2" >/dev/null 2>&1; then pass "$1"; else fail "$1"; fi; }
refuse() { if eval "$2" >/dev/null 2>&1; then fail "$1"; else pass "$1"; fi; }
s_client() { openssl s_client -connect "127.0.0.1:$HTTPS" -servername "$HOST" -CAfile "$CA" "$@" </dev/null 2>&1; }
curl_() { curl -sS --noproxy '*' --resolve "$HOST:$HTTPS:127.0.0.1" --resolve "$HOST:$HTTP:127.0.0.1" --cacert "$CA" "$@"; }

echo "== certificate"
check "chain verifies against the CA" "s_client -verify_return_error | grep -q 'Verify return code: 0 (ok)'"
check "certificate covers $HOST" "s_client -verify_hostname '$HOST' -verify_return_error | grep -q 'Verify return code: 0 (ok)'"
refuse "certificate does not cover other names" "s_client -verify_hostname 'evil.example' -verify_return_error | grep -q 'Verify return code: 0 (ok)'"
end=$(s_client </dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
days=$(( ($(date -d "$end" +%s) - $(date +%s)) / 86400 ))
if [ "$days" -ge 0 ]; then pass "certificate valid for $days more day(s) (until $end)"; else fail "certificate expired ($end)"; fi

echo "== protocols and ciphers"
proto() { grep -Eq "(Protocol *: |New, )$1[ ,]?" ; }
check "TLS 1.3 accepted" "s_client -tls1_3 | proto 'TLSv1\\.3'"
check "TLS 1.2 accepted" "s_client -tls1_2 | proto 'TLSv1\\.2'"
refuse "TLS 1.1 refused" "s_client -tls1_1 -cipher 'DEFAULT@SECLEVEL=0' | proto 'TLSv1\\.1'"
refuse "TLS 1.0 refused" "s_client -tls1 -cipher 'DEFAULT@SECLEVEL=0' | proto 'TLSv1,'"
refuse "CBC/SHA1 cipher (AES128-SHA) refused on TLS 1.2" "s_client -tls1_2 -cipher AES128-SHA | grep -q 'Cipher is AES128-SHA'"
check "forward secrecy (ECDHE) on TLS 1.2" "s_client -tls1_2 | grep -Eq 'Cipher is ECDHE-'"

echo "== HTTP"
check "http redirects to https (301)" "curl_ -o /dev/null -w '%{http_code} %{redirect_url}' http://$HOST:$HTTP/login | grep -q '^301 https://$HOST/login'"
check "ACME challenge path served over plain HTTP (no redirect)" "[ \"\$(curl_ -o /dev/null -w '%{http_code}' http://$HOST:$HTTP/.well-known/acme-challenge/probe)\" = 404 ]"
headers=$(curl_ -sI "https://$HOST:$HTTPS/login")
for h in "strict-transport-security: max-age=63072000; includesubdomains" "x-content-type-options: nosniff" \
         "x-frame-options: deny" "referrer-policy: no-referrer" "content-security-policy: default-src 'self'" \
         "cross-origin-opener-policy: same-origin" "permissions-policy:"; do
  check "header $h" "printf '%s' \"\$headers\" | tr 'A-Z' 'a-z' | grep -qF \"$h\""
done
refuse "server version not disclosed" "printf '%s' \"\$headers\" | grep -iE '^server: nginx/[0-9]'"
check "API health over HTTPS" "curl_ -f https://$HOST:$HTTPS/api/v1/health"
check "metrics endpoint not public (404)" "[ \"\$(curl_ -o /dev/null -w '%{http_code}' https://$HOST:$HTTPS/api/v1/metrics)\" = 404 ]"
echo "== $failed failed"
exit "$failed"
