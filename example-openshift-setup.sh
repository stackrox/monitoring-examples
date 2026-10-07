#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

for command in oc envsubst curl jq; do
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
TIMEOUT=${TIMEOUT:-300}
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || { echo "TIMEOUT must be a positive number of seconds" >&2; exit 1; }
if [[ -z "${NAMESPACE:-}" ]]; then
  NAMESPACE=$(oc project -q)
fi
: "${NAMESPACE:?Select the RHACS namespace or set NAMESPACE}"

# COO names the Prometheus service account after the MonitoringStack. The
# scrape uses its audience-bound, automatically rotated projected token.
MONITORING_STACK=sample-rhacs
SERVICE_ACCOUNT="$MONITORING_STACK-prometheus"
# COO publishes a service for this stack's Prometheus beside the shared
# prometheus-operated one, which governs the StatefulSet and selects every
# Prometheus in the namespace. The Perses datasource addresses this stack.
PROMETHEUS_SERVICE="$MONITORING_STACK-prometheus"
SCRAPE_IDENTITY="system:serviceaccount:$NAMESPACE:$SERVICE_ACCOUNT"
ACCESS_CONTROL=rhacs/openshift-platform-access-control.json
SCRAPE_ROLE=$(jq -er '.role.name' "$ACCESS_CONTROL")
export NAMESPACE SERVICE_ACCOUNT PROMETHEUS_SERVICE TIMEOUT

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

wait_for_service_ca() {
  local deadline=$((SECONDS + TIMEOUT)) ca
  while true; do
    if ca=$(oc -n "$NAMESPACE" get configmap/openshift-service-ca.crt \
      -o 'jsonpath={.data.service-ca\.crt}' 2>/dev/null) && [[ -n "$ca" ]]; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "Timed out waiting for openshift-service-ca.crt to contain service-ca.crt" >&2
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
  curl "$CURL_FAIL" --silent --show-error "${CURL_TLS[@]}" -X "$method" "$ROX_API_ENDPOINT$path" \
    -H "Authorization: Bearer $ROX_API_TOKEN" -H 'Content-Type: application/json' "$@"
}

# Fail before installing anything if Central's service or API credentials are wrong.
oc -n "$NAMESPACE" get service/central-ocp >/dev/null
rox_api GET /v1/auth/m2m >/dev/null

echo "Installing Cluster Observability Operator..."
oc apply -f cluster-observability-operator/subscription.yaml

echo "Waiting for COO CRDs to be established..."
for crd in monitoringstacks.monitoring.rhobs prometheuses.monitoring.rhobs \
  scrapeconfigs.monitoring.rhobs \
  prometheusrules.monitoring.rhobs uiplugins.observability.openshift.io \
  persesdashboards.perses.dev persesdatasources.perses.dev; do
  wait_for_resource "crd/$crd"
  oc wait --for=condition=Established "crd/$crd" --timeout="${TIMEOUT}s"
done

echo "Configuring RHACS M2M access for $SCRAPE_IDENTITY..."
bash rhacs/configure-m2m-metrics-access.sh

echo "Installing and configuring a monitoring stack instance..."
oc -n "$NAMESPACE" apply -f cluster-observability-operator/service-ca-configmap.yaml
wait_for_service_ca
oc -n "$NAMESPACE" apply -f cluster-observability-operator/monitoring-stack.yaml
wait_for_resource "prometheus.monitoring.rhobs/$MONITORING_STACK" -n "$NAMESPACE"
# The operator owns the Prometheus resource, but not these three pod/scrape
# fields. SSA preserves its other fields and refuses ownership conflicts.
# shellcheck disable=SC2016
envsubst '${NAMESPACE}' < cluster-observability-operator/prometheus-m2m.yaml.tpl |
  oc apply --server-side --field-manager=rhacs-m2m-example -f -
SCRAPE_SERVICE=central-ocp
SCRAPE_CA='{configMap: {name: openshift-service-ca.crt, key: service-ca.crt}}'
export SCRAPE_SERVICE SCRAPE_CA
echo "Scraping $SCRAPE_SERVICE.$NAMESPACE.svc:443..."
# The quoted argument is the envsubst allowlist, so other $... text stays intact.
# shellcheck disable=SC2016
envsubst '${NAMESPACE} ${SCRAPE_SERVICE} ${SCRAPE_CA}' \
  < cluster-observability-operator/scrape-config.yaml.tpl | oc -n "$NAMESPACE" apply -f -
# A previous certificate-backed setup may have left TLS client credentials on
# this same ScrapeConfig. Do not silently accept an authenticated cert scrape.
oc -n "$NAMESPACE" get scrapeconfig.monitoring.rhobs/sample-stackrox-scrape-config -o json |
  jq -e '.spec.scrapeClass == "rhacs-m2m" and
    (.spec.tlsConfig.cert == null) and (.spec.tlsConfig.keySecret == null)' >/dev/null || {
    echo "The scrape still contains client certificate credentials; remove them before using M2M." >&2
    exit 1
  }
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
echo "The kubelet renews its audience-bound M2M token; no client certificate renewal is needed."
echo "See cluster-observability-operator/README.md for verification."
