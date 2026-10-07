"""Setup regression tests: real shell/JSON tools, simulated cluster and API."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROVIDER = "OpenShift Platform Client Certificates"
SCRAPE_ROLE = "OpenShift Prometheus Metrics Reader"
ACCESS_SCOPE = "OpenShift Central Cluster"
PLATFORM_PROMETHEUS = "system:serviceaccount:openshift-monitoring:prometheus-k8s"
MOCK = r'''
import fcntl, json, os, pathlib, sys, yaml
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
# Branches below consume leading flags, so keep the invocation for the log.
call = [name, *args]
state_path = pathlib.Path(os.environ["MOCK_STATE"])
state = json.loads(state_path.read_text())
# A shell pipeline runs two of these concurrently, so record the writes and
# replay them on the current state under a lock instead of saving the snapshot.
writes = []
def finish(code=0, output=""):
    with open(f"{state_path}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads(state_path.read_text())
        current["calls"].append(call)
        for operation, key, value in writes:
            if operation == "set":
                current[key] = value
            elif operation == "append":
                current[key].append(value)
            elif operation == "count":
                current[key][value] = current[key].get(value, 0) + 1
            else:
                raise AssertionError(f"Unexpected state operation: {operation}")
        temporary = state_path.with_suffix(".writing")
        temporary.write_text(json.dumps(current))
        os.replace(temporary, state_path)
    if output:
        print(output)
    sys.exit(code)
if name == "sleep":
    finish()
if name == "oc":
    if args == ["project", "-q"]:
        finish(1 if os.environ.get("FAIL_PROJECT") else 0, "stackrox")
    if args[0:1] == ["-n"]:
        namespace, args = args[1], args[2:]
    if args[0:2] == ["get", "--raw"]:
        finish(output=json.dumps({"issuer": state["issuer"]}))
    if args[0:2] == ["get", "scrapeconfig.monitoring.rhobs/sample-stackrox-scrape-config"]:
        config = next(yaml.safe_load(text) for text in reversed(state["applied"])
                      if yaml.safe_load(text).get("kind") == "ScrapeConfig")
        finish(output=json.dumps(config))
    if args[0:2] == ["get", "configmap"]:
        assert namespace == "kube-system", namespace
        finish(output=os.environ["MOCK_CLIENT_CA"])
    if args[0:2] == ["get", "configmap/openshift-service-ca.crt"]:
        assert namespace == os.environ.get("NAMESPACE", "stackrox"), namespace
        assert args[2:] == ["-o", "jsonpath={.data.service-ca\\.crt}"], args
        resource = "configmap/openshift-service-ca.crt"
        count = state["gets"].get(resource, 0) + 1
        writes.append(("count", "gets", resource))
        if os.environ.get("NEVER_SERVICE_CA") or count < 3:
            finish(output="")
        finish(output="-----BEGIN CERTIFICATE-----\nmock\n-----END CERTIFICATE-----")
    # RHACS before 5.0 publishes no central-ocp service.
    if args[0:2] == ["get", "service/central-ocp"]:
        finish(1 if os.environ.get("MOCK_NO_CENTRAL_OCP") else 0)
    if args[0] == "get" and "--ignore-not-found" in args:
        resource = args[1]
        count = state["gets"].get(resource, 0) + 1
        writes.append(("count", "gets", resource))
        if os.environ.get("NEVER_CREATED") or count < 3:
            finish()
        finish(output=resource)
    if args[0] == "wait":
        assert state["gets"][args[2]] >= 3, "condition wait before creation"
    if args[0:2] == ["rollout", "status"]:
        assert args[2] == "statefulset/prometheus-sample-rhacs", args
        assert state["gets"][args[2]] >= 3, "rollout before creation"
    if args[0] == "apply" and args[-1] == "-":
        writes.append(("append", "applied", sys.stdin.read()))
    finish()
if name == "curl":
    if args[0:2] == ["--help", "all"]:
        finish(output="--fail-with-body Fail on HTTP errors but save the body")
    assert {"--fail", "--fail-with-body"} & set(args), "HTTP failures must propagate"
    assert "-k" not in args and "--insecure" not in args, "Central API must verify TLS"
    ca_file = os.environ.get("ROX_API_CA_FILE")
    assert (args[args.index("--cacert") + 1] if "--cacert" in args else None) == ca_file
    if os.environ.get("FAIL_HTTP"):
        finish(22, "HTTP 401")
    method = args[args.index("-X") + 1]
    path = next(arg for arg in args if arg.startswith("https://")).split("test", 1)[1]
    collections = {"/v1/auth/m2m": "m2mConfigs", "/v1/authProviders": "authProviders", "/v1/permissionsets": "permissionSets",
                   "/v1/simpleaccessscopes": "accessScopes", "/v1/groups": "groups",
                   "/v1/roles": "roles"}
    if method == "GET":
        collection = collections[path]
        finish(output=json.dumps({"configs" if path == "/v1/auth/m2m" else collection:
                                  state[collection]}))
    if os.environ.get("FAIL_POST"):
        finish(22, "HTTP 403")
    body = json.loads(args[args.index("--data-binary") + 1])
    if path.startswith("/v1/auth/m2m/"):
        writes.append(("set", "m2mConfigs", [body["config"] if cfg["id"] == path.rsplit("/", 1)[-1]
                                            else cfg for cfg in state["m2mConfigs"]]))
        finish(output="{}")
    if path == "/v1/auth/m2m":
        if os.environ.get("FAIL_M2M"):
            finish(22, "HTTP 403")
        body["config"]["id"] = f'm2m-{len(state["m2mConfigs"])}'
        writes.append(("append", "m2mConfigs", body["config"]))
        finish(output=json.dumps(body))
    if path.startswith("/v1/roles/"):
        writes.append(("append", "roles", body))
        finish(output="{}")
    collection = collections[path]
    if collection != "groups":
        body = dict(body, id=f"{collection}-{len(state[collection])}")
    writes.append(("append", collection, body))
    finish(output=json.dumps(body))
finish(1, "Unexpected command")
'''


class SetupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("bash", "openssl", "jq", "envsubst"):
            if not shutil.which(command):
                raise unittest.SkipTest(f"Missing test dependency: {command}")
        authority = tempfile.TemporaryDirectory()
        cls.addClassCleanup(authority.cleanup)
        cls.authority = Path(authority.name)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "365",
             "-subj", "/CN=test-kube-apiserver-client-signer",
             "-keyout", str(cls.authority / "ca.key"), "-out", str(cls.authority / "ca.crt")],
            check=True, capture_output=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "repo"
        self.repo.mkdir()
        for folder in ("cluster-observability-operator", "perses", "rhacs"):
            shutil.copytree(ROOT / folder, self.repo / folder)
        shutil.copy(ROOT / "example-openshift-setup.sh", self.repo)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for command in ("oc", "curl", "sleep"):
            path = self.bin / command
            path.write_text(f"#!{sys.executable}\n{MOCK}")
            path.chmod(0o755)
        self.state_path = self.directory / "state.json"
        # RHACS before 5.1: no provider and none of its access control objects.
        self.state_path.write_text(json.dumps({
            "calls": [], "gets": {}, "applied": [],
            "issuer": "https://oidc.example.test", "m2mConfigs": [],
            "authProviders": [], "permissionSets": [], "accessScopes": [], "groups": [],
            "roles": [{"name": "Admin"}],
        }))
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "MOCK_STATE": str(self.state_path),
            "MOCK_CLIENT_CA": (self.authority / "ca.crt").read_text(),
            "ROX_API_ENDPOINT": "https://central.example.test",
            "ROX_API_TOKEN": "test-token",
            "TIMEOUT": "10",
        }
        self.env.pop("NAMESPACE", None)
        self.env.pop("BASH_ENV", None)
        self.env.pop("ROX_API_CA_FILE", None)

    def state(self):
        return json.loads(self.state_path.read_text())

    def install_defaults(self, provider=True):
        """Simulate RHACS 5.1, which installs these access control objects."""
        state = self.state()
        state["permissionSets"] = [{"id": "installed-permission-set", "name": SCRAPE_ROLE,
                                    "resourceToAccess": {}}]
        state["accessScopes"] = [{"id": "installed-access-scope", "name": ACCESS_SCOPE}]
        state["roles"].append({"name": SCRAPE_ROLE,
                               "permissionSetId": "installed-permission-set",
                               "accessScopeId": "installed-access-scope"})
        if provider:
            state["authProviders"] = [{
                "id": "installed-provider", "name": PROVIDER, "type": "userpki", "enabled": True,
                "config": {"keys": self.env["MOCK_CLIENT_CA"]},
            }]
            state["groups"] = [{
                "props": {"authProviderId": "installed-provider", "key": "name",
                          "value": PLATFORM_PROMETHEUS},
                "roleName": SCRAPE_ROLE,
            }]
        self.state_path.write_text(json.dumps(state))

    def run_setup(self):
        # Deliberately run outside the repository to check path resolution.
        return subprocess.run(
            ["bash", str(self.repo / "example-openshift-setup.sh")],
            cwd=self.directory, env=self.env, text=True, capture_output=True, timeout=60,
        )

    def run_access_control_setup(self):
        return subprocess.run(
            ["bash", str(self.repo / "rhacs/create-openshift-platform-access-control.sh")],
            cwd=self.directory, env=self.env, text=True, capture_output=True, timeout=60,
        )

    def mappings(self, state=None):
        state = state or self.state()
        return {(group["props"]["value"], group["roleName"]) for group in state["groups"]}

    def test_setup_creates_role_m2m_config_and_certificate_free_scrape(self):
        self.env["NAMESPACE"] = "custom-rhacs"
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual([c["type"] for c in state["m2mConfigs"]], ["KUBE_SERVICE_ACCOUNT"])
        config = state["m2mConfigs"][0]
        self.assertEqual(config["issuer"], state["issuer"])
        self.assertEqual(config["audience"], "central.stackrox.io")
        self.assertEqual(config["mappings"], [{
            "key": "sub", "valueExpression":
            "^system:serviceaccount:custom-rhacs:sample-rhacs-prometheus$",
            "role": SCRAPE_ROLE,
        }])
        self.assertEqual([item["name"] for item in state["permissionSets"]], [SCRAPE_ROLE])
        self.assertEqual([item["name"] for item in state["accessScopes"]], [ACCESS_SCOPE])
        self.assertEqual([p["name"] for p in state["roles"]], ["Admin", SCRAPE_ROLE])
        self.assertEqual(state["authProviders"], [])
        self.assertEqual(self.scrape_config()["spec"]["scrapeClass"], "rhacs-m2m")
        self.assertEqual(self.scrape_config()["spec"]["staticConfigs"][0]["targets"],
                         ["central-ocp.custom-rhacs.svc:443"])
        self.assertEqual(self.scrape_config()["spec"]["tlsConfig"],
                         {"ca": {"configMap": {"name": "openshift-service-ca.crt",
                                               "key": "service-ca.crt"}}})
        service_ca = yaml.safe_load(
            (self.repo / "cluster-observability-operator/service-ca-configmap.yaml").read_text())
        self.assertEqual(service_ca["metadata"]["name"], "openshift-service-ca.crt")
        self.assertEqual(service_ca["metadata"]["annotations"],
                         {"service.beta.openshift.io/inject-cabundle": "true"})
        calls = state["calls"]
        apply_index = next(i for i, call in enumerate(calls)
                           if "cluster-observability-operator/service-ca-configmap.yaml" in call)
        ca_reads = [i for i, call in enumerate(calls)
                    if "configmap/openshift-service-ca.crt" in call]
        scrape_index = next(i for i, call in enumerate(calls)
                            if call[:3] == ["oc", "-n", "custom-rhacs"]
                            and call[-3:] == ["apply", "-f", "-"])
        self.assertLess(apply_index, ca_reads[0])
        self.assertEqual(len(ca_reads), 3)
        self.assertLess(ca_reads[-1], scrape_index)
        overlay = next(yaml.safe_load(doc) for doc in state["applied"]
                       if yaml.safe_load(doc).get("kind") == "Prometheus")
        self.assertEqual(overlay["metadata"]["namespace"], "custom-rhacs")
        self.assertTrue(any("--server-side" in call and "--field-manager=rhacs-m2m-example" in call
                            for call in state["calls"] if call[0] == "oc"))

    def scrape_config(self):
        for document in self.state()["applied"]:
            parsed = yaml.safe_load(document)
            if parsed.get("kind") == "ScrapeConfig":
                return parsed
        self.fail("the setup applied no ScrapeConfig")

    def test_existing_config_retains_its_mappings_and_is_idempotent(self):
        state = self.state()
        existing = {"id": "existing", "type": "KUBE_SERVICE_ACCOUNT", "issuer": state["issuer"],
                    "audience": "central.stackrox.io", "tokenExpirationDuration": "1h",
                    "mappings": [{"key": "sub", "valueExpression": "^existing$", "role": "Admin"}]}
        state["m2mConfigs"].append(existing)
        self.state_path.write_text(json.dumps(state))
        for _ in range(2):
            result = self.run_setup()
            self.assertEqual(result.returncode, 0, result.stderr)
        configs = self.state()["m2mConfigs"]
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["mappings"][0], existing["mappings"][0])
        self.assertEqual(len(configs[0]["mappings"]), 2)

    def test_incompatible_m2m_config_fails_without_replacing_it(self):
        state = self.state()
        state["m2mConfigs"] = [{"id": "other", "issuer": state["issuer"],
                                 "type": "GENERIC", "audience": "another", "mappings": []}]
        self.state_path.write_text(json.dumps(state))
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("existing M2M configuration", result.stderr)
        self.assertEqual(self.state()["m2mConfigs"], state["m2mConfigs"])
        self.assertFalse(any("monitoring-stack.yaml" in str(c) for c in self.state()["calls"]))

    def test_existing_identity_mapping_to_another_role_is_rejected(self):
        state = self.state()
        state["m2mConfigs"] = [{"id": "existing", "type": "KUBE_SERVICE_ACCOUNT",
                                 "issuer": state["issuer"], "audience": "central.stackrox.io",
                                 "mappings": [{"key": "sub", "valueExpression":
                                               "^system:serviceaccount:stackrox:sample-rhacs-prometheus$",
                                               "role": "None"}]}]
        self.state_path.write_text(json.dumps(state))
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rather than", result.stderr)
        self.assertEqual(self.state()["m2mConfigs"], state["m2mConfigs"])

    def test_preinstalled_metrics_role_is_reused(self):
        self.install_defaults()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(len(state["roles"]), 2)
        self.assertEqual(len(state["permissionSets"]), 1)
        self.assertEqual(len(state["accessScopes"]), 1)
        self.assertEqual([p["name"] for p in state["authProviders"]], [PROVIDER])

    def test_unrelated_certificate_provider_is_not_required_or_modified(self):
        state = self.state()
        state["authProviders"] = [{"id": "unrelated", "name": PROVIDER, "type": "oidc",
                                    "enabled": False}]
        self.state_path.write_text(json.dumps(state))
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()["authProviders"], state["authProviders"])

    def test_standalone_platform_certificate_script_remains_idempotent(self):
        for _ in range(2):
            result = self.run_access_control_setup()
            self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual([p["name"] for p in state["authProviders"]], [PROVIDER])
        self.assertEqual(self.mappings(), {(PLATFORM_PROMETHEUS, SCRAPE_ROLE)})
        self.assertEqual(state["m2mConfigs"], [])

    def test_missing_central_ocp_fails_before_installation(self):
        self.env["MOCK_NO_CENTRAL_OCP"] = "1"
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()["m2mConfigs"], [])
        self.assertFalse(any("apply" in call for call in self.state()["calls"]))

    def test_namespace_lookup_failure_stops_before_installation(self):
        self.env["FAIL_PROJECT"] = "1"
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()["calls"], [["oc", "project", "-q"]])

    def test_missing_api_token_stops_before_cluster_calls(self):
        del self.env["ROX_API_TOKEN"]
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ROX_API_TOKEN", result.stderr)
        self.assertEqual(self.state()["calls"], [])

    def test_api_authentication_failure_stops_before_installation(self):
        self.env["FAIL_HTTP"] = "1"
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("apply" in call for call in self.state()["calls"]))

    def test_m2m_creation_failure_does_not_install_monitoring_stack(self):
        self.env["FAIL_M2M"] = "1"
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()["m2mConfigs"], [])
        self.assertFalse(any("monitoring-stack.yaml" in str(c) for c in self.state()["calls"]))

    def test_creation_timeout_is_bounded(self):
        self.env.update(NEVER_CREATED="1", TIMEOUT="1")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timed out waiting for crd/", result.stderr)

    def test_service_ca_timeout_prevents_scrape_setup(self):
        self.env.update(NEVER_SERVICE_CA="1", TIMEOUT="1")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timed out waiting for openshift-service-ca.crt", result.stderr)
        self.assertFalse(any((yaml.safe_load(doc) or {}).get("kind") == "ScrapeConfig"
                             for doc in self.state()["applied"]))

    def test_custom_ca_is_used_for_all_central_api_calls(self):
        self.env["ROX_API_CA_FILE"] = str(self.authority / "ca.crt")
        for run in (self.run_setup, self.run_access_control_setup):
            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = [call for call in self.state()["calls"] if call[0] == "curl" and "-X" in call]
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(call[call.index("--cacert") + 1] == self.env["ROX_API_CA_FILE"]
                            for call in calls))

    def test_missing_custom_ca_fails_before_cluster_calls(self):
        self.env["ROX_API_CA_FILE"] = str(self.directory / "absent.crt")
        for run in (self.run_setup, self.run_access_control_setup):
            result = run()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ROX_API_CA_FILE", result.stderr)
        self.assertEqual(self.state()["calls"], [])

    def test_overlay_template_failure_does_not_install_the_scrape(self):
        command = self.bin / "envsubst"
        command.write_text("#!/bin/bash\nexit 1\n")
        command.chmod(0o755)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any((yaml.safe_load(doc) or {}).get("kind") == "ScrapeConfig"
                             for doc in self.state()["applied"]))

    def test_datasource_targets_this_stack(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        datasource = next(parsed for document in self.state()["applied"]
                          if (parsed := yaml.safe_load(document)).get("kind") == "PersesDatasource")
        url = datasource["spec"]["config"]["plugin"]["spec"]["proxy"]["spec"]["url"]
        self.assertEqual(url, "http://sample-rhacs-prometheus.stackrox.svc:9090")


if __name__ == "__main__":
    unittest.main()
