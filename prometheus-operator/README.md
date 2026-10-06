# Configuring Prometheus server using prometheus-operator

This example provides a sample Prometheus custom resource, and an additional scrape configuration file, that tells Prometheus server to use the projected service account token to access RHACS API.

## Prerequisites

- RHACS 4.9 or later and a running Prometheus Operator serving `monitoring.coreos.com/v1`.
- Select the namespace containing the `central` service. All resources below must
  be installed in that namespace.
- Configure RHACS to authenticate and authorize the service-account identity
  `system:serviceaccount:<namespace>:sample-stackrox-prometheus-server`, using the
  [RHACS authentication and role configuration](../rhacs/README.md#configuring-api-access).
  Projecting a Kubernetes token alone does not grant RHACS API access. On OpenShift,
  configure its OpenShift auth provider and map this identity to the
  **Prometheus Server** role after loading the provided declarative configuration.
- Change `spec.storage.volumeClaimTemplate.spec.storageClassName` in
  `prometheus.yaml` from `standard-rwo` to an available storage class. Check that
  the configured UID and group (`65534`) are allowed by your cluster's pod security
  policy/SCC.

## Install and verify

From the repository root, with the desired namespace selected:

```sh
kubectl apply -f prometheus-operator/service-account.yaml
kubectl apply -f prometheus-operator/additional-scrape-config.yaml
kubectl apply -f prometheus-operator/prometheus.yaml
kubectl apply -f prometheus-operator/service.yaml
```

The ServiceAccount must exist before the operator can start the Prometheus pods.
The kubelet refreshes the projected token; Prometheus reads it from the mounted
file. The sample scrape disables server certificate verification; configure
`tls_config.ca_file` and mount the appropriate CA to enable verification.

The operator itself reconciles only `prometheus-operated`, the headless service
that governs the StatefulSet. Its selector matches every Prometheus in the
namespace, so a query sent there reaches whichever instance answers first.
`service.yaml` publishes `sample-stackrox-prometheus-server` alongside it,
selecting this instance by its `operator.prometheus.io/name` pod label.

Run the [Prometheus Operator cluster smoke test](../tests/README.md) to verify
workload readiness, successful authenticated scraping, and fixed RHACS metrics.
Enable [custom metrics](../rhacs/README.md#configuring-metrics-via-api) separately
if using the Perses dashboard. Point its datasource at the service above:

```sh
export NAMESPACE=$(kubectl config view --minify -o jsonpath='{..namespace}')
export PROMETHEUS_SERVICE=sample-stackrox-prometheus-server
envsubst '${NAMESPACE} ${PROMETHEUS_SERVICE}' < perses/datasource.yaml.tpl |
  kubectl apply -f -
kubectl apply -f perses/dashboard.yaml
```

This needs a Perses installation serving `perses.dev/v1alpha2`. The
[COO example](../cluster-observability-operator/README.md) installs one through
its `UIPlugin`, which is OpenShift only.
