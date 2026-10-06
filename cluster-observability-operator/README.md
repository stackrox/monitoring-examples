# Cluster Observability Operator

The **Cluster Observability Operator (COO)** is a Kubernetes operator that simplifies the deployment and management of observability tools on OpenShift and Kubernetes clusters. It automates the installation and configuration of monitoring stack components including Prometheus, Alertmanager, and other observability tools.

The operator provides a streamlined way to set up monitoring infrastructure through Kubernetes-native custom resources, supporting features like multi-tenancy, custom metrics collection, and integration with OpenShift platform capabilities.

## Prerequisites

- An OpenShift cluster with RHACS 4.9 or later, and permission to install operators,
  create the cluster-scoped `UIPlugin`, and manage resources in the Central namespace.
- Permission to create `CertificateSigningRequests` and approve them for the
  `kubernetes.io/kube-apiserver-client` signer. The monitoring stack scrapes
  Central with a client certificate for its own service account identity, and
  that signer issues it.
- Read access to the `client-ca-file` key of the
  `extension-apiserver-authentication` ConfigMap in `kube-system`. The setup
   script provisions it with this CA via the RHACS access-control script, and on every later run
  compares it against the certificates that provider already trusts.
- Bash, `oc`, `kubectl`, `openssl`, `curl`, `jq`, `base64`, and `envsubst` (gettext).
  `oc` and `kubectl` must use the same cluster context.
- Central must be reachable as `central` in the selected namespace. The script
  selects the service to scrape and the CA that verifies it from what the
  namespace publishes, so there is nothing to adjust beforehand;
  `scrape-config.yaml.tpl` takes both as `${SCRAPE_SERVICE}` and `${SCRAPE_CA}`.
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

   Use a token with permission to read and create auth providers (`Access` read/write).
   Metrics configuration also needs `Administration` read/write. The example script
   uses `curl -k` for the external Central API; the Prometheus scrape verifies TLS
   using the configured CA.

2. Install the monitoring example:

   ```sh
   bash example-openshift-setup.sh
   ```

   `NAMESPACE` defaults to the current OpenShift project. `TIMEOUT` defaults to
   300 seconds for each resource-creation/readiness wait. The script creates the
   monitoring stack, scrape configuration, alert rule, and Perses resources.
   It waits for this stack's Prometheus StatefulSet to become ready.

   The scrape target and its CA are selected at install time. Where the
   namespace publishes `service/central-ocp` (RHACS 5.0 and later, annotated
   with `service.beta.openshift.io/serving-cert-secret-name: central-ocp-tls`),
   the scrape goes to `central-ocp.<namespace>.svc:443` and verifies the serving
   certificate with the `openshift-service-ca.crt` ConfigMap, key
   `service-ca.crt`. The cluster's service CA operator publishes that ConfigMap
   in every namespace and rotates the serving certificate, so nothing has to be
   renewed by hand. Otherwise the scrape goes to `central.<namespace>.svc:443`
   and verifies with `secret/central-tls`, key `ca.pem`, the StackRox CA that
   signs Central's own serving certificate. Both services reach the same Central
   endpoint.

   It also configures authentication and authorization, so neither a manual step
   in the RHACS console nor a declarative configuration ConfigMap is needed:

   - RHACS is expected to install the **OpenShift Prometheus Metrics Reader**
     role with its permission set and the **OpenShift Central Cluster** access
     scope, plus an **OpenShift Platform Client Certificates** auth provider
     that trusts the cluster's client CA and maps
     `system:serviceaccount:openshift-monitoring:prometheus-k8s` to that role
      (ROX-33524); no released version does so yet. When that provider is
      absent, the script invokes [the RHACS access-control script](../rhacs/create-openshift-platform-access-control.sh)
      to provision these defaults, reusing any permission set, access scope, or
      role already present. When the provider exists, setup leaves those objects
      and its trusted CA untouched.
   - It requests a client certificate for the monitoring stack's service
     account, `system:serviceaccount:<namespace>:sample-rhacs-prometheus`,
     stores it in the `sample-stackrox-prometheus-tls` Secret that the
     ScrapeConfig mounts, and adds a role mapping from that identity to the
     same **OpenShift Prometheus Metrics Reader** role. The identity travels in
     the certificate's common name, which X.509 limits to 64 characters; the
     script stops with an explicit error when the namespace makes it longer.
     The certificate is a cluster credential for that service account, so treat
     the Secret like the account's token and keep it in the Central namespace.

3. Enable the [custom metrics](../rhacs/README.md#configuring-metrics-via-api)
   needed by the [dashboard](../perses/README.md). Allow at least one configured
   gathering period and one Prometheus scrape before expecting data.

## Verify and rerun

Run the [COO cluster smoke test](../tests/README.md) to check scraping and the Perses
resources. In the OpenShift console, open **Observe → Dashboards** and select
**Advanced Cluster Security / Overview** to check rendering.

A scrape that authenticates but returns 403 points at the permission set:
Central authorizes `GET /metrics` with read access to `Administration`, and the
script does not verify that an already-installed permission set of the same
name grants it.

The signer sets the certificate's validity period, typically about a month, and
nothing renews it automatically. Rerun the script before it expires:
`request-client-certificate.sh` keeps the installed certificate while its subject
matches and it stays valid for more than `RENEW_BEFORE_DAYS` (default 7), and
otherwise requests a new one and replaces it in the Secret in place. Prometheus
picks the renewed files up from the mount without a restart. The script adds no
duplicate auth providers, access control objects, or role mappings on reruns.

A rotation of the cluster's own signer is a separate matter. The script copies
`client-ca-file` into the auth provider once, when it creates it, and never
replaces the CA an existing provider trusts. Each rerun therefore compares the
SHA-256 fingerprints of the certificates in the provider against the cluster's
published bundle: it stops with an error when none of them match, because every
scrape would be rejected, and warns when only some are missing, because
certificates from a rotated signer will be rejected. Updating the trusted
certificates is a manual step, in the RHACS console from the `client-ca-file`
key of `kube-system/extension-apiserver-authentication`, or by deleting the
provider and letting the next run recreate it.

## Resources

- **Documentation**: [OpenShift Monitoring Overview](https://docs.redhat.com/en/documentation/red_hat_openshift_cluster_observability_operator/1-latest/html-single/about_red_hat_openshift_cluster_observability_operator/index)
- **Official repository**: [observability-operator on GitHub](https://github.com/rhobs/observability-operator)
