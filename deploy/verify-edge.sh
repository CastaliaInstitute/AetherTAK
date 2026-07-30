#!/usr/bin/env bash
set -euo pipefail

tak_host="${TAK_HOST:-192.168.86.69}"
tak_api_url="${TAK_API_URL:-https://${tak_host}:8443}"
chirpstack_url="${CHIRPSTACK_URL:-http://${tak_host}:8080}"
tak_admin_pem="${TAK_ADMIN_PEM:-}"
tak_admin_key="${TAK_ADMIN_KEY:-}"
tak_cert_password="${TAK_CERT_PASSWORD:-}"

required_containers=(
  cloudrf-wrapper-db-1
  cloudrf-wrapper-tak-1
  chirpstack-chirpstack-1
  chirpstack-chirpstack-gateway-bridge-1
  chirpstack-chirpstack-gateway-bridge-basicstation-1
  chirpstack-chirpstack-rest-api-1
  chirpstack-mosquitto-1
  chirpstack-postgres-1
  chirpstack-redis-1
)

for container in "${required_containers[@]}"; do
  state="$(docker inspect --format '{{.State.Status}}' "$container")"
  if [[ "$state" != "running" ]]; then
    echo "FAIL: $container is $state" >&2
    exit 1
  fi
  echo "PASS: $container is running"
done

chirpstack_status="$(
  curl --fail --silent --show-error \
    --output /dev/null \
    --write-out '%{http_code}' \
    "$chirpstack_url/"
)"
[[ "$chirpstack_status" == "200" ]]
echo "PASS: ChirpStack UI returned HTTP 200"

if [[ -z "$tak_admin_pem" || -z "$tak_admin_key" ]]; then
  echo "FAIL: set TAK_ADMIN_PEM and TAK_ADMIN_KEY" >&2
  exit 1
fi

tak_version="$(
  curl --fail --silent --show-error --insecure \
    --cert "$tak_admin_pem" \
    --key "$tak_admin_key" \
    --pass "$tak_cert_password" \
    "${tak_api_url}/Marti/api/version"
)"

[[ "$tak_version" == *"RELEASE"* ]]
echo "PASS: AetherTAK API reported $tak_version"
echo "AetherTAK edge verification complete"
