"""Small offline checks for credentials, limited mutations, and deployment retries."""

import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("deploy", ROOT / "cluster/ssc-demo-deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeploymentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(deploy, "run", side_effect=AssertionError("Unexpected command")))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.instance = object.__new__(deploy.Deployment)
        self.instance.args = SimpleNamespace(timeout=30)
        self.instance.config = {"gitops": {"repoUrl": "https://example.test/demo.git"}}
        self.instance.source_revision = "fixed-revision"
        self.instance.source = ROOT
        self.instance.ns = "tssc-app-ci"
        self.instance.domain = "apps.example.test"
        self.instance.components = {
            "gitlab": {"enabled": True, "namespace": "gitlab"},
            "quay": {"enabled": True, "organization": "tssc"},
            "rhtpaOperator": {"enabled": True, "namespace": "tpa"}}
        self.instance.rhtpa_cpu_request = "100m"
        self.instance.get = Mock(side_effect=AssertionError("Unexpected resource query"))
        self.instance.oc = Mock(side_effect=AssertionError("Unexpected mutation"))

    def test_existing_credentials_are_reused_but_never_rotated(self):
        self.instance.get = Mock(return_value={"data": {"password": base64.b64encode(b"original").decode()}})
        self.assertEqual(self.instance.secret("demo", "ns", {"password": "original"}), {"password": "original"})
        with self.assertRaisesRegex(deploy.DeploymentError, "refusing credential rotation"):
            self.instance.secret("demo", "ns", {"password": "replacement"})
        self.instance.oc.assert_not_called()

    def test_unchanged_manifests_are_not_reapplied(self):
        desired = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "demo", "namespace": "ns"},
                   "data": {"key": "value"}}
        existing = copy.deepcopy(desired)
        existing["metadata"]["resourceVersion"] = "42"
        self.instance.get = Mock(return_value=existing)
        self.instance.apply([desired])
        self.instance.oc.assert_not_called()
        self.instance.get.return_value = None
        self.instance.oc = Mock()
        self.instance.apply([desired])
        self.assertEqual(json.loads(self.instance.oc.call_args.kwargs["data"])["items"], [desired])

    def test_completed_app_is_reused_failed_app_retries_and_active_app_resumes(self):
        app = {"metadata": {"name": "demo", "namespace": "argo"}, "spec": {
            "source": {"repoURL": self.instance.config["gitops"]["repoUrl"], "targetRevision": "main"}}}
        for phase, active, mutations in (("Succeeded", False, 0), ("Failed", False, 1), ("Error", False, 1),
                                         ("Failed", True, 0), ("Running", False, 0)):
            with self.subTest(phase=phase, active=active):
                live = dict(copy.deepcopy(app), status={"sync": {"revision": "fixed-revision"},
                                                       "operationState": {"phase": phase}})
                if active:
                    live["operation"] = {"sync": {}}
                self.instance.get = Mock(return_value=live)
                self.instance.oc = Mock()
                self.instance.ensure_app(app)
                self.assertEqual(self.instance.oc.call_count, mutations)
                if mutations:
                    operation = json.loads(self.instance.oc.call_args.args[-1])["operation"]
                    self.assertEqual(operation["sync"], {"revision": "fixed-revision", "prune": False})

    def test_pending_retry_does_not_fail_on_old_operation_status(self):
        app = {"metadata": {"name": "demo", "namespace": "argo"}, "spec": {}}
        states = [{"operation": {"sync": {}}, "status": {"operationState": {"phase": "Failed"}}},
                  {"status": {"operationState": {"phase": "Succeeded"}, "sync": {"status": "Synced"}}}]
        self.instance.get = Mock(side_effect=states)
        self.instance.fail_if_unschedulable = Mock()
        with patch.object(deploy.time, "sleep"):
            self.instance.wait_sync(app)
        self.assertEqual(self.instance.get.call_count, 2)

    def test_cpu_budget_reuses_capacity_of_existing_rhtpa_pods(self):
        node = {"metadata": {"name": "worker", "labels": {"node-role.kubernetes.io/worker": ""}},
                "status": {"allocatable": {"cpu": "2"}, "conditions": [{"type": "Ready", "status": "True"}]}}
        pod = {"metadata": {"name": "server-old", "namespace": "tpa"},
               "spec": {"nodeName": "worker", "containers": [{"resources": {"requests": {"cpu": "1"}}}]}}
        self.assertEqual(self.instance.select_rhtpa_cpu_request([node], [pod]), "1")
        pod["metadata"]["namespace"] = "another-app"
        self.assertEqual(self.instance.select_rhtpa_cpu_request([node], [pod]), "100m")

    def test_cpu_patch_is_skipped_when_correct_and_only_changes_two_requests(self):
        self.instance.get = Mock(return_value={"spec": {"modules": {
            name: {"resources": {"requests": {"cpu": "0.1"}}} for name in ("server", "importer")}}})
        self.instance.patch_rhtpa_cpu_requests()
        self.instance.oc.assert_not_called()
        self.instance.get.return_value = {"spec": {}}
        self.instance.oc = Mock()
        self.instance.patch_rhtpa_cpu_requests()
        self.assertEqual(json.loads(self.instance.oc.call_args.args[-1]), {"spec": {"modules": {
            name: {"resources": {"requests": {"cpu": "100m"}}} for name in ("server", "importer")}}})

    def test_correct_ca_configuration_does_not_restart_server(self):
        self.instance.get = Mock(return_value={"spec": {
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
            "template": {"spec": {"containers": [{"name": "server", "env": [
                {"name": "SSL_CERT_FILE", "value": "/etc/ssc-router-ca/ca-bundle.crt"}], "volumeMounts": [
                {"name": "ssc-router-ca", "mountPath": "/etc/ssc-router-ca", "readOnly": True}]}],
                "volumes": [{"name": "ssc-router-ca", "configMap": {"name": "ssc-router-ca", "defaultMode": 420}}]}}}})
        self.instance.oc = Mock()
        self.instance.configure_rhtpa_server_ca()
        self.instance.oc.assert_called_once()
        self.assertIn("rollout", self.instance.oc.call_args.args)

    def test_remediation_loader_reuses_verified_data_and_only_uploads_after_complete_check(self):
        self.instance.ingress_ca_pem = "-----BEGIN CERTIFICATE-----\npublic-ca\n-----END CERTIFICATE-----\n"
        self.instance.route_forward = (SimpleNamespace(poll=lambda: None), 63179, None)
        self.instance.get = Mock(return_value={"data": {
            "bombastic_api_url": base64.b64encode(
                b"https://server-trusted-profile-analyzer.apps.example.test").decode(),
            "oidc_issuer_url": base64.b64encode(
                b"https://sso.apps.example.test/realms/backstage").decode()}})

        cases = (("verified", 0, "", False),
                 ("incomplete", 1, "Verification FAILED -- see [FAIL] lines above.", True),
                 ("api-error", 6, "", False))
        with patch.object(deploy.shutil, "which", return_value="/usr/bin/curl"):
            for name, returncode, stdout, should_upload in cases:
                with self.subTest(result=name), patch.object(deploy, "run") as load:
                    def verify(command, *, text, capture_output, check, timeout, env):
                        self.assertEqual(command[-1], "--verify")
                        bin_dir = Path(env["PATH"].split(os.pathsep)[0])
                        wrapper = (bin_dir / "curl").read_text()
                        ca_bundle = Path(env["CURL_CA_BUNDLE"])
                        self.assertTrue(ca_bundle.is_file())
                        self.assertEqual(ca_bundle.read_text(), self.instance.ingress_ca_pem)
                        self.assertIn("--cacert " + str(ca_bundle), wrapper)
                        self.assertIn("--connect-to server-trusted-profile-analyzer.apps.example.test:443:127.0.0.1:63179", wrapper)
                        self.assertIn("--connect-to sso.apps.example.test:443:127.0.0.1:63179", wrapper)
                        self.assertIn("server-trusted-profile-analyzer.apps.example.test", env["NO_PROXY"])
                        self.assertIn("sso.apps.example.test", env["NO_PROXY"])
                        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

                    with patch.object(deploy.subprocess, "run", side_effect=verify) as check:
                        if name == "api-error":
                            with self.assertRaisesRegex(deploy.DeploymentError, "upload was not attempted"):
                                self.instance.load_remediation_data()
                        else:
                            self.instance.load_remediation_data()
                    check.assert_called_once()
                    if should_upload:
                        load.assert_called_once()
                        self.assertNotIn("--verify", load.call_args.args[0])
                    else:
                        load.assert_not_called()

    def test_smoke_run_reuses_success_or_in_progress_and_retries_failure(self):
        for status in ("True", "Unknown", "False"):
            with self.subTest(status=status):
                previous = {"metadata": {"name": "previous", "creationTimestamp": "2026-01-01"},
                            "status": {"conditions": [{"type": "Succeeded", "status": status}]}}
                self.instance.oc = Mock(side_effect=[json.dumps({"items": [previous]}),
                                                     json.dumps({"metadata": {"name": "retry"}})])
                self.instance.get = Mock(return_value={"status": {"conditions": [{"type": "Succeeded", "status": "True"}]}})
                self.instance.smoke_test()
                self.assertEqual(self.instance.oc.call_count, 2 if status == "False" else 1)
                if status == "False":
                    self.assertEqual(self.instance.oc.call_args.args[:2], ("create", "-f"))

    def test_cleanup_inventory_is_read_only_protects_shared_resources_and_omits_specs(self):
        self.instance.override = {"deployer": {"domain": "apps.example.test"}}

        def resource(kind, name, namespace=None):
            return {"kind": kind, "metadata": {"name": name, "namespace": namespace, "uid": "uid-" + name}}

        apps = []
        for name, namespace in (("ssc-gitlab", "gitlab"), ("ssc-keycloak", "keycloak"),
                                ("ssc-orchestrator", "automation-orchestrator-operator-system")):
            app = resource("Application", name, "openshift-gitops")
            app["metadata"].update(labels={"app.kubernetes.io/part-of": "ssc-demo"},
                                   finalizers=["resources-finalizer.argocd.argoproj.io"])
            app["spec"] = {"destination": {"namespace": namespace, "server": "https://kubernetes.default.svc"},
                           "source": {"helm": {"valuesObject": {"password": "do-not-output"}}}}
            apps.append(app)

        def get(kind, name=None, namespace=None, all_namespaces=False):
            if kind == "ingresses.config":
                return {"spec": {"domain": "apps.example.test"}}
            if kind == "crd":
                return resource("CustomResourceDefinition", name)
            if kind == "applications":
                return {"items": apps}
            if kind == "namespace":
                return resource("Namespace", name) if name in (
                    "gitlab", "keycloak", "automation-orchestrator-operator-system") else None
            if kind in ("persistentvolumeclaims", "objectbucketclaims") and namespace == "gitlab":
                return {"items": [resource(kind, "demo-data", namespace)]}
            if kind == "clusterroles,clusterrolebindings,securitycontextconstraints":
                return {"items": [resource("SecurityContextConstraints", "artifactory-scc")]}
            if kind == "subscriptions.operators.coreos.com" and all_namespaces:
                return {"items": [resource("Subscription", "shared-operator", "openshift-operators")]}
            raise AssertionError(f"Unexpected inventory request: {kind}/{name}/{namespace}")

        self.instance.get = Mock(side_effect=get)
        self.instance.oc = Mock(side_effect=lambda *args: "kube:admin" if args == ("whoami",) else
                                "https://api.example.test:6443" if args == ("whoami", "--show-server") else
                                self.fail("Cleanup attempted a mutation"))
        report = self.instance.cleanup_plan()
        self.assertFalse(report["deletionEnabled"])
        applications = {row["name"]: row for row in report["applications"]}
        self.assertEqual(applications["ssc-gitlab"]["decision"], "REVIEW")
        self.assertTrue(applications["ssc-gitlab"]["cascadeDeletion"])
        for name in ("ssc-keycloak", "ssc-orchestrator"):
            self.assertEqual(applications[name]["decision"], "KEEP")
        namespaces = {row["name"]: row for row in report["namespaces"]}
        self.assertEqual(namespaces["keycloak"]["decision"], "KEEP")
        self.assertEqual(namespaces["gitlab"]["persistentVolumeClaims"][0]["name"], "demo-data")
        self.assertEqual(report["clusterResources"][0]["decision"], "KEEP")
        self.assertEqual(report["operatorSubscriptions"][0]["decision"], "KEEP")
        self.assertNotIn("do-not-output", json.dumps(report))

    def test_cleanup_inventory_rejects_wrong_cluster_before_scanning(self):
        self.instance.override = {"deployer": {"domain": "apps.example.test"}}
        self.instance.oc = Mock(return_value="kube:admin")
        self.instance.get = Mock(return_value={"spec": {"domain": "apps.other.test"}})
        with self.assertRaisesRegex(deploy.DeploymentError, "does not match"):
            self.instance.cleanup_plan()
        self.instance.get.assert_called_once_with("ingresses.config", "cluster")
        self.instance.oc.assert_called_once_with("whoami")

    def test_cleanup_inventory_handles_an_install_with_no_gitops_or_demo_namespaces(self):
        self.instance.override = {"deployer": {"domain": "apps.example.test"}}
        self.instance.oc = Mock(return_value="https://api.example.test:6443")
        self.instance.get = Mock(side_effect=lambda kind, *args, **kwargs:
                                 {"spec": {"domain": "apps.example.test"}} if kind == "ingresses.config" else
                                 {"items": []} if kind == "clusterroles,clusterrolebindings,securitycontextconstraints" else None)
        report = self.instance.cleanup_plan()
        self.assertEqual(report["applications"], [])
        self.assertEqual(report["namespaces"], [])
        self.assertEqual(report["operatorSubscriptions"], [])
        self.assertFalse(report["deletionEnabled"])

    def test_cleanup_mode_never_enters_deployment_or_fetches_source(self):
        with patch.object(deploy, "Deployment") as constructor:
            instance = constructor.return_value
            instance.cleanup_plan.return_value = {"deletionEnabled": False}
            self.assertEqual(deploy.main(["--cleanup-plan", "--values", "/not-read.yaml"]), 0)
        instance.cleanup_plan.assert_called_once_with()
        instance.prepare_source.assert_not_called()
        instance.deploy.assert_not_called()
        instance.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
