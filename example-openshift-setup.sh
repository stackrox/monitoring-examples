#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

for command in oc kubectl envsubst openssl curl jq base64; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done
: "${ROX_API_ENDPOINT:?Set ROX_API_ENDPOINT to the Central URL, including https://}"
: "${ROX_API_TOKEN:?Set ROX_API_TOKEN to a token with Access permissions}"
ROX_API_ENDPOINT=${ROX_API_ENDPOINT%/}
case "$ROX_API_ENDPOINT" in
  https://*) ;;
  *) echo "ROX_API_ENDPOINT must start with https://" >&2; exit 1 ;;
esac
TIMEOUT=${TIMEOUT:-300}
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || { echo "TIMEOUT must be a positive number of seconds" >&2; exit 1; }
if [[ -z "${NAMESPACE:-}" ]]; then
  NAMESPACE=$(oc project -q)
fi
: "${NAMESPACE:?Select the RHACS namespace or set NAMESPACE}"

# COO names the Prometheus service account after the MonitoringStack, and the
# scrape presents a client certificate for that Kubernetes identity.
MONITORING_STACK=sample-rhacs
SERVICE_ACCOUNT="$MONITORING_STACK-prometheus"
# COO publishes a service for this stack's Prometheus beside the shared
# prometheus-operated one, which governs the StatefulSet and selects every
# Prometheus in the namespace. The Perses datasource addresses this stack.
PROMETHEUS_SERVICE="$MONITORING_STACK-prometheus"
CLIENT_SECRET=sample-stackrox-prometheus-tls
SCRAPE_IDENTITY="system:serviceaccount:$NAMESPACE:$SERVICE_ACCOUNT"
ACCESS_CONTROL=rhacs/openshift-platform-access-control.json
# The same role that authorizes OpenShift platform monitoring.
SCRAPE_ROLE=$(jq -er '.role.name' "$ACCESS_CONTROL")
export NAMESPACE SERVICE_ACCOUNT CLIENT_SECRET PROMETHEUS_SERVICE TIMEOUT

# X.509 limits a common name to 64 characters, and it carries the full identity.
if (( ${#SCRAPE_IDENTITY} > 64 )); then
  echo "The identity $SCRAPE_IDENTITY is ${#SCRAPE_IDENTITY} characters; a common name allows 64." >&2
  echo "Install RHACS in a namespace of at most $((64 - ${#SCRAPE_IDENTITY} + ${#NAMESPACE})) characters." >&2
  exit 1
fi

# A condition wait only works once the named object exists.
wait_for_resource() {
  local resource=$1 deadline=$((SECONDS + TIMEOUT)) found
  shift
  while true; do
    found=$(oc "$@" get "$resource" --ignore-not-found -o name)
    [[ -z "$found" ]] || return 0
    if (( SECONDS >= deadline )); then
      echo "Timed out waiting for $resource to be created" >&2
      return 1
    fi
    sleep 2
  done
}

# Central reports why a call failed in the response body, which --fail discards.
CURL_FAIL=--fail-with-body
curl --help all 2>/dev/null | grep -q -- '--fail-with-body' || CURL_FAIL=--fail

rox_api() {
  local method=$1 path=$2
  shift 2
  curl "$CURL_FAIL" --silent --show-error -k -X "$method" "$ROX_API_ENDPOINT$path" \
    -H "Authorization: Bearer $ROX_API_TOKEN" -H 'Content-Type: application/json' "$@"
}

# Role mappings belong to one auth provider; leave existing mappings untouched.
ensure_role_mapping() {
  local provider_id=$1 key=$2 value=$3 role=$4 mapping groups mapped
  mapping=$(jq -nc --arg id "$provider_id" --arg key "$key" --arg value "$value" --arg role "$role" \
    '{props: {authProviderId: $id, key: $key, value: $value}, roleName: $role}')
  groups=$(rox_api GET /v1/groups)
  mapped=$(jq -r --argjson mapping "$mapping" \
    'first(.groups[]? | select(.props.authProviderId == $mapping.props.authProviderId and
      .props.key == $mapping.props.key and .props.value == $mapping.props.value) | .roleName) // empty' <<< "$groups")
  if [[ -n "$mapped" ]]; then
    # One identity resolves to one mapping, so adding a second grants nothing.
    [[ "$mapped" == "$role" ]] || {
      echo "$value is already mapped to the $mapped role instead of $role." >&2
      echo "Remove that mapping in the RHACS console, or grant $mapped the same access." >&2
      exit 1
    }
    return 0
  fi
  rox_api POST /v1/groups --data-binary "$mapping" >/dev/null
}

# Echo the SHA-256 fingerprint of every certificate in the PEM bundle on stdin.
certificate_fingerprints() {
  local line block=
  while IFS= read -r line; do
    block+="$line"$'\n'
    if [[ "$line" == *-----END\ CERTIFICATE-----* ]]; then
      openssl x509 -noout -fingerprint -sha256 2>/dev/null <<< "$block" | cut -d= -f2
      block=
    fi
  done
}

# Fail before installing anything if Central's service or API credentials are wrong.
oc -n "$NAMESPACE" get service/central >/dev/null
providers=$(rox_api GET /v1/authProviders)

echo "Installing Cluster Observability Operator..."
oc apply -f cluster-observability-operator/subscription.yaml

echo "Waiting for COO CRDs to be established..."
for crd in monitoringstacks.monitoring.rhobs scrapeconfigs.monitoring.rhobs \
  prometheusrules.monitoring.rhobs uiplugins.observability.openshift.io \
  persesdashboards.perses.dev persesdatasources.perses.dev; do
  wait_for_resource "crd/$crd"
  oc wait --for=condition=Established "crd/$crd" --timeout="${TIMEOUT}s"
done

provider_name=$(jq -er '.authProvider.name' "$ACCESS_CONTROL")
echo "Configuring the $provider_name auth provider..."
provider=$(jq -c --arg name "$provider_name" '[.authProviders[]? | select(.name == $name)] |
  if length > 1 then error("Multiple \($name) auth providers") else .[0] end' <<< "$providers")
# The scrape certificate comes from the cluster's signer, so the provider only
# accepts it while it trusts the client CA that the cluster publishes.
client_ca=$(oc -n kube-system get configmap extension-apiserver-authentication \
  -o jsonpath='{.data.client-ca-file}')
[[ -n "$client_ca" ]] || { echo "The cluster publishes no client CA to trust" >&2; exit 1; }
if [[ "$provider" == "null" ]]; then
  # Provision the defaults only when RHACS has not installed the provider.
  bash rhacs/create-openshift-platform-access-control.sh
  providers=$(rox_api GET /v1/authProviders)
  provider=$(jq -cer --arg name "$provider_name" '[.authProviders[]? | select(.name == $name)] |
    if length != 1 then error("Expected one \($name) auth provider") else .[0] end' <<< "$providers")
fi
# Reuse the installed provider without replacing the client CA it trusts.
jq -e '.type == "userpki" and .enabled == true' <<< "$provider" >/dev/null || {
  echo "The existing $provider_name auth provider is not an enabled user certificate provider." >&2
  exit 1
}
# Nothing in a rejected scrape says which CA the provider trusts, so compare
# the bundles here instead of leaving a silent authentication failure behind.
trusted=$(certificate_fingerprints <<< "$(jq -r '.config.keys // ""' <<< "$provider")")
published=$(certificate_fingerprints <<< "$client_ca")
if [[ -z "$trusted" ]] || ! grep -qxF -f <(echo "$trusted") <(echo "$published"); then
  echo "The existing $provider_name auth provider trusts none of the certificates in the" >&2
  echo "cluster's client CA, so it would reject the scrape. Update the certificates it" >&2
  echo "trusts from the client-ca-file key of kube-system/extension-apiserver-authentication." >&2
  exit 1
fi
if comm -23 <(sort -u <<< "$published") <(sort -u <<< "$trusted") | grep -q .; then
  echo "Warning: the $provider_name auth provider does not trust every certificate in the" >&2
  echo "cluster's client CA. Certificates from a rotated signer will be rejected." >&2
fi
provider_id=$(jq -er '.id' <<< "$provider")

echo "Requesting a client certificate for $SCRAPE_IDENTITY..."
cluster-observability-operator/request-client-certificate.sh

echo "Mapping $SCRAPE_IDENTITY to the $SCRAPE_ROLE role..."
ensure_role_mapping "$provider_id" name "$SCRAPE_IDENTITY" "$SCRAPE_ROLE"

echo "Installing and configuring a monitoring stack instance..."
oc -n "$NAMESPACE" apply -f cluster-observability-operator/monitoring-stack.yaml
# RHACS 5.0 and later publish central-ocp, whose serving certificate comes from
# the OpenShift service CA: the cluster rotates it and publishes that CA as a
# ConfigMap in every namespace. Earlier versions publish only central, whose
# certificate the StackRox CA in secret/central-tls signs. Both services reach
# the same Central endpoint, so the scrape itself is identical.
if oc -n "$NAMESPACE" get service/central-ocp >/dev/null 2>&1; then
  SCRAPE_SERVICE=central-ocp
  SCRAPE_CA='{configMap: {name: openshift-service-ca.crt, key: service-ca.crt}}'
else
  SCRAPE_SERVICE=central
  SCRAPE_CA='{secret: {name: central-tls, key: ca.pem}}'
fi
export SCRAPE_SERVICE SCRAPE_CA
echo "Scraping $SCRAPE_SERVICE.$NAMESPACE.svc:443..."
# The quoted argument is the envsubst allowlist, so other $... text stays intact.
# shellcheck disable=SC2016
envsubst '${NAMESPACE} ${SCRAPE_SERVICE} ${SCRAPE_CA}' \
  < cluster-observability-operator/scrape-config.yaml.tpl | oc -n "$NAMESPACE" apply -f -
oc -n "$NAMESPACE" apply -f cluster-observability-operator/alert-rules.yaml

echo "Waiting for Prometheus to be ready..."
wait_for_resource "statefulset/prometheus-$MONITORING_STACK" -n "$NAMESPACE"
oc -n "$NAMESPACE" rollout status "statefulset/prometheus-$MONITORING_STACK" --timeout="${TIMEOUT}s"

echo "Installing Perses and configuring the RHACS dashboard..."
oc apply -f perses/ui-plugin.yaml
# shellcheck disable=SC2016
envsubst '${NAMESPACE} ${PROMETHEUS_SERVICE}' < perses/datasource.yaml.tpl |
  oc -n "$NAMESPACE" apply -f -
oc -n "$NAMESPACE" apply -f perses/dashboard.yaml

echo "The monitoring stack scrapes Central as $SCRAPE_IDENTITY with the $SCRAPE_ROLE role."
echo "Rerun this script to renew the client certificate before it expires."
echo "See cluster-observability-operator/README.md for verification."
