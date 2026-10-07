# Configuring Prometheus server using prometheus-operator

This example uses a projected service-account token to scrape Central over
verified HTTPS. The supplied CA ConfigMap is injected by OpenShift for a
Central service using an OpenShift service serving certificate.

## Prerequisites

- RHACS 4.9 or later and a running Prometheus Operator serving `monitoring.coreos.com/v1`.
- Select the namespace containing `service/central-ocp`, serving `/metrics`
  with an OpenShift service serving certificate. Install all resources below
  in that namespace. The injected service CA does not validate Central's
  internal certificate or a certificate issued by a different CA.
- Configure RHACS to authenticate and authorize the service-account identity
  `system:serviceaccount:<namespace>:sample-stackrox-prometheus-server`, using the
  [RHACS authentication and role configuration](../rhacs/README.md#configuring-api-access).
  Projecting a Kubernetes token alone does not grant RHACS API access. On OpenShift,
  configure its OpenShift auth provider and map this identity to the
  **Prometheus Server** role after loading the provided
  [declarative configuration](../rhacs/declarative-configuration-configmap.yaml).
  Its permission set grants only `Administration` `READ_ACCESS`, which Central
  checks for `GET /metrics`; extra permissions would violate least privilege.
  Applying the ConfigMap alone does **not** load it: configure Central to mount
  this ConfigMap as declarative configuration during Central installation (or
  update its installation to add the mount), following the
  [RHACS declarative configuration documentation](https://docs.redhat.com/en/documentation/red_hat_advanced_cluster_security_for_kubernetes/4.9/html/configuring/declarative-configuration-using).
  Apply `rhacs/declarative-configuration-configmap.yaml` in Central's namespace
  (`kubectl -n stackrox apply -f rhacs/declarative-configuration-configmap.yaml`,
  replacing `stackrox` with Central's namespace)
  and configure Central's mount. Verify the role and permission set appear with
  Origin **Declarative** in Access Control before
  mapping the service-account identity to the role. See the product's Central
  installation documentation for your installation method's mount settings.
- Change `spec.storage.volumeClaimTemplate.spec.storageClassName` in
  `prometheus.yaml` from `standard-rwo` to an available storage class. Check that
  the configured UID and group (`65534`) are allowed by your cluster's pod security
  policy/SCC.
- Install `envsubst` (gettext) to render the namespace-qualified scrape target.

## Install and verify

From the repository root, set the namespace explicitly. The scrape target uses
the full service DNS name in the serving certificate; substitute `NAMESPACE`
when creating the additional scrape Secret (Prometheus does not substitute
environment variables in scrape configuration):

```sh
export NAMESPACE=stackrox  # Replace with Central's namespace.
kubectl -n "$NAMESPACE" apply -f prometheus-operator/service-ca-configmap.yaml
kubectl -n "$NAMESPACE" get configmap sample-stackrox-prometheus-service-ca \
  -o jsonpath='{.data.service-ca\.crt}' | grep -q 'BEGIN CERTIFICATE'
kubectl -n "$NAMESPACE" apply -f prometheus-operator/service-account.yaml
envsubst '${NAMESPACE}' < prometheus-operator/additional-scrape-config.yaml |
  kubectl -n "$NAMESPACE" apply -f -
kubectl -n "$NAMESPACE" apply -f prometheus-operator/prometheus.yaml
kubectl -n "$NAMESPACE" apply -f prometheus-operator/service.yaml
```

If injection has not completed yet, stop and retry the ConfigMap check before
starting Prometheus. The ServiceAccount must exist before the operator starts the pods.
The kubelet refreshes the projected token; Prometheus reads it from the mounted
file. Prometheus mounts the injected `service-ca.crt` and uses it to verify
Central's serving certificate, including its `central-ocp.<namespace>.svc`
hostname. Client authentication and server TLS verification are independent.

For other Kubernetes clusters or a Central service without an OpenShift service
serving certificate, before applying the CA ConfigMap replace its injection
annotation with `data.service-ca.crt` containing the PEM CA bundle,
and update the scrape target to the DNS name in Central's serving certificate.
Keep the ConfigMap name and key matching the mounted `ca_file`, or update both
the volume and `ca_file` together. Provide a trusted CA for the certificate
actually presented on port 443; do not disable TLS verification. If Central
does not publish `central-ocp`, select its real service and certificate name.

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
export NAMESPACE=stackrox  # Use the namespace selected above.
export PROMETHEUS_SERVICE=sample-stackrox-prometheus-server
envsubst '${NAMESPACE} ${PROMETHEUS_SERVICE}' < perses/datasource.yaml.tpl |
  kubectl apply -f -
kubectl apply -f perses/dashboard.yaml
```

This needs a Perses installation serving `perses.dev/v1alpha2`. The
[COO example](../cluster-observability-operator/README.md) installs one through
its `UIPlugin`, which is OpenShift only.
