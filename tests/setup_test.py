"""Setup regression tests: real shell/crypto/JSON tools, simulated cluster and API."""

import base64
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
import base64, fcntl, json, os, pathlib, subprocess, sys, tempfile
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
                current[key] += value
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
    if args[0:2] == ["get", "configmap"]:
        assert namespace == "kube-system", namespace
        finish(output=os.environ["MOCK_CLIENT_CA"])
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
if name == "kubectl":
    if args[0:1] == ["-n"]:
        args = args[2:]
    if args[0] == "get" and args[1].startswith("secret/"):
        finish(output=json.dumps(state["secret"]) if state["secret"] else "")
    if args[0:2] == ["delete", "certificatesigningrequest"]:
        writes.append(("set", "request", None))
        finish()
    if args[0:2] == ["create", "-f"]:
        document = sys.stdin.read()
        request = [line.split(":", 1)[1].strip() for line in document.splitlines()
                   if line.strip().startswith("request:")]
        assert "signerName: kubernetes.io/kube-apiserver-client" in document, document
        writes.append(("set", "request", {"pem": request[0], "certificate": ""}))
        writes.append(("increment", "issued", 1))
        finish()
    if args[0:2] == ["certificate", "approve"]:
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            (directory / "request.pem").write_bytes(base64.b64decode(state["request"]["pem"]))
            # Sign with the same test CA the cluster publishes as its client CA.
            subprocess.run(
                ["openssl", "x509", "-req", "-in", str(directory / "request.pem"),
                 "-CA", os.environ["MOCK_CA_CERTIFICATE"], "-CAkey", os.environ["MOCK_CA_KEY"],
                 "-set_serial", str(state["issued"]), "-out", str(directory / "issued.pem"),
                 "-days", os.environ.get("MOCK_CERTIFICATE_DAYS", "30")],
                check=True, capture_output=True)
            issued = base64.b64encode((directory / "issued.pem").read_bytes()).decode()
        writes.append(("set", "request", dict(state["request"], certificate=issued)))
        finish()
    if args[0] == "get" and args[1].startswith("certificatesigningrequest/"):
        finish(output=state["request"]["certificate"])
    if args[0:3] == ["create", "secret", "tls"]:
        paths = {flag.split("=", 1)[0]: flag.split("=", 1)[1]
                 for flag in args if flag.startswith(("--cert=", "--key="))}
        finish(output=json.dumps({"data": {
            "tls.crt": base64.b64encode(pathlib.Path(paths["--cert"]).read_bytes()).decode(),
            "tls.key": base64.b64encode(pathlib.Path(paths["--key"]).read_bytes()).decode()}}))
    if args[0] == "apply" and args[-1] == "-":
        writes.append(("set", "secret", json.loads(sys.stdin.read())))
        finish()
    finish(1, "Unexpected kubectl operation")
if name == "curl":
    if args[0:2] == ["--help", "all"]:
        finish(output="--fail-with-body Fail on HTTP errors but save the body")
    assert {"--fail", "--fail-with-body"} & set(args), "HTTP failures must propagate"
    if os.environ.get("FAIL_HTTP"):
        finish(22, "HTTP 401")
    method = args[args.index("-X") + 1]
    path = next(arg for arg in args if arg.startswith("https://")).split("test", 1)[1]
    collections = {"/v1/authProviders": "authProviders", "/v1/permissionsets": "permissionSets",
                   "/v1/simpleaccessscopes": "accessScopes", "/v1/groups": "groups",
                   "/v1/roles": "roles"}
    if method == "GET":
        collection = collections[path]
        finish(output=json.dumps({collection: state[collection]}))
    if os.environ.get("FAIL_POST"):
        finish(22, "HTTP 403")
    body = json.loads(args[args.index("--data-binary") + 1])
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
        for command in ("bash", "openssl", "jq", "envsubst", "base64"):
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
        for command in ("oc", "kubectl", "curl", "sleep"):
            path = self.bin / command
            path.write_text(f"#!{sys.executable}\n{MOCK}")
            path.chmod(0o755)
        self.state_path = self.directory / "state.json"
        # RHACS before 5.1: no provider and none of its access control objects.
        self.state_path.write_text(json.dumps({
            "calls": [], "gets": {}, "applied": [], "secret": None, "request": None, "issued": 0,
            "authProviders": [], "permissionSets": [], "accessScopes": [], "groups": [],
            "roles": [{"name": "Admin"}],
        }))
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "MOCK_STATE": str(self.state_path),
            "MOCK_CA_CERTIFICATE": str(self.authority / "ca.crt"),
            "MOCK_CA_KEY": str(self.authority / "ca.key"),
            "MOCK_CLIENT_CA": (self.authority / "ca.crt").read_text(),
            "ROX_API_ENDPOINT": "https://central.example.test",
            "ROX_API_TOKEN": "test-token",
            "TIMEOUT": "10",
        }
        self.env.pop("NAMESPACE", None)
        self.env.pop("BASH_ENV", None)

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

    def certificate_subject(self, state=None):
        state = state or self.state()
        certificate = base64.b64decode(state["secret"]["data"]["tls.crt"])
        result = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-nameopt", "RFC2253"],
            input=certificate, capture_output=True, check=True)
        return result.stdout.decode().strip()

    def test_waits_for_creation_then_readiness_and_scopes_namespace(self):
        self.env["NAMESPACE"] = "custom-rhacs"
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(len(state["gets"]), 7)
        self.assertTrue(all(count == 3 for count in state["gets"].values()))
        for call in state["calls"]:
            if call[0] == "oc" and "apply" in call:
                if call[-1].endswith(("subscription.yaml", "ui-plugin.yaml")):
                    continue
                self.assertEqual(call[1:3], ["-n", "custom-rhacs"])
        self.assertEqual(
            self.certificate_subject(state),
            "subject=CN=system:serviceaccount:custom-rhacs:sample-rhacs-prometheus")

    def test_missing_provider_is_created_with_its_access_control_objects(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual([provider["name"] for provider in state["authProviders"]], [PROVIDER])
        provider = state["authProviders"][0]
        self.assertEqual(provider["type"], "userpki")
        # The provider has to trust the CA the cluster signs client certificates
        # with. Command substitution drops the bundle's trailing newline.
        self.assertEqual(provider["config"]["keys"], self.env["MOCK_CLIENT_CA"].rstrip("\n"))
        self.assertEqual([item["name"] for item in state["permissionSets"]], [SCRAPE_ROLE])
        self.assertEqual([item["name"] for item in state["accessScopes"]], [ACCESS_SCOPE])
        role = next(item for item in state["roles"] if item["name"] == SCRAPE_ROLE)
        self.assertEqual(role["permissionSetId"], state["permissionSets"][0]["id"])
        self.assertEqual(role["accessScopeId"], state["accessScopes"][0]["id"])
        self.assertEqual(self.mappings(state), {
            (PLATFORM_PROMETHEUS, SCRAPE_ROLE),
            ("system:serviceaccount:stackrox:sample-rhacs-prometheus", SCRAPE_ROLE),
        })

    def test_access_control_script_runs_standalone_and_is_idempotent(self):
        for _ in range(2):
            result = self.run_access_control_setup()
            self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(len(state["authProviders"]), 1)
        self.assertEqual(len(state["permissionSets"]), 1)
        self.assertEqual(len(state["accessScopes"]), 1)
        self.assertEqual([role["name"] for role in state["roles"]], ["Admin", SCRAPE_ROLE])
        self.assertEqual(self.mappings(state), {(PLATFORM_PROMETHEUS, SCRAPE_ROLE)})
        self.assertIsNone(state["secret"])
        self.assertFalse(any(call[0] == "oc" and "apply" in call for call in state["calls"]))

    def test_existing_provider_does_not_create_missing_default_objects(self):
        state = self.state()
        state["authProviders"] = [{
            "id": "installed-provider", "name": PROVIDER, "type": "userpki", "enabled": True,
            "config": {"keys": self.env["MOCK_CLIENT_CA"]},
        }]
        self.state_path.write_text(json.dumps(state))
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(state["permissionSets"], [])
        self.assertEqual(state["accessScopes"], [])
        self.assertEqual([role["name"] for role in state["roles"]], ["Admin"])
        self.assertEqual(self.mappings(state), {
            ("system:serviceaccount:stackrox:sample-rhacs-prometheus", SCRAPE_ROLE),
        })

    def test_standalone_script_leaves_existing_provider_untouched(self):
        self.install_defaults()
        before = self.state()
        result = self.run_access_control_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.state()
        for collection in ("authProviders", "permissionSets", "accessScopes", "roles", "groups"):
            self.assertEqual(after[collection], before[collection])
        self.assertFalse(any(call[0] == "curl" and "POST" in call for call in after["calls"]))

    def test_installed_objects_are_reused_and_only_the_stack_mapping_is_added(self):
        self.install_defaults()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        # Nothing RHACS installs itself may be duplicated or replaced.
        self.assertEqual([provider["id"] for provider in state["authProviders"]], ["installed-provider"])
        self.assertEqual([item["id"] for item in state["permissionSets"]], ["installed-permission-set"])
        self.assertEqual([item["id"] for item in state["accessScopes"]], ["installed-access-scope"])
        self.assertEqual([item["name"] for item in state["roles"]], ["Admin", SCRAPE_ROLE])
        self.assertEqual(self.mappings(state), {
            (PLATFORM_PROMETHEUS, SCRAPE_ROLE),
            ("system:serviceaccount:stackrox:sample-rhacs-prometheus", SCRAPE_ROLE),
        })
        self.assertFalse(any(call[0] == "curl" and "POST" in call and "/v1/authProviders" in call[-3:]
                             for call in state["calls"]))

    def scrape_config(self, state=None):
        state = state or self.state()
        for document in state["applied"]:
            parsed = yaml.safe_load(document)
            if parsed.get("kind") == "ScrapeConfig":
                return parsed
        self.fail("the setup applied no ScrapeConfig")

    def test_datasource_targets_the_stack_prometheus_not_the_shared_service(self):
        self.env["NAMESPACE"] = "custom-rhacs"
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        datasource = next(parsed for document in self.state()["applied"]
                          if (parsed := yaml.safe_load(document)).get("kind") == "PersesDatasource")
        url = datasource["spec"]["config"]["plugin"]["spec"]["proxy"]["spec"]["url"]
        # prometheus-operated governs the StatefulSet and selects every
        # Prometheus in the namespace; the stack's own service selects one.
        self.assertEqual(url, "http://sample-rhacs-prometheus.custom-rhacs.svc:9090")

    def test_scrape_uses_central_ocp_and_the_cluster_service_ca_when_published(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.scrape_config()["spec"]
        self.assertEqual(spec["staticConfigs"][0]["targets"], ["central-ocp.stackrox.svc:443"])
        # The cluster rotates this certificate and publishes its CA everywhere.
        self.assertEqual(spec["tlsConfig"]["ca"],
                         {"configMap": {"name": "openshift-service-ca.crt", "key": "service-ca.crt"}})

    def test_scrape_falls_back_to_central_and_the_stackrox_ca(self):
        """RHACS before 5.0 publishes no central-ocp service."""
        self.env["MOCK_NO_CENTRAL_OCP"] = "1"
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.scrape_config()["spec"]
        self.assertEqual(spec["staticConfigs"][0]["targets"], ["central.stackrox.svc:443"])
        self.assertEqual(spec["tlsConfig"]["ca"], {"secret": {"name": "central-tls", "key": "ca.pem"}})

    def test_scrape_always_presents_the_requested_client_certificate(self):
        for no_central_ocp in (None, "1"):
            with self.subTest(central_ocp=not no_central_ocp):
                self.setUp()
                if no_central_ocp:
                    self.env["MOCK_NO_CENTRAL_OCP"] = no_central_ocp
                self.assertEqual(self.run_setup().returncode, 0)
                tls = self.scrape_config()["spec"]["tlsConfig"]
                self.assertEqual(tls["cert"]["secret"]["name"], "sample-stackrox-prometheus-tls")
                self.assertEqual(tls["keySecret"]["name"], "sample-stackrox-prometheus-tls")

    def test_provider_trusting_another_client_ca_is_rejected(self):
        """A stale provider would reject every scrape without explaining why."""
        self.install_defaults()
        state = self.state()
        other = self.directory / "other-ca.crt"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "365",
             "-subj", "/CN=other-signer", "-keyout", str(self.directory / "other-ca.key"),
             "-out", str(other)], check=True, capture_output=True)
        state["authProviders"][0]["config"]["keys"] = other.read_text()
        self.state_path.write_text(json.dumps(state))
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusts none of the certificates", result.stderr)

    def test_identity_mapped_to_another_role_is_reported(self):
        """A second mapping for the same identity would silently grant nothing."""
        self.install_defaults()
        state = self.state()
        state["groups"].append({
            "props": {"authProviderId": "installed-provider", "key": "name",
                      "value": "system:serviceaccount:stackrox:sample-rhacs-prometheus"},
            "roleName": "None",
        })
        self.state_path.write_text(json.dumps(state))
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already mapped to the None role", result.stderr)

    def test_installed_role_is_reused_when_the_provider_is_missing(self):
        """An upgraded RHACS can serve the role while the provider is absent."""
        self.install_defaults(provider=False)
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual([item["id"] for item in state["permissionSets"]], ["installed-permission-set"])
        self.assertEqual([item["id"] for item in state["accessScopes"]], ["installed-access-scope"])
        self.assertEqual([item["name"] for item in state["roles"]], ["Admin", SCRAPE_ROLE])
        self.assertEqual([provider["name"] for provider in state["authProviders"]], [PROVIDER])
        self.assertEqual(self.mappings(state), {
            (PLATFORM_PROMETHEUS, SCRAPE_ROLE),
            ("system:serviceaccount:stackrox:sample-rhacs-prometheus", SCRAPE_ROLE),
        })

    def test_rerun_reuses_the_provider_mapping_and_certificate(self):
        for _ in range(2):
            result = self.run_setup()
            self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(len(state["authProviders"]), 1)
        self.assertEqual(len(state["permissionSets"]), 1)
        self.assertEqual(len(state["accessScopes"]), 1)
        self.assertEqual(len(state["groups"]), 2)
        self.assertEqual(state["issued"], 1, "the valid certificate was replaced")

    def test_expiring_certificate_is_renewed_in_place(self):
        self.env["MOCK_CERTIFICATE_DAYS"] = "1"
        self.assertEqual(self.run_setup().returncode, 0)
        expiring = self.state()["secret"]["data"]["tls.crt"]
        del self.env["MOCK_CERTIFICATE_DAYS"]
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(state["issued"], 2)
        self.assertNotEqual(state["secret"]["data"]["tls.crt"], expiring)
        self.assertEqual(
            self.certificate_subject(state),
            "subject=CN=system:serviceaccount:stackrox:sample-rhacs-prometheus")

    def test_namespace_too_long_for_a_common_name_stops_before_cluster_calls(self):
        self.env["NAMESPACE"] = "r" * 40
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("common name allows 64", result.stderr)
        self.assertEqual(self.state()["calls"], [])

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

    def test_provider_creation_failure_stops_setup(self):
        self.env["FAIL_POST"] = "1"
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("cluster-observability-operator/monitoring-stack.yaml" in call
                             for call in self.state()["calls"]))

    def test_creation_timeout_is_bounded(self):
        self.env.update(NEVER_CREATED="1", TIMEOUT="1")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timed out waiting for crd/", result.stderr)
        self.assertFalse(any("wait" in call for call in self.state()["calls"]))

    def test_template_failure_propagates_through_pipeline(self):
        command = self.bin / "envsubst"
        command.write_text("#!/bin/bash\nexit 1\n")
        command.chmod(0o755)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("rollout" in call for call in self.state()["calls"]))

    def test_unrelated_provider_is_not_overwritten(self):
        state = self.state()
        state["authProviders"] = [{"id": "oidc", "name": PROVIDER, "type": "oidc", "enabled": True}]
        self.state_path.write_text(json.dumps(state))
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an enabled user certificate provider", result.stderr)
        state = self.state()
        self.assertEqual([provider["type"] for provider in state["authProviders"]], ["oidc"])
        self.assertEqual(state["groups"], [])

    def test_certificate_is_not_installed_when_signing_fails(self):
        command = self.bin / "openssl"
        command.write_text("#!/bin/bash\nexit 1\n")
        command.chmod(0o755)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(self.state()["secret"])


if __name__ == "__main__":
    unittest.main()
