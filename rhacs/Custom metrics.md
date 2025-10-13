# RHACS Custom metrics

RHACS central API service (starting from verion 4.9) exposes Prometheus metrics on `/metrics` path.
The access is subject for authentication, authorization and scoped access control.

## Configuring role

[declarative-configuration-configmap.yaml](declarative-configuration-configmap.yaml): sample Permission Set and Role declarative configuration for Prometheus server access.

See details on using declarative configuration in [the product documentation](https://docs.redhat.com/en/documentation/red_hat_advanced_cluster_security_for_kubernetes/4.8/html/configuring/declarative-configuration-using).

## Configuring API access

* You can store a long-living ROX API token in a secret.
* You can configure Prometheus to access RHACS API with a Kubernetes service account token in a few of ways:
  * OpenShift OAuth provider:
    * use the long-living service account token as the client key.
  * Short-living projected service account token:
    * can only be configured via additional scrape config file (not with ServiceMonitor, not with PodMonitor, neither with ScrapeConfig);
    * see the example in [prometheus-operator](../prometheus-operator).
  * Generated service account token secret:
    * you can create a secret of type `kubernetes.io/service-account-token`, and Kubernetes will add there a long-living token, which can be used as the Bearer token for accessing RHACS API.
* You can configure Prometheus to access RHACS API using TLS certificate:
  * see examples in [cluster-observability-operator](../cluster-observability-operator).
