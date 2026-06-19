#!/usr/bin/bash
set -e

export NAMESPACE=$(oc project -q)

echo "Installing Cluster Observability Operator..."
oc apply -f cluster-observability-operator/subscription.yaml

echo "Waiting for COO CRDs to be established..."
oc wait --for=condition=Established \
  crd/monitoringstacks.monitoring.rhobs \
  crd/scrapeconfigs.monitoring.rhobs \
  crd/prometheusrules.monitoring.rhobs \
  crd/uiplugins.observability.openshift.io \
  crd/persesdashboards.perses.dev \
  crd/persesdatasources.perses.dev \
  --timeout=300s

echo "Generating user certificates..."
cluster-observability-operator/generate-test-user-certificate.sh
export TLS_CERT=$(awk '{printf "%s\\n", $0}' tls.crt)

echo "Installing and configuring a monitoring stack instance..."
oc apply -f cluster-observability-operator/monitoring-stack.yaml
envsubst < cluster-observability-operator/scrape-config.yaml.tpl | oc apply -f -
oc apply -f cluster-observability-operator/alert-rules.yaml

echo "Waiting for Prometheus to be ready..."
oc rollout status statefulset -l app.kubernetes.io/name=prometheus --timeout=300s

echo "Installing Perses and configuring the RHACS dashboard..."
oc apply -f perses/ui-plugin.yaml
envsubst < perses/datasource.yaml.tpl | oc apply -f -
oc apply -f perses/dashboard.yaml

echo "Declaring a permission set and a role in RHACS..."
# This assumes declarative configuration mount is configured.
oc apply -f rhacs/declarative-configuration-configmap.yaml

echo "Creating a User-Certificate auth-provider..."
curl -k -X POST "$ROX_API_ENDPOINT/v1/authProviders" \
  -H "Authorization: Bearer $ROX_API_TOKEN" \
  --data-raw "$(envsubst < rhacs/auth-provider.json.tpl)"

echo "Now configure the minimum role for the just created Monitoring auth provider."
