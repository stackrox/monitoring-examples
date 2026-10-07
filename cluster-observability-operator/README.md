# Cluster Observability Operator

The **Cluster Observability Operator (COO)** is a Kubernetes operator that simplifies the deployment and management of observability tools on OpenShift and Kubernetes clusters. It automates the installation and configuration of monitoring stack components including Prometheus, Alertmanager, and other observability tools.

The operator provides a streamlined way to set up monitoring infrastructure through Kubernetes-native custom resources, supporting features like multi-tenancy, custom metrics collection, and integration with OpenShift platform capabilities.

## Prerequisites

- An OpenShift cluster with RHACS supporting `KUBE_SERVICE_ACCOUNT` machine
  access configurations, permission to install operators, create the
  cluster-scoped `UIPlugin`, and manage resources in the Central namespace.
- RHACS must publish `service/central-ocp` in that namespace. Central must be
  able to reach the cluster's service-account OIDC discovery document and JWKS.
- Bash, `oc`, `curl`, `jq`, and `envsubst` (gettext).
- The COO release selected by the `stable` subscription must provide
  `MonitoringStack` and `ScrapeConfig` (`monitoring.rhobs/v1alpha1`),
  `PrometheusRule` (`monitoring.rhobs/v1`), `UIPlugin`
  (`observability.openshift.io/v1alpha1`), and Perses dashboard/datasource CRDs
  (`perses.dev/v1alpha2`). The script waits for these CRDs to exist and become established.

## Setup

Run these commands from the repository root.

1. Select Central's namespace and configure the API connection:

   ```sh
   oc project stackrox  # Replace with your Central namespace.
   NAMESPACE=$(oc project -q)
   export NAMESPACE
   export ROX_API_ENDPOINT=https://central.example.com
   export ROX_API_TOKEN='<your-api-token>'
   ```

   Use a token with permission to manage machine access configurations and
   access-control objects (`Access` read/write).
   Metrics configuration also needs `Administration` read/write. Central API
   requests verify TLS using the system trust store by default. If Central uses
   a private CA, set `ROX_API_CA_FILE` to a readable PEM CA bundle (for example,
   `export ROX_API_CA_FILE=/path/to/central-ca.crt`) before running setup. This
   CA is for the external Central API endpoint, not for the in-cluster scrape.

2. Install the monitoring example:

   ```sh
   bash example-openshift-setup.sh
   ```

   `NAMESPACE` defaults to the current OpenShift project. `TIMEOUT` defaults to
   300 seconds for each resource-creation/readiness wait. The script creates the
   monitoring stack, scrape configuration, alert rule, and Perses resources.
   It creates the annotated `openshift-service-ca.crt` ConfigMap in Central's
   namespace and waits (up to `TIMEOUT`) for OpenShift to inject its
   `service-ca.crt` key before configuring the scrape. It waits for this
   stack's Prometheus StatefulSet to become ready.

   The sample retains metrics for 45 days to support the 30-day vulnerability
   comparison. Size storage for your environment and use persistent storage if
   the history must survive Prometheus pod recreation.

   The scrape reaches `central-ocp.<namespace>.svc:443` and validates its
   OpenShift-managed serving certificate with the `openshift-service-ca.crt`
   ConfigMap. The [RHACS M2M script](../rhacs/configure-m2m-metrics-access.sh)
   provisions the **OpenShift Prometheus Metrics Reader** role when absent and
   adds an exact service-account `sub` mapping to an existing machine access
   configuration for this issuer, or creates one. It preserves unrelated
   mappings and rejects an incompatible existing configuration.

   COO creates the Prometheus resource from `MonitoringStack/sample-rhacs`.
   Setup then applies [the Prometheus overlay](prometheus-m2m.yaml.tpl) with
   Server-Side Apply to its unowned `volumes`, `volumeMounts`, and `scrapeClasses`
   fields; [the ScrapeConfig](scrape-config.yaml.tpl) selects that class.
   Prometheus reads a projected, audience-bound service-account token on each
   scrape. Kubelet renews the token automatically without a Prometheus restart.

3. Enable the [custom metrics](../rhacs/README.md#configuring-metrics-via-api)
   needed by the [dashboard](../perses/README.md). Allow at least one configured
   gathering period and one Prometheus scrape before expecting data.

## Verify and rerun

The RHACS machine access configuration expects audience `central.stackrox.io`.
The setup script uses the same value in the projected token, and maps only
`^system:serviceaccount:<namespace>:sample-rhacs-prometheus$` to the reader
role. The role needs read access to `Administration` for `GET /metrics`.
The script does not alter an already-installed permission set with the same
name; verify it grants that access if a scrape authenticates but returns 403.

COO must not already own the overlay's three Prometheus fields. Server-Side
Apply refuses ownership conflicts rather than forcing them; inspect
`oc -n "$NAMESPACE" get prometheus.monitoring.rhobs sample-rhacs -o yaml --show-managed-fields`
if your COO version differs. The service CA handles server TLS independently
of M2M client authentication.
Only trusted administrators should be able to create or edit scrape resources
selected by this stack: a scrape selecting `rhacs-m2m` can send the token to
its chosen target. The dedicated audience prevents using that token against
the Kubernetes API but does not prevent its use against RHACS.

In the Prometheus Targets page, check that
`scrapeConfig/<namespace>/sample-stackrox-scrape-config` is **UP** without
`tls_config.cert_file` or `tls_config.key_file` in its generated scrape
configuration. Confirm it stays UP across token rotation.
The `StackRoxMetricsScrapeUnavailable` rule alerts after five minutes if this
example's target is down or absent. It selects the `rhacs_scrape` label supplied
by the ScrapeConfig, so an unrelated Central scrape cannot mask a missing
target. Like any rule in the same Prometheus, it cannot alert if Prometheus or
rule evaluation itself is unavailable.

Run the [COO cluster smoke test](../tests/README.md) to check scraping and the Perses
resources. In the OpenShift console, open **Observe → Dashboards** and select
**Advanced Cluster Security / Overview** to check rendering.

On migration from the certificate-backed example, applying the new ScrapeConfig
removes the TLS client certificate references. The setup script verifies this
before reporting success. It does **not** delete the old
`sample-stackrox-prometheus-tls` Secret, the user-certificate auth provider, or
its role mappings: check for other users before removing those credentials.
The former certificate-request helper is not part of this example; the
default setup no longer requires permission to approve CSRs.

## Resources

- **Documentation**: [OpenShift Monitoring Overview](https://docs.redhat.com/en/documentation/red_hat_openshift_cluster_observability_operator/1-latest/html-single/about_red_hat_openshift_cluster_observability_operator/index)
- **Official repository**: [observability-operator on GitHub](https://github.com/rhobs/observability-operator)
