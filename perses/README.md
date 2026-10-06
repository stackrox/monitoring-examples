# Sample Perses configuration

The provided [datasource template](datasource.yaml.tpl) takes the Prometheus
service as `${PROMETHEUS_SERVICE}` in the selected `${NAMESPACE}`, because each
deployment path publishes its own:

- [monitoring stack](../cluster-observability-operator) — `sample-rhacs-prometheus`,
  which COO names after the `MonitoringStack`. `example-openshift-setup.sh`
  renders and applies the template for you.
- [prometheus-operator](../prometheus-operator) — `sample-stackrox-prometheus-server`,
  published by [service.yaml](../prometheus-operator/service.yaml).

Neither path uses `prometheus-operated`. The operator reconciles that headless
service to govern the StatefulSet, and its selector matches every Prometheus in
the namespace, so it is not a stable target for a datasource.

For any other installation, set `PROMETHEUS_SERVICE` to a service that addresses
your Prometheus, or change `spec.config.plugin.spec.proxy.spec.url` directly.

The [dashboard](dashboard.yaml) references some predefined metrics, and some that need to be enabled at the RHACS side.

Available by default:

- `rox_central_health_cluster_info`
- `rox_central_cfg_total_policies`
- `rox_central_cert_exp_hours`

Need to be enabled via UI or API:

- `rox_central_image_vuln_deployment_severity`, with at least the following labels:
  - Cluster
  - Namespace
  - Deployment
  - IsPlatformWorkload
  - IsFixable
  - Severity
- `rox_central_node_vuln_node_severity`, with at least Cluster label.
- `rox_central_policy_violation_namespace_severity`, with at least Cluster, Namespace, and Severity labels.

See [configuring metrics via API](../rhacs/README.md#configuring-metrics-via-api)
for a command that enables all three customizable metrics with these labels.

The dashboard's default time range is 30 days, while the sample
[monitoring stack](../cluster-observability-operator/monitoring-stack.yaml) keeps
only `spec.retention: 1d`. Panels then show data for the retained period only.
Raise that retention, or lower `spec.config.duration` in
[dashboard.yaml](dashboard.yaml), to match the history you want.

![Screenshot](screenshot.png "Screenshot")
