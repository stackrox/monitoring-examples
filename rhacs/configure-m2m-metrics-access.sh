#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

for command in oc curl jq; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done

: "${NAMESPACE:?Set NAMESPACE to the Central namespace}"
: "${SERVICE_ACCOUNT:?Set SERVICE_ACCOUNT to the MonitoringStack Prometheus service account}"
: "${ROX_API_ENDPOINT:?Set ROX_API_ENDPOINT to the Central URL, including https://}"
: "${ROX_API_TOKEN:?Set ROX_API_TOKEN to a token with Access permissions}"

ROX_API_ENDPOINT=${ROX_API_ENDPOINT%/}
case "$ROX_API_ENDPOINT" in
  https://*) ;;
  *) echo "ROX_API_ENDPOINT must start with https://" >&2; exit 1 ;;
esac
CURL_TLS=()
if [[ -n "${ROX_API_CA_FILE:-}" ]]; then
  [[ -f "$ROX_API_CA_FILE" && -r "$ROX_API_CA_FILE" ]] || {
    echo "ROX_API_CA_FILE must be a readable CA bundle file" >&2; exit 1;
  }
  CURL_TLS=(--cacert "$ROX_API_CA_FILE")
fi

M2M_AUDIENCE=central.stackrox.io
ACCESS_CONTROL=rhacs/openshift-platform-access-control.json
SCRAPE_ROLE=$(jq -er '.role.name' "$ACCESS_CONTROL")
identity="system:serviceaccount:$NAMESPACE:$SERVICE_ACCOUNT"

# Kubernetes names from this example contain only letters, digits and '-'.
# Check that no regex metacharacter can enter the RHACS RE2 role mapping.
for name in "$NAMESPACE" "$SERVICE_ACCOUNT"; do
  [[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "Invalid service-account identity: $identity" >&2; exit 1; }
done

subject_pattern="^$identity$"

CURL_FAIL=--fail-with-body
curl --help all 2>/dev/null | grep -q -- '--fail-with-body' || CURL_FAIL=--fail
rox_api() {
  local method=$1 path=$2
  shift 2
  curl "$CURL_FAIL" --silent --show-error "${CURL_TLS[@]}" -X "$method" "$ROX_API_ENDPOINT$path" \
    -H "Authorization: Bearer $ROX_API_TOKEN" -H 'Content-Type: application/json' "$@"
}

ensure_object() {
  local path=$1 collection=$2 object=$3 name listing id
  name=$(jq -er '.name' <<< "$object")
  listing=$(rox_api GET "$path")
  id=$(jq -r --arg name "$name" --arg collection "$collection" \
    'first(.[$collection][]? | select(.name == $name and (.id // "") != "") | .id) // empty' <<< "$listing")
  if [[ -z "$id" ]]; then
    id=$(rox_api POST "$path" --data-binary "$object" | jq -er '.id')
  fi
  echo "$id"
}

ensure_role() {
  local role=$1 name
  name=$(jq -er '.name' <<< "$role")
  if rox_api GET /v1/roles | jq -e --arg name "$name" 'any(.roles[]?; .name == $name)' >/dev/null; then
    return 0
  fi
  rox_api POST "/v1/roles/$(jq -rn --arg name "$name" '$name | @uri')" --data-binary "$role" >/dev/null
}

issuer=$(oc get --raw /.well-known/openid-configuration | jq -er '.issuer | select(startswith("https://"))')
configs=$(rox_api GET /v1/auth/m2m)
config=$(jq -c --arg issuer "$issuer" '[.configs[]? | select(.issuer == $issuer)] |
  if length > 1 then error("Multiple M2M configurations for the Kubernetes issuer") else .[0] end' <<< "$configs")
if [[ "$config" != "null" ]]; then
  jq -e --arg audience "$M2M_AUDIENCE" \
    '.type == "KUBE_SERVICE_ACCOUNT" and .audience == $audience' <<< "$config" >/dev/null || {
      echo "The existing M2M configuration for $issuer is not a Kubernetes service account configuration" >&2
      echo "with audience $M2M_AUDIENCE. Leave its mappings untouched and resolve this in RHACS." >&2
      exit 1
    }
fi

# Provision only the metrics role and its dependencies. The separate platform
# certificate provider script remains opt-in; COO does not need a userpki provider.
permission_set_id=$(ensure_object /v1/permissionsets permissionSets "$(jq -c '.permissionSet' "$ACCESS_CONTROL")")
access_scope_id=$(ensure_object /v1/simpleaccessscopes accessScopes "$(jq -c '.accessScope' "$ACCESS_CONTROL")")
ensure_role "$(jq -c --arg permissionSetId "$permission_set_id" --arg accessScopeId "$access_scope_id" \
  '.role + {$permissionSetId, $accessScopeId}' "$ACCESS_CONTROL")"

mapping=$(jq -nc --arg pattern "$subject_pattern" --arg role "$SCRAPE_ROLE" \
  '{key: "sub", valueExpression: $pattern, role: $role}')
if [[ "$config" == "null" ]]; then
  body=$(jq -nc --arg issuer "$issuer" --arg audience "$M2M_AUDIENCE" --argjson mapping "$mapping" \
    '{config: {type: "KUBE_SERVICE_ACCOUNT", issuer: $issuer, audience: $audience,
      tokenExpirationDuration: "1h", mappings: [$mapping]}}')
  rox_api POST /v1/auth/m2m --data-binary "$body" >/dev/null
  echo "Configured RHACS M2M access for $identity."
  exit 0
fi

mapped=$(jq -r --arg pattern "$subject_pattern" \
  'first(.mappings[]? | select(.key == "sub" and .valueExpression == $pattern) | .role) // empty' <<< "$config")
if [[ -n "$mapped" ]]; then
  [[ "$mapped" == "$SCRAPE_ROLE" ]] || {
    echo "$identity already has an M2M mapping to $mapped rather than $SCRAPE_ROLE." >&2
    exit 1
  }
  echo "Reusing the M2M mapping for $identity."
  exit 0
fi

id=$(jq -er '.id' <<< "$config")
body=$(jq -nc --argjson config "$config" --argjson mapping "$mapping" \
  '{config: ($config | .mappings += [$mapping])}')
rox_api PUT "/v1/auth/m2m/$id" --data-binary "$body" >/dev/null
echo "Added an M2M mapping for $identity without changing the other mappings."
