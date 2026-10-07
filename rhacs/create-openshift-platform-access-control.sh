#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

for command in oc curl jq; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done
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

ACCESS_CONTROL=rhacs/openshift-platform-access-control.json
CURL_FAIL=--fail-with-body
curl --help all 2>/dev/null | grep -q -- '--fail-with-body' || CURL_FAIL=--fail

rox_api() {
  local method=$1 path=$2
  shift 2
  curl "$CURL_FAIL" --silent --show-error "${CURL_TLS[@]}" -X "$method" "$ROX_API_ENDPOINT$path" \
    -H "Authorization: Bearer $ROX_API_TOKEN" -H 'Content-Type: application/json' "$@"
}

# Reuse any objects RHACS has already installed rather than modifying them.
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

ensure_role_mapping() {
  local provider_id=$1 key=$2 value=$3 role=$4 mapping groups mapped
  mapping=$(jq -nc --arg id "$provider_id" --arg key "$key" --arg value "$value" --arg role "$role" \
    '{props: {authProviderId: $id, key: $key, value: $value}, roleName: $role}')
  groups=$(rox_api GET /v1/groups)
  mapped=$(jq -r --argjson mapping "$mapping" \
    'first(.groups[]? | select(.props.authProviderId == $mapping.props.authProviderId and
      .props.key == $mapping.props.key and .props.value == $mapping.props.value) | .roleName) // empty' <<< "$groups")
  if [[ -n "$mapped" ]]; then
    [[ "$mapped" == "$role" ]] || {
      echo "$value is already mapped to the $mapped role instead of $role." >&2
      exit 1
    }
    return 0
  fi
  rox_api POST /v1/groups --data-binary "$mapping" >/dev/null
}

provider_name=$(jq -er '.authProvider.name' "$ACCESS_CONTROL")
providers=$(rox_api GET /v1/authProviders)
provider=$(jq -c --arg name "$provider_name" '[.authProviders[]? | select(.name == $name)] |
  if length > 1 then error("Multiple \($name) auth providers") else .[0] end' <<< "$providers")
if [[ "$provider" != "null" ]]; then
  echo "The $provider_name auth provider already exists; leaving it and its access control unchanged."
  exit 0
fi

client_ca=$(oc -n kube-system get configmap extension-apiserver-authentication \
  -o jsonpath='{.data.client-ca-file}')
[[ -n "$client_ca" ]] || { echo "The cluster publishes no client CA to trust" >&2; exit 1; }

# RHACS is expected to install these defaults itself (ROX-33524). Until then,
# create the missing objects and map OpenShift platform monitoring to the role.
permission_set_id=$(ensure_object /v1/permissionsets permissionSets "$(jq -c '.permissionSet' "$ACCESS_CONTROL")")
access_scope_id=$(ensure_object /v1/simpleaccessscopes accessScopes "$(jq -c '.accessScope' "$ACCESS_CONTROL")")
ensure_role "$(jq -c --arg permissionSetId "$permission_set_id" --arg accessScopeId "$access_scope_id" \
  '.role + {$permissionSetId, $accessScopeId}' "$ACCESS_CONTROL")"
provider=$(rox_api POST /v1/authProviders --data-binary "$(jq -c \
  --arg keys "$client_ca" --arg endpoint "$ROX_API_ENDPOINT" \
  '.authProvider + {config: {keys: $keys}, uiEndpoint: $endpoint}' "$ACCESS_CONTROL")")
ensure_role_mapping "$(jq -er '.id' <<< "$provider")" \
  "$(jq -er '.platformPrometheus.key' "$ACCESS_CONTROL")" \
  "$(jq -er '.platformPrometheus.value' "$ACCESS_CONTROL")" \
  "$(jq -er '.platformPrometheus.role' "$ACCESS_CONTROL")"
echo "Created the $provider_name auth provider and configured OpenShift platform monitoring."
