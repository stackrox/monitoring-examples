"""Validate the example manifests, queries, alert rules, and their documentation."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ENVIRONMENT = {
    "NAMESPACE": "stackrox",
    "ROX_API_ENDPOINT": "https://central.example.test",
    # The M2M example uses Central's OpenShift service and its CA.
    "SCRAPE_SERVICE": "central-ocp",
    "SCRAPE_CA": "{configMap: {name: openshift-service-ca.crt, key: service-ca.crt}}",
    # Each deployment path publishes its own Prometheus service.
    "PROMETHEUS_SERVICE": "sample-rhacs-prometheus",
}
# Metrics RHACS always exposes; they have no descriptor configuration.
FIXED_METRICS = {
    "rox_central_health_cluster_info",
    "rox_central_cfg_total_policies",
    "rox_central_cert_exp_hours",
}
# Metric name prefix per /v1/config metrics group.
CUSTOM_METRIC_GROUPS = {
    "imageVulnerabilities": "rox_central_image_vuln_",
    "nodeVulnerabilities": "rox_central_node_vuln_",
    "policyViolations": "rox_central_policy_violation_",
}


def render(path):
    text = path.read_text()
    if path.suffix != ".tpl":
        return text
    return subprocess.run(
        ["envsubst"], input=text, text=True, capture_output=True, check=True,
        env={**os.environ, **TEMPLATE_ENVIRONMENT},
    ).stdout


def manifests():
    for path in sorted(ROOT.rglob("*.yaml*")):
        if ".git" not in path.parts and path.name.endswith((".yaml", ".yaml.tpl")):
            yield path


def dashboard_queries():
    dashboard = yaml.safe_load((ROOT / "perses/dashboard.yaml").read_text())
    for name, panel in dashboard["spec"]["config"]["panels"].items():
        for index, query in enumerate(panel["spec"]["queries"]):
            yield f"{name}[{index}]", query["spec"]["plugin"]["spec"]["query"]


class ManifestTest(unittest.TestCase):
    def test_manifests_and_rendered_templates_parse(self):
        for path in manifests():
            with self.subTest(path=path.relative_to(ROOT)):
                documents = list(yaml.safe_load_all(render(path)))
                self.assertTrue(any(documents), "no YAML documents")
                for document in documents:
                    embedded = {**document.get("data", {}), **document.get("stringData", {})}
                    for key, value in embedded.items():
                        if key.endswith((".yaml", ".yml")):
                            list(yaml.safe_load_all(value))

    def test_platform_access_control_objects_are_consistent(self):
        objects = json.loads((ROOT / "rhacs/openshift-platform-access-control.json").read_text())
        self.assertEqual(objects["authProvider"]["type"], "userpki")
        self.assertTrue(objects["authProvider"]["enabled"])
        # Central authorizes GET /metrics with View(Administration), so a role
        # built from this permission set scrapes nothing without that access.
        self.assertEqual(objects["permissionSet"]["resourceToAccess"],
                         {"Administration": "READ_ACCESS"})
        # The role mapping is useless unless it names the role installed with it.
        self.assertEqual(objects["platformPrometheus"]["role"], objects["role"]["name"])
        # Certificate identities arrive as the "name" attribute, the common name.
        self.assertEqual(objects["platformPrometheus"]["key"], "name")
        self.assertRegex(objects["platformPrometheus"]["value"], r"^system:serviceaccount:[^:]+:[^:]+$")

    def test_monitoring_stack_matches_its_service_account(self):
        setup = (ROOT / "example-openshift-setup.sh").read_text()
        stack = re.search(r"^MONITORING_STACK=(\S+)$", setup, re.MULTILINE).group(1)
        manifest = yaml.safe_load((ROOT / "cluster-observability-operator/monitoring-stack.yaml").read_text())
        self.assertEqual(manifest["metadata"]["name"], stack)
        self.assertEqual(stack, "sample-rhacs")

    def test_coo_m2m_scrape_uses_projected_token_without_a_client_certificate(self):
        directory = ROOT / "cluster-observability-operator"
        prometheus = yaml.safe_load(render(directory / "prometheus-m2m.yaml.tpl"))
        scrape = yaml.safe_load(render(directory / "scrape-config.yaml.tpl"))
        self.assertEqual(prometheus["metadata"]["name"], "sample-rhacs")
        self.assertEqual(prometheus["metadata"]["namespace"], scrape["metadata"]["namespace"])
        token = prometheus["spec"]["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"]
        self.assertEqual(token["audience"], "central.stackrox.io")
        mount = prometheus["spec"]["volumeMounts"][0]
        scrape_class = prometheus["spec"]["scrapeClasses"][0]
        self.assertFalse(scrape_class.get("default", False))
        self.assertEqual(scrape_class["authorization"]["credentialsFile"],
                         f'{mount["mountPath"]}/{token["path"]}')
        self.assertEqual(scrape["spec"]["scrapeClass"], scrape_class["name"])
        self.assertEqual(scrape["metadata"]["labels"], {"app": "central"})
        self.assertEqual(scrape["spec"]["staticConfigs"][0]["targets"],
                         ["central-ocp.stackrox.svc:443"])
        self.assertEqual(scrape["spec"]["tlsConfig"]["ca"],
                         {"configMap": {"name": "openshift-service-ca.crt", "key": "service-ca.crt"}})
        self.assertNotIn("cert", scrape["spec"]["tlsConfig"])
        self.assertNotIn("keySecret", scrape["spec"]["tlsConfig"])

    def test_templates_only_use_substituted_variables(self):
        for path in sorted(ROOT.rglob("*.tpl")):
            with self.subTest(path=path.relative_to(ROOT)):
                variables = set(re.findall(r"\$\{(\w+)\}", path.read_text()))
                self.assertTrue(variables)
                self.assertFalse(variables - set(TEMPLATE_ENVIRONMENT))

    def test_scrape_ca_the_setup_selects_renders(self):
        setup = (ROOT / "example-openshift-setup.sh").read_text()
        selected = re.findall(r"^\s*SCRAPE_SERVICE=(\S+)\n\s*SCRAPE_CA='(.+)'$", setup, re.MULTILINE)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][0], "central-ocp")
        template = ROOT / "cluster-observability-operator/scrape-config.yaml.tpl"
        for service, authority in selected:
            with self.subTest(service=service):
                rendered = subprocess.run(
                    ["envsubst"], input=template.read_text(), text=True, capture_output=True,
                    check=True, env={**os.environ, **TEMPLATE_ENVIRONMENT,
                                     "SCRAPE_SERVICE": service, "SCRAPE_CA": authority},
                ).stdout
                tls = yaml.safe_load(rendered)["spec"]["tlsConfig"]
                self.assertEqual(len(tls["ca"]), 1)
                (reference,) = tls["ca"].values()
                self.assertEqual(set(reference), {"name", "key"})

    def test_standalone_service_addresses_only_its_own_prometheus(self):
        """The shared prometheus-operated Service selects every Prometheus."""
        service = yaml.safe_load((ROOT / "prometheus-operator/service.yaml").read_text())
        prometheus = yaml.safe_load((ROOT / "prometheus-operator/prometheus.yaml").read_text())
        selector = service["spec"]["selector"]
        self.assertEqual(selector["app.kubernetes.io/name"], "prometheus")
        # The operator labels each pod with the Prometheus resource it belongs to.
        self.assertEqual(selector["operator.prometheus.io/name"], prometheus["metadata"]["name"])
        (port,) = service["spec"]["ports"]
        self.assertEqual((port["port"], port["targetPort"]), (9090, "web"))

    def test_each_path_points_the_datasource_at_its_own_prometheus(self):
        setup = (ROOT / "example-openshift-setup.sh").read_text()
        stack = re.search(r"^MONITORING_STACK=(\S+)$", setup, re.MULTILINE).group(1)
        standalone = yaml.safe_load((ROOT / "prometheus-operator/service.yaml").read_text())
        # COO names the stack's service after the MonitoringStack.
        services = {
            "coo": f"{stack}-prometheus",
            "prometheus-operator": standalone["metadata"]["name"],
        }
        self.assertIn(f'PROMETHEUS_SERVICE="$MONITORING_STACK-prometheus"', setup)
        # Neither path may fall back to the shared governing Service.
        for path, service in services.items():
            with self.subTest(path=path):
                self.assertNotEqual(service, "prometheus-operated")
                rendered = subprocess.run(
                    ["envsubst"], input=(ROOT / "perses/datasource.yaml.tpl").read_text(),
                    text=True, capture_output=True, check=True,
                    env={**os.environ, **TEMPLATE_ENVIRONMENT, "PROMETHEUS_SERVICE": service},
                ).stdout
                url = yaml.safe_load(rendered)["spec"]["config"]["plugin"]["spec"]["proxy"]["spec"]["url"]
                self.assertEqual(url, f"http://{service}.stackrox.svc:9090")
        # The standalone README has to install the Service it names.
        readme = (ROOT / "prometheus-operator/README.md").read_text()
        self.assertIn("prometheus-operator/service.yaml", readme)
        self.assertIn(services["prometheus-operator"], readme)

    def test_local_documentation_links_resolve(self):
        for path in sorted(ROOT.rglob("*.md")):
            for target in re.findall(r"\]\(([^\s)]+)\)", path.read_text()):
                if "://" in target or target.startswith("#"):
                    continue
                with self.subTest(path=path.relative_to(ROOT), target=target):
                    self.assertTrue((path.parent / target.split("#")[0]).exists())

    def test_dashboard_layouts_reference_existing_panels(self):
        config = yaml.safe_load((ROOT / "perses/dashboard.yaml").read_text())["spec"]["config"]
        referenced = {
            item["content"]["$ref"].rsplit("/", 1)[-1]
            for layout in config["layouts"] for item in layout["spec"]["items"]
        }
        self.assertEqual(referenced, set(config["panels"]))

    def test_dashboard_variables_are_defined(self):
        config = yaml.safe_load((ROOT / "perses/dashboard.yaml").read_text())["spec"]["config"]
        defined = {variable["spec"]["name"] for variable in config["variables"]}
        for name, query in dashboard_queries():
            with self.subTest(query=name):
                self.assertFalse(set(re.findall(r"\$(\w+)", query)) - defined)

    def test_dashboard_metrics_are_documented(self):
        documentation = (ROOT / "perses/README.md").read_text()
        for name, query in dashboard_queries():
            for metric in re.findall(r"rox_central_\w+", query):
                with self.subTest(query=name, metric=metric):
                    self.assertIn(metric, documentation)


class MetricsConfigurationTest(unittest.TestCase):
    """The dashboard only renders when the documented labels match its queries."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("jq"):
            raise unittest.SkipTest("Missing test dependency: jq")
        cls.required = {}
        for name, query in dashboard_queries():
            metrics = set(re.findall(r"rox_central_\w+", query))
            assert len(metrics) == 1, f"{name} must query exactly one metric"
            metric = metrics.pop()
            labels = set(re.findall(r"(\w+)\s*(?:=~|!=|=)", "".join(re.findall(r"\{([^}]*)\}", query))))
            for grouping in re.findall(r"by\s*\(([^)]*)\)", query):
                labels.update(label.strip() for label in grouping.split(",") if label.strip())
            cls.required.setdefault(metric, set()).update(labels)
        # The documented configuration command, executed against a stored configuration.
        readme = (ROOT / "rhacs/README.md").read_text()
        filters = re.findall(r"jq '(.*?)' \| \\", readme, re.DOTALL)
        assert len(filters) == 1, "expected exactly one configuration jq filter"
        result = subprocess.run(
            ["jq", filters[0]], text=True, capture_output=True, check=True,
            input=json.dumps({"privateConfig": {"unrelated": "preserved"}}),
        )
        cls.configuration = json.loads(result.stdout)["config"]

    def test_documented_command_preserves_unrelated_configuration(self):
        self.assertEqual(self.configuration["privateConfig"]["unrelated"], "preserved")
        self.assertEqual(json.loads(json.dumps(self.configuration)), self.configuration)

    def test_documented_descriptors_cover_dashboard_metrics_and_labels(self):
        descriptors = {}
        for group, prefix in CUSTOM_METRIC_GROUPS.items():
            configured = self.configuration["privateConfig"]["metrics"][group]
            self.assertGreater(configured["gatheringPeriodMinutes"], 0, group)
            for name, descriptor in configured["descriptors"].items():
                descriptors[prefix + name] = set(descriptor["labels"])
        for metric, labels in self.required.items():
            if metric in FIXED_METRICS:
                continue
            with self.subTest(metric=metric):
                self.assertIn(metric, descriptors)
                self.assertLessEqual(labels, descriptors[metric])

    def test_dashboard_label_requirements_are_documented(self):
        sections = re.split(r"- `(rox_central_\w+)`", (ROOT / "perses/README.md").read_text())
        documented = dict(zip(sections[1::2], sections[2::2]))
        for metric, labels in self.required.items():
            if metric in FIXED_METRICS:
                continue
            with self.subTest(metric=metric):
                self.assertIn(metric, documented)
                for label in labels:
                    self.assertRegex(documented[metric], rf"\b{label}\b")


@unittest.skipUnless(shutil.which("promtool"), "Missing test dependency: promtool")
class PrometheusTest(unittest.TestCase):
    def promtool(self, *arguments, document=None, directory=None):
        if document is not None:
            arguments = (*arguments, "/dev/stdin")
        result = subprocess.run(
            ["promtool", *arguments], text=True, capture_output=True, cwd=directory,
            input=None if document is None else yaml.safe_dump(document),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_alert_rules_are_valid(self):
        rules = yaml.safe_load((ROOT / "cluster-observability-operator/alert-rules.yaml").read_text())
        self.promtool("check", "rules", document=rules["spec"])

    def test_alert_rules_fire_recover_and_stay_quiet_when_healthy(self):
        rules = yaml.safe_load((ROOT / "cluster-observability-operator/alert-rules.yaml").read_text())
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "rules.yaml").write_text(yaml.safe_dump(rules["spec"]))
            shutil.copy(Path(__file__).with_name("alert_rules_test.yaml"), directory)
            self.promtool("test", "rules", "alert_rules_test.yaml", directory=directory)

    def test_additional_scrape_configuration_is_valid(self):
        secret = yaml.safe_load((ROOT / "prometheus-operator/additional-scrape-config.yaml").read_text())
        scrape_configs = yaml.safe_load(secret["stringData"]["prometheus-additional.yaml"])
        self.promtool("check", "config", "--syntax-only", document={"scrape_configs": scrape_configs})

    def test_dashboard_queries_are_valid_promql(self):
        rules = [
            {"record": "dashboard:check", "expr": re.sub(r"\$\w+", ".*", query)}
            for _, query in dashboard_queries()
        ]
        self.promtool("check", "rules", document={"groups": [{"name": "dashboard", "rules": rules}]})


if __name__ == "__main__":
    unittest.main()
