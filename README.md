# Monitoring components configuration guides and examples

The examples in this repository assume that RHACS is installed in the current context namespace.

RHACS exposes metrics, Prometheus scrapes them, and Perses displays the results.
Choose one Prometheus deployment path:

- **OpenShift with Cluster Observability Operator (COO):** follow the
  [setup guide](cluster-observability-operator/README.md), then run
  `bash example-openshift-setup.sh`. This path uses the monitoring stack's
  automatically rotated service-account token for RHACS M2M authentication
  and installs the Perses console integration.
- **An existing Prometheus Operator installation:** follow the
  [projected service-account token example](prometheus-operator/README.md).

Custom metrics require RHACS 4.9 or later. The operator APIs required by each path
are listed in its guide; the repository does not yet have a cluster-tested version matrix.

## Examples

- [cluster-observability-operator](cluster-observability-operator) —
  Sample namespaced monitoring stack configuration.
- [prometheus-operator](prometheus-operator) —
  Sample operator based Prometheus server configuration.
- [rhacs](rhacs) —
  Instructions and configuration on the RHACS side.
- [perses](perses) —
  Perses Data Source and Dashboard examples.

## Validation

```sh
python3 -m unittest discover -s tests -p '*_test.py'
```

These [local tests](tests/README.md) need no cluster. They validate the manifests,
dashboard queries, alert-rule behavior, documented metric labels, and the setup
scripts' failure handling and reruns. CI also runs ShellCheck.

To check an installed example on either deployment path, see the
[installed-setup smoke checks](tests/README.md#installed-setup-smoke-checks).

## Resources

- **Documentation**: [Red Hat Advanced Cluster Security for Kubernetes](https://docs.redhat.com/en/documentation/red_hat_advanced_cluster_security_for_kubernetes/4.9/).
- **Official repository**: [stackrox/stackrox](https://github.com/stackrox/stackrox)
