"""Exercise installed-setup smoke checks without cluster or network access."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().with_name("e2e.sh")
METRICS = {
    "rox_central_health_cluster_info",
    "rox_central_cfg_total_policies",
    "rox_central_cert_exp_hours",
}
MOCK = r'''
import json, os, pathlib, sys, time
tool, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
root = pathlib.Path(os.environ["SMOKE_MOCK_DIR"])
if tool == "sleep":
    time.sleep(0.05)
    sys.exit(0)
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps([tool, *args]) + "\n")
fixtures = json.loads((root / "fixtures.json").read_text())
if tool == "kubectl":
    if args[:2] == ["config", "current-context"]:
        print("mock-context")
    elif args[:2] == ["config", "view"]:
        print("stackrox")
    else:
        assert args[:4] == ["--context", "mock-context", "--namespace", "stackrox"], args
        args = args[4:]
        if args[0] == "port-forward":
            assert "pod/expected-pod" in args, args
            (root / "forward.pid").write_text(str(os.getpid()))
            print("Forwarding from 127.0.0.1:12345 -> 9090", flush=True)
            while True:
                time.sleep(60)
        elif args[:2] == ["rollout", "status"]:
            assert args[2] == fixtures["workload"], args
            print("StatefulSet rolled out")
        elif args[0] == "get":
            key = " ".join(args[1:args.index("-o")])
            print(json.dumps(fixtures["resources"][key]))
        else:
            raise AssertionError(args)
elif tool == "curl":
    assert "--fail" in args, args
    url = next(arg for arg in args if arg.startswith("http://"))
    assert url.startswith("http://127.0.0.1:12345/"), url
    if url.endswith("/-/ready"):
        data = {}
    elif "/api/v1/targets?" in url:
        data = {"activeTargets": fixtures["targets"]}
    elif url.endswith("/api/v1/query"):
        query = args[args.index("--data-urlencode") + 1]
        metric = query.removeprefix("query=").split("{")[0]
        data = {"resultType": "vector", "result": [] if metric == fixtures.get("missing_metric")
                else [{"metric": {}, "value": [123, "0"]}]}
    else:
        raise AssertionError(args)
    pathlib.Path(args[args.index("--output") + 1]).write_text(
        json.dumps({"status": "success", "data": data}))
else:
    raise AssertionError(tool)
'''


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("bash", "jq", "mktemp", "sed"):
            if not shutil.which(command):
                raise unittest.SkipTest(f"Missing test dependency: {command}")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.scratch = self.directory / "scratch"
        self.scratch.mkdir()
        for command in ("kubectl", "curl", "sleep"):
            executable = self.bin / command
            executable.write_text(f"#!{sys.executable}\n{MOCK}")
            executable.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "TMPDIR": str(self.scratch),
            "KUBECONFIG": str(self.directory / "unused-kubeconfig"),
            "SMOKE_MOCK_DIR": str(self.directory),
            "SMOKE_TIMEOUT_SECONDS": "2",
        }
        self.env.pop("BASH_ENV", None)

    def fixtures(self, path="coo"):
        coo = path == "coo"
        name = "sample-rhacs" if coo else "sample-stackrox-prometheus-server"
        group = "monitoring.rhobs" if coo else "monitoring.coreos.com"
        job = "scrapeConfig/stackrox/sample-stackrox-scrape-config/0" if coo else "sample-stackrox-metrics"
        resources = {
            f"prometheuses.{group} {name}": {"metadata": {"uid": "prometheus-uid"}},
            f"statefulset prometheus-{name}": {
                "metadata": {"uid": "sts-uid", "ownerReferences": [{"uid": "prometheus-uid", "kind": "Prometheus"}]},
                "spec": {"replicas": 1},
            },
            "pods": {"items": [
                {"metadata": {"name": pod, "ownerReferences": [{"uid": owner}]},
                 "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
                for pod, owner in (("unrelated-pod", "unrelated"), ("expected-pod", "sts-uid"))
            ]},
        }
        for kind in ("datasource", "dashboard"):
            resources[f"perses{kind}s.perses.dev sample-stackrox-{kind}"] = {
                "metadata": {"generation": 2},
                "status": {"conditions": [
                    {"type": "Available", "status": "True", "observedGeneration": 2},
                    {"type": "Degraded", "status": "False"},
                ]},
            }
        return {
            "workload": f"statefulset/prometheus-{name}",
            "resources": resources,
            "targets": [{"scrapeUrl": "https://central.stackrox.svc:443/metrics", "health": "up",
                         "lastError": "", "labels": {"job": job, "instance": "central:443"}}],
        }

    def run_smoke(self, args, fixtures):
        (self.directory / "fixtures.json").write_text(json.dumps(fixtures))
        log = self.directory / "calls.jsonl"
        log.unlink(missing_ok=True)
        pidfile = self.directory / "forward.pid"
        pidfile.unlink(missing_ok=True)
        with subprocess.Popen(
            ["bash", str(SCRIPT), *args], cwd=self.directory, env=self.env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=15)
                if pidfile.exists():
                    with self.assertRaises(ProcessLookupError, msg="port-forward was not stopped"):
                        os.kill(int(pidfile.read_text()), 0)
                self.assertEqual(list(self.scratch.iterdir()), [], "smoke temporary files leaked")
            finally:
                # Also clean up descendants if the script regresses or times out.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr), calls

    def test_both_paths_query_fixed_metrics_from_the_selected_target(self):
        for path in ("coo", "prometheus-operator"):
            with self.subTest(path=path):
                fixtures = self.fixtures(path)
                # Exercise both the context namespace and an explicit namespace.
                args = [path] if path == "coo" else [path, "stackrox"]
                result, calls = self.run_smoke(args, fixtures)
                self.assertEqual(result.returncode, 0, result.stderr)
                queries = [call[call.index("--data-urlencode") + 1]
                           for call in calls if "--data-urlencode" in call]
                self.assertEqual({q.split("{")[0].removeprefix("query=") for q in queries}, METRICS)
                job = fixtures["targets"][0]["labels"]["job"]
                for query in queries:
                    self.assertIn(f'job="{job}"', query)
                    self.assertIn('instance="central:443"', query)
                perses_calls = [call for call in calls if any(arg.startswith("perses") for arg in call)]
                self.assertEqual(len(perses_calls), 2 if path == "coo" else 0)

    def test_unhealthy_target_fails_and_cleans_up(self):
        fixtures = self.fixtures()
        fixtures["targets"][0].update(health="down", lastError="401 Unauthorized")
        result, calls = self.run_smoke(["coo"], fixtures)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("401 Unauthorized", result.stderr)
        self.assertFalse(any("--data-urlencode" in call for call in calls))

    def test_missing_fixed_metric_fails_and_cleans_up(self):
        fixtures = self.fixtures("prometheus-operator")
        fixtures["missing_metric"] = "rox_central_cert_exp_hours"
        result, _ = self.run_smoke(["prometheus-operator"], fixtures)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing fixed metric rox_central_cert_exp_hours", result.stderr)

    def test_unavailable_perses_fails_and_cleans_up(self):
        fixtures = self.fixtures()
        dashboard = fixtures["resources"]["persesdashboards.perses.dev sample-stackrox-dashboard"]
        dashboard["status"]["conditions"][0]["status"] = "False"
        result, _ = self.run_smoke(["coo"], fixtures)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Perses reconciliation not confirmed", result.stderr)

    def test_wrong_workload_owner_fails_before_port_forward(self):
        fixtures = self.fixtures()
        statefulset = fixtures["resources"]["statefulset prometheus-sample-rhacs"]
        statefulset["metadata"]["ownerReferences"][0]["uid"] = "unrelated-prometheus"
        result, calls = self.run_smoke(["coo"], fixtures)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owned by", result.stderr)
        self.assertFalse(any("port-forward" in call or call[0] == "curl" for call in calls))

    def test_no_or_invalid_arguments_make_no_cluster_calls(self):
        for args in ([], ["invalid"], ["coo", "stackrox", "unexpected"]):
            with self.subTest(args=args):
                result, calls = self.run_smoke(args, self.fixtures())
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Usage:", result.stderr)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
