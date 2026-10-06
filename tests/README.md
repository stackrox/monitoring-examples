# Tests

## Local tests

```sh
python3 -m unittest discover -s tests -p '*_test.py' -v
```

These tests never connect to a cluster; they use simulated cluster/API commands
and temporary files.

| File | Covers |
| --- | --- |
| `manifests_test.py` | Manifests, rendered templates, documentation links, dashboard queries/variables, alert-rule behavior, and whether the documented metric labels match the dashboard. |
| `setup_test.py` | `example-openshift-setup.sh` and the certificate helper: ordering, failure handling, and reruns. |
| `smoke_test.py` | `e2e.sh` check logic and cleanup for both paths. |

Dependencies: Python 3 with PyYAML, Bash, OpenSSL, jq, envsubst (gettext), and
base64. `promtool` from [Prometheus](https://prometheus.io/download/) enables the
PromQL, scrape-configuration, and alert-rule checks, which skip when it is absent.
`alert_rules_test.yaml` holds the promtool alert cases and runs via
`manifests_test.py`.

CI runs these tests, `bash -n`, and ShellCheck on pushes and pull requests.

## Installed-setup smoke checks

`e2e.sh` is an **opt-in cluster smoke test**, despite its filename. Install and
configure the chosen example first; the script does not provision a cluster,
install operators, apply manifests, configure RHACS, or create credentials.

```bash
bash tests/e2e.sh coo stackrox
bash tests/e2e.sh prometheus-operator stackrox

# Use the current kubectl context's namespace (or "default" if unset):
bash tests/e2e.sh coo

# Allow longer for reconciliation and initial metric gathering:
SMOKE_TIMEOUT_SECONDS=900 bash tests/e2e.sh prometheus-operator stackrox
```

A path argument is required; invoking the script without one makes no cluster
requests. It uses the current `kubectl` context, including `KUBECONFIG`, and pins
that context for the run. The optional second argument selects the namespace.
`SMOKE_TIMEOUT_SECONDS` is a per-stage retry/readiness timeout, not a whole-run
deadline (default 300, range 1–86400). Individual HTTP calls time out after 15
seconds, so a retry stage can slightly exceed its deadline.

## Prerequisites and permissions

- Bash, `kubectl`, `curl`, `jq`, and standard shell utilities on the local host.
  A writable temporary directory (`TMPDIR`, or `/tmp`) and loopback port-forward
  connectivity are required.
- RHACS Central 4.9+ and the selected monitoring example already running in the
  same namespace, using the repository's sample resource names and Central
  service endpoint. Prometheus must expose its HTTP API on pod port 9090.
- **COO:** the monitoring stack's client certificate installed and its service
  account identity mapped to the RHACS **OpenShift Prometheus Metrics Reader**
  role; the sample MonitoringStack, ScrapeConfig, PersesDatasource, and
  PersesDashboard installed, with the Perses operator/backend running.
- **prometheus-operator:** the standalone Prometheus, service account, projected
  token volume, additional scrape configuration, and corresponding RHACS
  authentication/authorization configured.
- RHACS must already have gathered the three fixed metrics below, with data
  visible to the configured monitoring identity. Cluster health requires a
  visible secured cluster. Fixed metrics are gathered hourly; a new installation
  may need time beyond the default timeout. Custom vulnerability/policy-violation
  metrics are not prerequisites for this smoke test.
- Kubernetes access to get the named `prometheuses` resource in `monitoring.rhobs`
  (COO) or `monitoring.coreos.com` (standalone), get/watch its StatefulSet, get/list
  pods, and create `pods/portforward` connections in the selected namespace.
  COO also needs get access to `persesdatasources.perses.dev` and
  `persesdashboards.perses.dev`. Normal API discovery access is required.
  The script does not read Secrets or need a local RHACS API token.

## What is checked

Both paths share the same checks:

1. The named Prometheus owns the exact expected StatefulSet, which has nonzero
   replicas and completes its rollout:
   - COO: `prometheus-sample-rhacs`
   - Standalone: `prometheus-sample-stackrox-prometheus-server`
2. A Ready pod owned by that StatefulSet serves `/-/ready`. A temporary
   port-forward goes directly to that pod, avoiding another Prometheus behind a
   shared `prometheus-operated` Service.
3. `/api/v1/targets` contains the HTTPS Central `/metrics` target for the sample
   scrape, and every matching target is `up` without a scrape error. Standalone
   requires `job="sample-stackrox-metrics"`. COO matches the Central service
   endpoint because the operator may expose a generated job label instead of
   `ScrapeConfig.spec.jobName`. Accepted hosts are `central` and `central-ocp`,
   with or without `.<namespace>` and its `.svc`/fully qualified service DNS
   forms, on HTTPS port 443. The setup scrapes `central-ocp` where RHACS
   publishes it and `central` otherwise.
4. `/api/v1/query` returns nonempty, finite numeric samples for **each** fixed
   metric, restricted to each matched target's actual `job` and `instance`:
   - `rox_central_health_cluster_info`
   - `rox_central_cfg_total_policies`
   - `rox_central_cert_exp_hours`

A healthy target alone could scrape an empty metrics response. Requiring RHACS
metrics as well exercises the installed authenticated scrape and its scoped
data access. Zero-valued samples are valid. This does not independently verify
which credential the installed scrape configuration uses or test token rotation.

COO additionally requires `Available=True` and `Degraded=False` in the status of
`sample-stackrox-datasource` and `sample-stackrox-dashboard`, and prints those
statuses. These are the Perses operator's
[v1alpha2 reconciliation conditions](https://github.com/perses/perses-operator/blob/main/controllers/dashboards/persesdashboard_controller.go),
not an assumed `Ready` condition. If `Available.observedGeneration` is populated
and nonzero, it must cover the current generation. Versions that omit it cannot
provide that freshness guarantee. Missing or incompatible conditions fail with
the resource status for inspection rather than silently passing.

Exit status is zero only when all checks pass. Failed target checks print scrape
errors; failed metric checks print the last query response. The script reads
cluster resources and opens a local port-forward; it does not mutate the
installation. Exit/signal traps stop the port-forward and remove local temporary
files.

## Coverage limits and future installation tests

These checks validate an **existing setup**, not installation order, CRD/operator
bootstrap, credential generation, initial RHACS configuration, teardown, negative
authentication cases, or projected-token rotation. They inspect one Ready
Prometheus replica's API. Perses reconciliation status does not validate browser
rendering, every dashboard query, or end-to-end datasource connectivity from
Perses. Optional/custom dashboard metrics are outside this smoke check.

Future full installation E2E tests would need isolated clusters or namespaces,
explicit provisioning and cleanup, both authentication paths configured from
scratch, token renewal coverage, and separate Perses API/browser assertions.
Run the current smoke checks only when explicitly requested against an already
installed setup; local syntax checks such as `bash -n tests/e2e.sh` need no cluster.
