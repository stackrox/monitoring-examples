# RHACS Custom metrics

RHACS central API service (starting from version 4.9) exposes Prometheus metrics on `/metrics` path on port https (443).
The access is subject for authentication, authorization and scoped access control.

## Configuring role

RHACS is expected to install the **OpenShift Prometheus Metrics Reader** role by default, together with the permission set of the same name and the **OpenShift Central Cluster** access scope (ROX-33524); no released version does so yet. When the provider is absent, [create-openshift-platform-access-control.sh](create-openshift-platform-access-control.sh) creates these defaults. See [OpenShift Platform Client Certificates](#openshift-platform-client-certificates) below.

To define your own instead:
[declarative-configuration-configmap.yaml](declarative-configuration-configmap.yaml): sample Permission Set and Role declarative configuration for Prometheus server access.

See details on using declarative configuration in [the product documentation](https://docs.redhat.com/en/documentation/red_hat_advanced_cluster_security_for_kubernetes/4.9/html/configuring/declarative-configuration-using).

## Configuring API access

- You can store a long-lived ROX API token in a secret.
- You can configure Prometheus to access RHACS API with a Kubernetes service account token in a few of ways:
  - OpenShift OAuth provider:
    - use the long-lived service account token as the client key.
  - Short-lived projected service account token:
    - can only be configured via additional scrape config file (not with ServiceMonitor, not with PodMonitor, neither with ScrapeConfig);
    - see the example in [prometheus-operator](../prometheus-operator).
  - Generated service account token secret:
    - you can create a secret of type `kubernetes.io/service-account-token`, and Kubernetes will add there a long-lived token, which can be used as the Bearer token for accessing RHACS API.
- You can configure Prometheus to access RHACS API using TLS certificate:
  - see examples in [cluster-observability-operator](../cluster-observability-operator).

### OpenShift Platform Client Certificates

RHACS is expected to install an **OpenShift Platform Client Certificates** user certificates auth provider by default (ROX-33524); no released version does so yet. If it is absent, [example-openshift-setup.sh](../example-openshift-setup.sh) invokes [create-openshift-platform-access-control.sh](create-openshift-platform-access-control.sh) to create the provider and its access control objects. You can also run `bash rhacs/create-openshift-platform-access-control.sh` independently with `ROX_API_ENDPOINT` and `ROX_API_TOKEN` set. If the provider already exists, the provisioning script leaves it and its access control unchanged.
It trusts the cluster's client CA (`client-ca-file` of the `extension-apiserver-authentication` ConfigMap in `kube-system`), so any client certificate issued by the cluster's `kubernetes.io/kube-apiserver-client` signer authenticates.
Such a certificate carries a Kubernetes identity in its common name, and the provider's role mappings authorize it by matching that common name with the `name` attribute.
A provider the script creates keeps the copy of that CA taken at creation time, and nothing refreshes it: once the cluster's signer rotates, the trusted certificates have to be updated by hand, and a rerun of the script reports the mismatch rather than replacing them.

The provider carries one mapping for OpenShift platform monitoring:
`system:serviceaccount:openshift-monitoring:prometheus-k8s` to the **OpenShift Prometheus Metrics Reader** role, which combines the permission set of the same name with the **OpenShift Central Cluster** access scope.
The permission set grants read access to `Administration`, which is what Central checks to authorize `GET /metrics`.

Add a mapping for any other Prometheus with a certificate from that signer, for example one run by the Cluster Observability Operator:
[example-openshift-setup.sh](../example-openshift-setup.sh) maps its monitoring stack's service account to the same role.
[openshift-platform-access-control.json](openshift-platform-access-control.json) holds these definitions; when provisioning an absent provider, the script reuses any permission set, access scope, or role already present by name.
Note that X.509 limits a common name to 64 characters, so long namespace and service account names cannot be expressed as a certificate identity.

## Configuring metrics via API

Some of the exposed metrics are fixed: they are always exposed with a fixed set of labels.
Other are customizable: they are enabled with a non-zero gathering period, have a custom name and a custom set of labels.

Fixed metrics, gathered once per hour:

- Cluster health: `rox_central_health_cluster_info`
- Total policy numbers: `rox_central_cfg_total_policies`
- Certificate expiry: `rox_central_cert_exp_hours`

Customizable:

- Image vulnerabilities: `rox_central_image_vuln_<name>`, configuration key: `imageVulnerabilities`
- Node vulnerabilities: `rox_central_node_vuln_<name>`, configuration key: `nodeVulnerabilities`
- Policy violations: `rox_central_policy_violation_<name>`, configuration key: `policyViolations`

Call `/v1/config` service to get or set the configuration, including the customizable metrics.

To get the current configuration:

```sh
curl "$ROX_API_ENDPOINT/v1/config" -H "Authorization: Bearer $ROX_API_TOKEN" | jq
```

The public and the private parts can also be fetched separately with `/v1/config/public` and `/v1/config/private`.
Note that these paths are read-only: the configuration can only be updated with PUT on `/v1/config`, which takes the complete configuration.

To configure custom metrics, retrieve the current configuration, add or modify the `metrics` key under `privateConfig`, and send the complete configuration back. For example, the following enables the metric descriptors used by the Perses dashboard (and an additional image metric grouped by namespace). It replaces the descriptors in these three metric groups; preserve any existing descriptors you also need.

```sh
curl -s "$ROX_API_ENDPOINT/v1/config" -H "Authorization: Bearer $ROX_API_TOKEN" | \
  jq '.publicConfig = (.publicConfig // {}) |
      .privateConfig.metrics.imageVulnerabilities = {
        gatheringPeriodMinutes: 10,
        descriptors: {
          deployment_severity: {
            labels: ["Cluster", "Namespace", "Deployment", "IsPlatformWorkload", "IsFixable", "Severity"]
          },
          namespace_severity: {
            labels: ["Cluster", "Namespace", "Severity"]
          }
        }
      } |
      .privateConfig.metrics.nodeVulnerabilities = {
        gatheringPeriodMinutes: 10,
        descriptors: {
          node_severity: { labels: ["Cluster", "Severity"] }
        }
      } |
      .privateConfig.metrics.policyViolations = {
        gatheringPeriodMinutes: 10,
        descriptors: {
          namespace_severity: { labels: ["Cluster", "Namespace", "Severity"] }
        }
      } |
      { config: . }' | \
  curl -X PUT "$ROX_API_ENDPOINT/v1/config" -H "Authorization: Bearer $ROX_API_TOKEN" --data-binary @-
```

> Note: all metrics under `imageVulnerabilities` are exposed with `rox_central_image_vuln_` prefix.
>
> Note: the new configuration has to be put back under `config` key, as: `{ "config": {...} }`.
>
> Note: GET may return `"publicConfig": null`, which PUT rejects. The `.publicConfig = (.publicConfig // {})` expression replaces `null` with an empty object and keeps the existing value otherwise.
