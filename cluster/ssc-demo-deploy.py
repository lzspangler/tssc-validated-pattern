#!/usr/bin/env python3
"""Optional, resumable coordinator for the existing fresh-cluster runbook."""

import argparse
import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

try:
    import yaml
except ImportError:
    sys.exit("Install local dependencies first: python3 -m pip install -r cluster/requirements.txt")


ROOT = Path(__file__).resolve().parents[1]
PASSWORDS = ("keycloakDbPassword", "gitlabRootPassword", "tpaDbPassword",
             "orchestratorDbPassword", "tpaCliClientSecret", "quayAdminPassword")
AF_PASSWORDS = {"adminPassword": 20, "masterKey": 32, "joinKey": 16,
                "postgresPassword": 16, "appPassword": 16}


class DeploymentError(Exception):
    pass


class RouteResolutionError(DeploymentError):
    pass


class RouteTLSVerificationError(DeploymentError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalTunnelHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a local oc port-forward while validating the real route host."""

    def connect(self):
        sock = socket.create_connection(("localhost", self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


def load_yaml(path):
    try:
        with Path(path).open() as stream:
            value = yaml.safe_load(stream)
        require(isinstance(value, dict), f"Expected a YAML mapping: {path}")
        return value
    except (OSError, yaml.YAMLError):
        raise DeploymentError(f"Cannot read YAML: {path}; contents withheld") from None


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def safe_url(value):
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname and not (
        parsed.username or parsed.password or parsed.query or parsed.fragment)


def valid_domain(value):
    return isinstance(value, str) and len(value) <= 253 and "." in value and all(
        re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
        for label in value.split("."))


def cpu_millicores(value, *, allow_zero=False):
    """Parse Kubernetes CPU quantities in cores or millicores."""
    require(not isinstance(value, bool), "RHTPA CPU requests must be positive CPU quantities")
    text = str(value)
    try:
        amount = Decimal(text[:-1]) if text.endswith("m") else Decimal(text) * 1000
    except InvalidOperation:
        raise DeploymentError("RHTPA CPU requests must be positive CPU quantities") from None
    require((amount >= 0 if allow_zero else amount > 0) and amount == amount.to_integral_value(),
            "RHTPA CPU requests must be positive whole millicores")
    return int(amount)


def pod_cpu_millicores(pod):
    spec = pod.get("spec", {})

    def requests(containers):
        return sum(cpu_millicores(container.get("resources", {}).get("requests", {}).get("cpu", 0), allow_zero=True)
                   for container in containers)

    app_request = requests(spec.get("containers", []))
    init_request = max((requests([container]) for container in spec.get("initContainers", [])), default=0)
    overhead = cpu_millicores(spec.get("overhead", {}).get("cpu", 0), allow_zero=True)
    return max(app_request, init_request) + overhead


def can_place_equal_pods(headroom, request, count=2):
    remaining = sorted(headroom, reverse=True)
    for _ in range(count):
        if not remaining or remaining[0] < request:
            return False
        remaining[0] -= request
        remaining.sort(reverse=True)
    return True


def rhtpa_cpu_headroom(nodes, pods):
    """Return worker CPU headroom after accounting for bound, non-terminal pods."""
    workers = {}
    for node in nodes:
        metadata = node.get("metadata", {})
        labels = metadata.get("labels", {})
        if "node-role.kubernetes.io/worker" not in labels:
            continue
        conditions = node.get("status", {}).get("conditions", [])
        if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            continue
        if node.get("spec", {}).get("unschedulable"):
            continue
        if any(taint.get("effect") in ("NoSchedule", "NoExecute")
               for taint in node.get("spec", {}).get("taints", [])):
            continue
        allocatable = node.get("status", {}).get("allocatable", {}).get("cpu")
        require(allocatable is not None, f"Worker {metadata.get('name', 'unknown')} has no allocatable CPU value")
        workers[metadata["name"]] = cpu_millicores(allocatable)

    for pod in pods:
        node_name = pod.get("spec", {}).get("nodeName")
        if node_name not in workers or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        workers[node_name] -= pod_cpu_millicores(pod)
    return [max(0, amount) for amount in workers.values()]


def run(args, *, data=None, timeout=120, env=None):
    """Never echo subprocess output: rendered Applications contain credentials."""
    try:
        result = subprocess.run(args, input=data, text=True, capture_output=True,
                                check=False, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise DeploymentError(f"{Path(args[0]).name} could not complete within {timeout}s; check installation/connectivity") from None
    require(result.returncode == 0,
            f"{Path(args[0]).name} failed (exit {result.returncode}); output withheld to protect credentials")
    return result.stdout


def init_values(args):
    target = args.values.expanduser().resolve()
    require(not target.is_relative_to(ROOT), "Keep private values outside this checkout")
    require(args.repo_url and safe_url(args.repo_url), "--repo-url must be a credential-free HTTPS URL")
    require(args.revision, "--revision is required (a remote branch or tag)")
    require(valid_domain(args.domain), "--domain must be the cluster apps subdomain")
    require(target.parent.is_dir(), "Create a private parent directory for --values first")
    values = {"deployer": {"domain": args.domain},
              "gitops": {"repoUrl": args.repo_url, "revision": args.revision},
              "credentials": {name: secrets.token_hex(16) for name in PASSWORDS},
              "deployment": {"artifactory": {name: secrets.token_hex(size) for name, size in AF_PASSWORDS.items()}}}
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            yaml.safe_dump(values, stream, sort_keys=False)
    except FileExistsError:
        raise DeploymentError("Values file already exists; refusing to regenerate credentials") from None
    print(f"Created private values: {target}. Preserve this file for every rerun.")


class Deployment:
    def __init__(self, args):
        self.args = args
        self.values = args.values.expanduser().resolve()
        require(not self.values.is_relative_to(ROOT), "Private values must be outside this checkout")
        require(self.values.is_file(), "Missing private --values file; run --init-values first")
        require(self.values.stat().st_mode & 0o077 == 0, "Private values must have mode 600 (or stricter)")
        self.override = load_yaml(self.values)
        self.config = None
        self.apps = []
        self.source_revision = None
        self.route_forward = None
        self.skip_quay_noobaa_preflight = False
        self.keycloak_route_override = False
        self.rhtpa_cpu_request = None
        self.rhtpa_hook_ca_enabled = False

    def oc(self, *args, data=None, timeout=45):
        return run(["oc", "--request-timeout=30s", *args], data=data, timeout=timeout)

    def get(self, resource, name=None, namespace=None, all_namespaces=False):
        if resource != "namespace" and namespace:
            if not self.oc("get", "namespace", namespace, "--ignore-not-found", "-o", "json").strip():
                return None
        args = (["-n", namespace] if namespace else []) + ["get", resource]
        if all_namespaces:
            require(not namespace, "A namespaced resource query cannot select one namespace and all namespaces")
            args.append("--all-namespaces")
        if name:
            args += [name, "--ignore-not-found"]
        text = self.oc(*args, "-o", "json")
        return json.loads(text) if text.strip() else None

    def apply(self, documents):
        documents = [doc for doc in documents if not self.contains(
            self.get(doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace")), doc)]
        if documents:
            self.oc("apply", "-f", "-", data=json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}))

    def wait(self, description, check):
        print(f"Waiting up to {self.args.timeout}s: {description}")
        deadline = time.monotonic() + self.args.timeout
        while True:
            if check():
                print(f"Ready: {description}")
                return
            remaining = deadline - time.monotonic()
            require(remaining > 0, f"Timed out waiting for {description}; inspect the named resource before rerunning")
            time.sleep(min(10, remaining))

    def wait_app(self, app):
        name, namespace = app["metadata"]["name"], app["metadata"]["namespace"]

        def ready():
            live = self.get("application", name, namespace) or {}
            status = live.get("status", {})
            operation = status.get("operationState", {})
            if operation.get("phase") in ("Failed", "Error") and not live.get("operation"):
                raise DeploymentError(f"Application {namespace}/{name} sync failed; inspect its Argo sync operation")
            revision = getattr(self, "source_revision", None)
            source = app.get("spec", {}).get("source", {})
            from_selected_repo = self.config and source.get("repoURL") == self.config["gitops"]["repoUrl"]
            synced = (status.get("sync", {}).get("status") == "Synced" and status.get("health", {}).get("status") == "Healthy"
                      and operation.get("phase") == "Succeeded" and not live.get("operation")
                      and (not revision or not from_selected_repo or status.get("sync", {}).get("revision") == revision))
            if not synced:
                self.fail_if_unschedulable()
            return synced

        self.wait(f"Application {namespace}/{name} Synced, Healthy, and sync operation Succeeded", ready)

    def wait_sync(self, app):
        """Wait until Argo applied one Application, without blocking on cross-app Jobs."""
        name, namespace = app["metadata"]["name"], app["metadata"]["namespace"]

        def synced():
            live = self.get("application", name, namespace) or {}
            status = live.get("status", {})
            operation = status.get("operationState", {})
            if operation.get("phase") in ("Failed", "Error") and not live.get("operation"):
                raise DeploymentError(f"Application {namespace}/{name} sync failed; inspect its Argo sync operation")
            revision = getattr(self, "source_revision", None)
            source = app.get("spec", {}).get("source", {})
            from_selected_repo = self.config and source.get("repoURL") == self.config["gitops"]["repoUrl"]
            applied = (status.get("sync", {}).get("status") == "Synced"
                       and operation.get("phase") == "Succeeded" and not live.get("operation")
                       and (not revision or not from_selected_repo or status.get("sync", {}).get("revision") == revision))
            if not applied:
                self.fail_if_unschedulable()
                if (name == "ssc-rhtpa-prerequisites" and operation.get("phase") == "Running"
                        and not self.rhtpa_hook_ca_enabled):
                    self.complete_verified_keycloak_wait()
            return applied

        self.wait(f"Application {namespace}/{name} sync applied", synced)

    def complete_verified_keycloak_wait(self):
        """Substitute a verified route probe when UBI lacks this cluster's ingress CA."""
        namespace = self.components["rhtpaPrerequisites"]["namespace"]
        job = self.get("job", "wait-for-keycloak", namespace)
        if not job or job.get("status", {}).get("succeeded", 0):
            return
        status = job.get("status", {})
        if status.get("failed", 0) or not status.get("active", 0):
            return
        issuer = f"https://sso.{self.domain}/realms/{self.components['keycloak']['realm']}"
        discovery = self.route_json(Request(issuer + "/.well-known/openid-configuration"))
        if not discovery:
            return
        require(discovery.get("issuer") == issuer,
                "OIDC discovery returned an issuer different from the configured realm")
        endpoint = urlparse(discovery.get("token_endpoint", ""))
        require(endpoint.scheme == "https" and endpoint.hostname == urlparse(issuer).hostname,
                "OIDC token endpoint must use the verified Keycloak HTTPS route")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        condition = {"type": "Complete", "status": "True", "reason": "CoordinatorVerified",
                     "message": "OIDC discovery was verified against the cluster ingress certificate",
                     "lastTransitionTime": now}
        criteria = dict(condition, type="SuccessCriteriaMet")
        self.oc("-n", namespace, "patch", "job", "wait-for-keycloak", "--subresource=status",
                "--type=merge", "-p", json.dumps({"status": {
                    "active": 0, "ready": 0, "succeeded": 1, "completionTime": now,
                    "conditions": [criteria, condition]}}))
        self.oc("-n", namespace, "delete", "pods", "-l", "job-name=wait-for-keycloak",
                "--wait=false", "--ignore-not-found=true")
        print("OIDC readiness verified with the trusted ingress certificate; completed the in-cluster wait hook because its UBI image does not trust the self-signed route CA.")

    def fail_if_unschedulable(self):
        for pod in (self.get("pods", all_namespaces=True) or {}).get("items", []):
            if pod.get("status", {}).get("phase") != "Pending":
                continue
            condition = next((c for c in pod.get("status", {}).get("conditions", [])
                              if c.get("type") == "PodScheduled" and c.get("status") == "False"), {})
            if condition.get("reason") == "Unschedulable":
                namespace = pod.get("metadata", {}).get("namespace", "unknown")
                if (self.rhtpa_cpu_request and namespace == self.components["rhtpaOperator"]["namespace"]
                        and pod.get("metadata", {}).get("name", "").startswith(("server-", "importer-"))):
                    continue
                raise DeploymentError(
                    f"Unschedulable pod {namespace}/{pod['metadata']['name']}: "
                    f"{condition.get('message', 'insufficient schedulable resources')}. "
                    "Deployment stopped before later components; no existing resources were deleted or downsized. "
                    "Inspect node requests/events and add capacity or resolve the constraint before rerunning.")

    def select_rhtpa_cpu_request(self, nodes, pods):
        scheduling = self.components["rhtpaOperator"].get("scheduling", {})
        preferred = scheduling.get("preferredCpuRequest", "1")
        fallback = scheduling.get("fallbackCpuRequest", "100m")
        preferred_m = cpu_millicores(preferred)
        fallback_m = cpu_millicores(fallback)
        require(fallback_m <= preferred_m,
                "components.rhtpaOperator.scheduling.fallbackCpuRequest must not exceed preferredCpuRequest")
        namespace = self.components["rhtpaOperator"]["namespace"]
        # Budget replacements, not two additional copies of the existing workloads.
        other_pods = [pod for pod in pods if not (
            pod.get("metadata", {}).get("namespace") == namespace
            and pod.get("metadata", {}).get("name", "").startswith(("server-", "importer-")))]
        headroom = rhtpa_cpu_headroom(nodes, other_pods)
        if can_place_equal_pods(headroom, preferred_m):
            return str(preferred)
        require(can_place_equal_pods(headroom, fallback_m),
                "RHTPA server/importer CPU requests cannot fit on schedulable workers, even at the configured fallback; add capacity or adjust the fallback deliberately")
        print(f"RHTPA worker CPU headroom requires the configured fallback request ({fallback}); upstream request ({preferred}) is preserved when it fits.")
        return str(fallback)

    def protect_rhtpa_cpu_requests(self, app):
        namespace = self.components["rhtpaOperator"]["namespace"]
        ignores = app["spec"].setdefault("ignoreDifferences", [])
        entries = [
            {"group": "rhtpa.io", "kind": "TrustedProfileAnalyzer",
             "name": "trusted-profile-analyzer", "namespace": namespace,
             "jsonPointers": ["/spec/modules/server/resources/requests/cpu",
                              "/spec/modules/importer/resources/requests/cpu"]},
        ]
        if self.components["rhtpaOperator"].get("serverCaConfigMap", "ssc-router-ca"):
            entries.append({"group": "apps", "kind": "Deployment", "name": "server", "namespace": namespace,
                            "jsonPointers": ["/spec/template/spec/containers/0/env",
                                             "/spec/template/spec/containers/0/volumeMounts",
                                             "/spec/template/spec/volumes", "/spec/strategy"]})
        for entry in entries:
            target = next((item for item in ignores if all(item.get(key) == entry[key]
                           for key in ("group", "kind", "name", "namespace"))), None)
            if target is None:
                ignores.append(entry)
            else:
                target["jsonPointers"] = list(dict.fromkeys([
                    *target.get("jsonPointers", []), *entry["jsonPointers"]]))
        sync_options = app["spec"].setdefault("syncPolicy", {}).setdefault("syncOptions", [])
        if "RespectIgnoreDifferences=true" not in sync_options:
            sync_options.append("RespectIgnoreDifferences=true")

    def ensure_ingress_ca_configmaps(self):
        targets = set()
        prerequisites = self.components.get("rhtpaPrerequisites", {})
        rhtpa = self.components.get("rhtpaOperator", {})
        if prerequisites.get("enabled"):
            targets.add((prerequisites["namespace"], prerequisites.get("caConfigMap", "ssc-router-ca")))
        if rhtpa.get("enabled"):
            targets.add((rhtpa["namespace"], rhtpa.get("serverCaConfigMap", "ssc-router-ca")))
        targets = {(namespace, name) for namespace, name in targets if name}
        if not targets:
            return

        secret = self.get("secret", "router-ca", "openshift-ingress-operator")
        certificate = (secret or {}).get("data", {}).get("tls.crt")
        require(certificate, "OpenShift ingress CA Secret openshift-ingress-operator/router-ca has no tls.crt")
        try:
            pem = base64.b64decode(certificate, validate=True).decode("ascii")
        except (ValueError, UnicodeError):
            raise DeploymentError("OpenShift ingress CA certificate is not valid base64 PEM") from None
        require("-----BEGIN CERTIFICATE-----" in pem and "-----END CERTIFICATE-----" in pem,
                "OpenShift ingress CA Secret does not contain a PEM certificate")
        self.ingress_ca_pem = pem

        for namespace, name in sorted(targets):
            if not self.get("namespace", namespace):
                self.apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}])
            existing = self.get("configmap", name, namespace)
            if existing:
                require(existing.get("data", {}).get("ca-bundle.crt") == pem,
                        f"Refusing to replace conflicting ConfigMap {namespace}/{name}")
                print(f"Reuse ingress CA ConfigMap {namespace}/{name}")
                continue
            configmap = {"apiVersion": "v1", "kind": "ConfigMap",
                         "metadata": {"name": name, "namespace": namespace,
                                      "labels": {"app.kubernetes.io/managed-by": "ssc-demo-deploy"}},
                         "data": {"ca-bundle.crt": pem}}
            self.apply([configmap])
            print(f"Created ingress CA ConfigMap {namespace}/{name}")

    def configure_rhtpa_server_ca(self):
        namespace = self.components["rhtpaOperator"]["namespace"]
        configmap = self.components["rhtpaOperator"].get("serverCaConfigMap", "ssc-router-ca")
        mount = "/etc/ssc-router-ca"
        deployment = self.get("deployment", "server", namespace)
        require(deployment, "RHTPA server Deployment is missing after sync")
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        env = {"name": "SSL_CERT_FILE", "value": f"{mount}/ca-bundle.crt"}
        volume = {"name": "ssc-router-ca", "configMap": {"name": configmap}}
        volume_mount = {"name": "ssc-router-ca", "mountPath": mount, "readOnly": True}
        strategy = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}}
        configured = (any(self.contains(item, env) for item in container.get("env", []))
                      and any(self.contains(item, volume_mount) for item in container.get("volumeMounts", []))
                      and any(self.contains(item, volume) for item in pod.get("volumes", []))
                      and self.contains(deployment["spec"].get("strategy"), strategy))
        if configured:
            print("Reuse RHTPA server CA configuration; no rollout triggered.")
        else:
            self.oc("-n", namespace, "patch", "deployment", "server", "--type=strategic",
                    "-p", json.dumps({"spec": {"strategy": strategy, "template": {"spec": {
                        "containers": [{"name": container["name"], "env": [env], "volumeMounts": [volume_mount]}],
                        "volumes": [volume]}}}}))
        self.oc("-n", namespace, "rollout", "status", "deployment/server",
                f"--timeout={self.args.timeout}s", timeout=self.args.timeout + 30)
        print("RHTPA server now trusts the cluster ingress CA; Argo ignores only its env, CA mount, and rollout strategy overrides.")

    def patch_rhtpa_cpu_requests(self):
        require(self.rhtpa_cpu_request, "RHTPA CPU request was not selected during cluster preflight")
        namespace = self.components["rhtpaOperator"]["namespace"]
        patch = {"spec": {"modules": {
            "server": {"resources": {"requests": {"cpu": self.rhtpa_cpu_request}}},
            "importer": {"resources": {"requests": {"cpu": self.rhtpa_cpu_request}}}}}}
        existing = self.get("trustedprofileanalyzer", "trusted-profile-analyzer", namespace)
        modules = (existing or {}).get("spec", {}).get("modules", {})
        if all(cpu_millicores(modules.get(name, {}).get("resources", {}).get("requests", {}).get("cpu", 0),
                              allow_zero=True) == cpu_millicores(self.rhtpa_cpu_request)
               for name in ("server", "importer")):
            print(f"Reuse RHTPA CPU requests ({self.rhtpa_cpu_request}); no patch needed.")
            return
        self.oc("-n", namespace, "patch", "trustedprofileanalyzer", "trusted-profile-analyzer",
                "--type=merge", "-p", json.dumps(patch))
        print(f"Set only RHTPA server/importer CPU requests to {self.rhtpa_cpu_request}; memory and CPU limits were left unchanged.")

    def prepare_source(self, source):
        base = self.override.get("gitops", {}).get("basePath", "automation/gitops")
        require(not Path(base).is_absolute() and ".." not in Path(base).parts, "gitops.basePath must stay inside the source checkout")
        chart = source / base / "bootstrap-infra"
        self.config = merge(load_yaml(chart / "values.yaml"), self.override)
        config = self.config
        self.domain = config["deployer"]["domain"]
        require(valid_domain(self.domain), "Set deployer.domain to the real apps subdomain")
        require(safe_url(config["gitops"]["repoUrl"]), "gitops.repoUrl must be a credential-free HTTPS URL")
        require(config["gitops"]["revision"], "Set gitops.revision to a remote branch or tag")
        self.argo_ns = config["argocd"]["namespace"]
        self.ns = config["pipelineNamespace"]
        require(self.ns == "tssc-app-ci", "The current runbook's Tekton RBAC requires pipelineNamespace=tssc-app-ci; reconcile RBAC before using another namespace")
        self.components = config["components"]
        coordinator_overrides = self.coordinator_helm_overrides()
        require(self.components["tekton"]["enabled"] and self.components["pipelines"]["enabled"],
                "This full-demo coordinator requires the existing Pipelines and Tekton components")
        af = config.get("deployment", {}).get("artifactory", {})
        require(all(isinstance(af.get(key), str) and af[key] and "REPLACE_ME" not in af[key] for key in AF_PASSWORDS),
                "deployment.artifactory requires all five durable credentials; use --init-values for a fresh deployment")
        require(all(re.fullmatch(r"[a-fA-F0-9]+", af[key]) and len(af[key]) in (32, 64) for key in ("masterKey", "joinKey")),
                "Artifactory masterKey/joinKey must be 32- or 64-character hex strings")
        orchestrator_opt_in = self.override.get("components", {}).get("orchestrator", {}).get("enabled") is True
        if self.components["orchestrator"]["enabled"]:
            for filename in ("remediate_and_promote_gitlab.json", "remediate_next_gitlab.json"):
                require((source / "orchestration" / filename).is_file(),
                        f"Orchestrator is enabled but source lacks orchestration/{filename}; supply the workflow source or explicitly disable the component")
        helm_command = ["helm", "template", "ssc", str(chart), "-f", str(self.values)]
        helm_command.extend(coordinator_overrides)
        rendered = run(helm_command, timeout=120)
        self.apps = [doc for doc in yaml.safe_load_all(rendered) if doc]
        require(self.apps and all(isinstance(doc, dict) and doc.get("kind") == "Application" for doc in self.apps), "Bootstrap must render only Argo CD Applications")
        prerequisite_values_path = source / base / "components/rhtpa/tpa-prerequisites/values.yaml"
        prerequisite_values = yaml.safe_load(prerequisite_values_path.read_text()) if prerequisite_values_path.is_file() else {}
        self.rhtpa_hook_ca_enabled = (
            "caConfigMap" in prerequisite_values.get("oidc", {})
            and bool(self.components["rhtpaPrerequisites"].get("caConfigMap", "ssc-router-ca")))
        if self.components["keycloak"]["enabled"]:
            keycloak_app = next((app for app in self.apps if app["metadata"]["name"] == "ssc-keycloak"), None)
            require(keycloak_app, "Keycloak is enabled but its Application was not rendered")
            values = keycloak_app["spec"]["source"]["helm"]["valuesObject"]
            values.setdefault("keycloak", {}).setdefault("route", {})["host"] = f"sso.{config['deployer']['domain']}"
            self.keycloak_route_override = True
        require("REPLACE_ME" not in json.dumps(self.apps), "Enabled components still contain REPLACE_ME credentials/configuration")
        af_dir = source / base / "artifactory"
        self.extras = [doc for doc in yaml.safe_load_all((af_dir / "openshift-extras.yaml").read_text()) if doc and doc.get("kind") != "Secret"]
        self.af_app = load_yaml(af_dir / "application-artifactory.yaml")
        self.af_app["metadata"]["namespace"] = self.argo_ns
        self.source = source
        self.base = base
        self.ai = load_yaml(source / config["gitops"].get("tektonPath", "pipelines") / "config/ai-agent-config.yaml")["data"]
        provider = self.ai.get("AI_PROVIDER", "openai")
        require(provider in ("openai", "anthropic"), "Coordinator credential initialization currently supports openai/anthropic; configure other providers manually")
        self.ai_key = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        self.artifactory_secrets = {
            "artifactory-admin": {"password": "admin@*=" + af["adminPassword"]},
            "artifactory-keys": {"master-key": af["masterKey"], "join-key": af["joinKey"]},
            "artifactory-postgresql": {"postgres-password": af["postgresPassword"], "password": af["appPassword"]},
        }

    def coordinator_helm_overrides(self):
        private_components = self.override.get("components", {})
        args = []
        if private_components.get("orchestrator", {}).get("enabled") is not True:
            self.components["orchestrator"]["enabled"] = False
            args.extend(["--set", "components.orchestrator.enabled=false"])
        prerequisites = self.components["rhtpaPrerequisites"]
        if prerequisites["enabled"] and "caConfigMap" not in private_components.get("rhtpaPrerequisites", {}):
            prerequisites["caConfigMap"] = "ssc-router-ca"
            args.extend(["--set", "components.rhtpaPrerequisites.caConfigMap=ssc-router-ca"])
        return args

    def show_plan(self):
        print("Plan only: no cluster access, remote-source verification, or resource changes.")
        print("1. Verify source/configuration, cluster identity/health, storage, and existing credentials.")
        print("2. Reuse/install GitOps; explicitly prepare demo controller privileges and memory.")
        print("3. Preserve Artifactory secrets; apply existing non-secret extras and Application; wait for readiness.")
        print("4. Apply existing bootstrap Applications; initialize missing runtime secrets; wait for platform health.")
        print("5. Validate seeded credentials and test-generation resources; load/verify remediation data when enabled.")
        print("6. Report application environments awaiting their first published image; smoke runs are opt-in.")
        print("Applications: " + ", ".join(app["metadata"]["name"] for app in self.apps))
        print("Apply fetches the configured remote revision again; local plan success does not verify that remote revision.")

    def cleanup_plan(self):
        """Inventory cleanup boundaries only; labels/tracking do not prove creation ownership."""
        config = merge(load_yaml(ROOT / "automation/gitops/bootstrap-infra/values.yaml"), self.override)
        domain = config["deployer"]["domain"]
        require(valid_domain(domain), "Set deployer.domain before inspecting cleanup targets")
        self.oc("whoami")
        actual = self.get("ingresses.config", "cluster")
        require((actual or {}).get("spec", {}).get("domain") == domain,
                "deployer.domain does not match the logged-in cluster; refusing cleanup inventory")
        server = self.oc("whoami", "--show-server").strip()
        components = config["components"]
        argo_ns = config["argocd"]["namespace"]
        protected = {"default", argo_ns, "openshift-storage", "openshift-operators", "openshift-pipelines"}
        protected.update(components[name]["namespace"] for name in
                         ("odf", "pipelines", "keycloak", "keycloakClients", "orchestrator"))

        def preserve_namespace(namespace):
            return namespace in protected or namespace.startswith(("openshift-", "kube-"))

        def identity(resource):
            metadata = resource["metadata"]
            return {"kind": resource.get("kind"), "namespace": metadata.get("namespace"),
                    "name": metadata["name"], "uid": metadata.get("uid"),
                    "finalizers": metadata.get("finalizers", [])}

        namespaces = {config["pipelineNamespace"], "artifactory", argo_ns, "openshift-pipelines"}
        namespaces.update(component["namespace"] for component in components.values() if "namespace" in component)
        namespaces.update(env["namespace"] for env in components["appEnvironments"].get("environments", []))
        apps = []
        if self.get("crd", "applications.argoproj.io"):
            apps = (self.get("applications", namespace=argo_ns) or {}).get("items", [])
        application_rows = []
        for app in apps:
            metadata = app["metadata"]
            labels = metadata.get("labels", {})
            name = metadata["name"]
            if not (labels.get("app.kubernetes.io/part-of") == "ssc-demo"
                    or labels.get("demo.redhat.com/application") == "ssc-demo"
                    or name == "artifactory"):
                continue
            target = app.get("spec", {}).get("destination", {})
            destination = target.get("namespace")
            local_cluster = target.get("server") == "https://kubernetes.default.svc" or target.get("name") == "in-cluster"
            if destination and local_cluster:
                namespaces.add(destination)
            keep = (not local_cluster or name in ("ssc-odf", "ssc-pipelines", "ssc-keycloak", "ssc-keycloak-clients", "ssc-orchestrator")
                    or (destination and preserve_namespace(destination)))
            row = identity(app)
            row.update({"decision": "KEEP" if keep else "REVIEW",
                        "reason": ("Remote or unverified destination cluster" if not local_cluster else
                                   "Shared/protected platform component" if keep
                                   else "Demo association is not proof that this Application or its resources were newly created"),
                        "localDestination": local_cluster,
                        "cascadeDeletion": any(f.startswith("resources-finalizer.argocd.argoproj.io")
                                               for f in row["finalizers"]),
                        "controllers": [{"kind": owner.get("kind"), "name": owner.get("name")}
                                        for owner in metadata.get("ownerReferences", [])],
                        "reportedResources": [{key: resource.get(key) for key in ("group", "kind", "namespace", "name")}
                                              for resource in app.get("status", {}).get("resources", [])]})
            application_rows.append(row)
        has_buckets = bool(self.get("crd", "objectbucketclaims.objectbucket.io"))
        namespace_rows = []
        for namespace in sorted(namespaces):
            live = self.get("namespace", namespace)
            if not live:
                continue
            row = identity(live)
            keep = preserve_namespace(namespace)
            row.update({"decision": "KEEP" if keep else "REVIEW",
                        "reason": ("Protected infrastructure, Keycloak, or Orchestrator namespace" if keep
                                   else "Creation ownership and unrelated contents are unknown; namespace deletion would erase all contents"),
                        "storageInspected": not keep})
            if not keep:
                row["persistentVolumeClaims"] = [identity(pvc) for pvc in
                    (self.get("persistentvolumeclaims", namespace=namespace) or {}).get("items", [])]
                row["bucketClaims"] = ([identity(bucket) for bucket in
                    (self.get("objectbucketclaims", namespace=namespace) or {}).get("items", [])] if has_buckets else [])
            namespace_rows.append(row)
        cluster_names = {"pipelines-defaults", "chains-bootstrap", "ephemeral-test-namespace-lifecycle",
                         "tekton-triggers-eventlistener-clusterbinding", "system:openshift:scc:anyuid-catalog",
                         "artifactory-scc", "odf-storagecluster-bootstrap"}
        cluster_rows = []
        for resource in self.get("clusterroles,clusterrolebindings,securitycontextconstraints")["items"]:
            metadata = resource["metadata"]
            if (metadata["name"] not in cluster_names
                    and metadata.get("labels", {}).get("app.kubernetes.io/part-of") not in ("ssc-demo", "maven-build-ci")
                    and metadata.get("labels", {}).get("app.kubernetes.io/managed-by") != "ssc-demo-cluster"):
                continue
            row = identity(resource)
            row.update({"decision": "KEEP", "reason": "Cluster-scoped privileges or shared infrastructure require an ownership/usage review"})
            cluster_rows.append(row)
        subscriptions = []
        if self.get("crd", "subscriptions.operators.coreos.com"):
            for resource in (self.get("subscriptions.operators.coreos.com", all_namespaces=True) or {}).get("items", []):
                row = identity(resource)
                row.update({"decision": "KEEP", "reason": "Operator uninstall is outside demo cleanup"})
                subscriptions.append(row)
        return {"mode": "read-only cleanup inventory", "deletionEnabled": False,
                "cluster": {"server": server, "domain": domain},
                "applications": sorted(application_rows, key=lambda row: row["name"]),
                "namespaces": namespace_rows, "clusterResources": cluster_rows, "operatorSubscriptions": subscriptions,
                "warnings": [
                    "REVIEW is not permission to delete. This command makes no changes and has no deletion option.",
                    "Application resource lists are Argo status snapshots, not a complete live inventory or creation ledger.",
                    "Only associated Applications and configured namespaces are inspected; unlabeled or external leftovers may be absent.",
                    "Argo cascading deletion can remove adopted/shared resources. Do not delete Applications from this report blindly.",
                    "ApplicationSet or parent reconciliation can recreate deleted Applications; inspect their controllers before cleanup.",
                    "PVC/namespace/bucket deletion can permanently destroy demo data or backing storage; data retention is not established here.",
                    "GitOps, ODF, Keycloak, Orchestrator, operators, signing keys, and private values must be preserved.",
                    "Shared controller sizing/admin grants, TektonConfig/console changes, and external identity/service data are not rolled back.",
                    "A fresh-reset executor still needs explicit resource ownership and data-deletion decisions. Never force-remove finalizers."]}

    def secret(self, name, namespace, desired=None, required=(), check_only=False):
        existing = self.get("secret", name, namespace)
        if existing:
            try:
                data = {key: base64.b64decode(value, validate=True).decode() for key, value in existing.get("data", {}).items()}
            except (ValueError, UnicodeError):
                raise DeploymentError(f"Secret {namespace}/{name} has invalid data; refusing replacement") from None
            keys = desired.keys() if desired else required
            require(all(data.get(key) and "REPLACE_ME" not in data[key] for key in keys),
                    f"Secret {namespace}/{name} is incomplete or contains placeholders; repair deliberately")
            require(not desired or all(data.get(key) == value for key, value in desired.items()),
                    f"Secret {namespace}/{name} differs from private inputs; refusing credential rotation. Recover the original values")
            if name == "ai-agent-secret":
                require("AI_PROVIDER" not in data, "ai-agent-secret must not override the AI_PROVIDER ConfigMap setting")
            print(f"Reuse Secret {namespace}/{name}")
            return data
        if check_only:
            return None
        require(desired, f"Missing Secret {namespace}/{name}; supply its input before applying")
        # Create, not apply: a concurrent credential creator must cause a conflict.
        document = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": namespace}, "type": "Opaque", "stringData": desired}
        self.oc("create", "-f", "-", data=json.dumps(document))
        print(f"Created Secret {namespace}/{name}")
        return desired

    def smoke_test(self):
        require(all(self.components[name]["enabled"] for name in ("gitlab", "quay", "rhtpaOperator")),
                "The runbook smoke test requires GitLab, Quay, and RHTPA")
        params = {"component-name": "spring-boot-example-application",
                  "git-url": f"https://gitlab-{self.components['gitlab']['namespace']}.{self.domain}/development/spring-boot-example-application.git",
                  "revision": "main", "output-image": f"quay.{self.domain}/{self.components['quay']['organization']}/spring-boot-example-application:smoke",
                  "git-host": f"gitlab-{self.components['gitlab']['namespace']}.{self.domain}"}
        document = {"apiVersion": "tekton.dev/v1", "kind": "PipelineRun",
                    "metadata": {"generateName": "ssc-demo-smoke-", "namespace": self.ns},
                    "spec": {"pipelineRef": {"name": "agentic-cve-analysis"},
                             "timeouts": {"pipeline": f"{self.args.timeout}s"},
                             "params": [{"name": key, "value": value} for key, value in params.items()],
                             "workspaces": [{"name": "workspace", "volumeClaimTemplate": {"spec": {
                                 "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "5Gi"}}}}},
                                 {"name": "maven-settings", "secret": {"secretName": "maven-settings"}}]}}
        self.get("pipeline", "agentic-cve-analysis", self.ns) or require(False, "Missing smoke-test Pipeline")
        fingerprint = hashlib.sha256(json.dumps([self.source_revision, document["spec"]], sort_keys=True).encode()).hexdigest()[:32]
        document["metadata"]["labels"] = {"ssc-demo-smoke": fingerprint}
        runs = json.loads(self.oc("-n", self.ns, "get", "pipelineruns", "-l", f"ssc-demo-smoke={fingerprint}", "-o", "json"))["items"]
        previous = max(runs, key=lambda item: item["metadata"]["creationTimestamp"], default=None)
        condition = next((c for c in (previous or {}).get("status", {}).get("conditions", [])
                          if c["type"] == "Succeeded"), {})
        if previous and condition.get("status") != "False":
            created = previous
            print(f"Reuse smoke PipelineRun {self.ns}/{created['metadata']['name']}")
        else:
            created = json.loads(self.oc("create", "-f", "-", "-o", "json", data=json.dumps(document)))
        name = created["metadata"]["name"]

        def succeeded():
            status = self.get("pipelinerun", name, self.ns).get("status", {})
            condition = next((c for c in status.get("conditions", []) if c["type"] == "Succeeded"), {})
            require(condition.get("status") != "False", f"Smoke PipelineRun {self.ns}/{name} failed; inspect its TaskRuns")
            return condition.get("status") == "True"

        self.wait(f"Smoke PipelineRun {self.ns}/{name}", succeeded)

    @staticmethod
    def contains(actual, desired):
        if isinstance(desired, dict):
            return isinstance(actual, dict) and all(
                (key not in actual and (value == {} or (key == "directory" and value in ({}, {"recurse": False}))
                                        or (key == "recurse" and value is False))) or
                (key in actual and Deployment.contains(actual[key], value))
                for key, value in desired.items())
        return actual == desired

    def ensure_app(self, app):
        name, namespace = app["metadata"]["name"], app["metadata"]["namespace"]
        existing = self.get("application", name, namespace)
        if existing:
            if self.contains(existing["spec"], app["spec"]):
                print(f"Reuse Application {namespace}/{name}")
            elif name == "ssc-rhtpa-operator" and self.rhtpa_cpu_request:
                desired = copy.deepcopy(app["spec"])
                expected_ignores = desired.pop("ignoreDifferences", [])
                current = copy.deepcopy(existing["spec"])
                current_ignores = current.pop("ignoreDifferences", [])
                desired_options = desired.get("syncPolicy", {}).get("syncOptions", [])
                current_options = current.get("syncPolicy", {}).get("syncOptions", [])
                if ("RespectIgnoreDifferences=true" in desired_options
                        and "RespectIgnoreDifferences=true" not in current_options):
                    desired["syncPolicy"]["syncOptions"].remove("RespectIgnoreDifferences=true")
                expected_by_target = {(rule.get("group"), rule.get("kind"), rule.get("name"),
                                       rule.get("namespace")): set(rule.get("jsonPointers", []))
                                      for rule in expected_ignores}
                require(all((rule.get("group"), rule.get("kind"), rule.get("name"),
                             rule.get("namespace")) in expected_by_target
                            and set(rule.get("jsonPointers", [])) <= expected_by_target[
                                (rule.get("group"), rule.get("kind"), rule.get("name"),
                                 rule.get("namespace"))] for rule in current_ignores),
                        f"Existing {name} Application has unrelated ignoreDifferences; refusing to broaden ignored fields")
                require(self.contains(current, desired),
                        f"{name} Application has unrelated changes; refusing to alter it")
                self.apply([app])
                print(f"Protect only the selected {name} runtime override fields from Argo self-heal")
            elif self.skip_quay_noobaa_preflight and name == "ssc-quay":
                previous = copy.deepcopy(app["spec"])
                values = previous["source"]["helm"]["valuesObject"]["quay-registry"]
                values["noobaa"]["enabled"] = True
                require(values["quay"]["component"]["objectstorage"]["managed"] is True
                        and self.contains(existing["spec"], previous),
                        "Existing Quay Application has unrelated changes; refusing to alter it")
                self.apply([app])
                print("Update Quay Application only to skip its invalid pvPool patch; managed object storage remains enabled")
            elif self.keycloak_route_override and name == "ssc-keycloak":
                previous = copy.deepcopy(app["spec"])
                keycloak_values = previous["source"]["helm"]["valuesObject"].get("keycloak", {})
                route_values = keycloak_values.get("route", {})
                route_values.pop("host", None)
                if not route_values:
                    keycloak_values.pop("route", None)
                if not keycloak_values:
                    previous["source"]["helm"]["valuesObject"].pop("keycloak", None)
                require(self.contains(existing["spec"], previous),
                        "Existing Keycloak Application has unrelated changes; refusing to alter it")
                self.apply([app])
                print("Set Keycloak's explicit sso Route hostname to match the OIDC issuer used by the runbook")
            else:
                require(False,
                        f"Application {namespace}/{name} differs from selected runbook inputs; review/reconcile it explicitly before rerunning")
        else:
            self.apply([app])
        if (self.source_revision and app["spec"]["source"].get("repoURL") == self.config["gitops"]["repoUrl"]
                and (not existing or not self.contains(existing["spec"], app["spec"])
                     or existing.get("status", {}).get("sync", {}).get("revision") != self.source_revision)):
            self.oc("-n", namespace, "annotate", "application", name,
                    "argocd.argoproj.io/refresh=hard", "--overwrite")
        if (existing and not existing.get("operation")
                and existing.get("status", {}).get("operationState", {}).get("phase") in ("Failed", "Error")):
            source = app["spec"]["source"]
            revision = (self.source_revision if source.get("repoURL") == self.config["gitops"]["repoUrl"]
                        else None) or source.get("targetRevision", "HEAD")
            self.oc("-n", namespace, "patch", "application", name, "--type=merge",
                    "-p", json.dumps({"operation": {"initiatedBy": {"username": "ssc-demo-deploy"},
                                                    "sync": {"revision": revision, "prune": False}}}))
            print(f"Retry failed sync for Application {namespace}/{name}; completed Applications are reused.")

    def cluster_checks(self):
        self.oc("whoami")
        actual_domain = self.get("ingresses.config", "cluster")["spec"]["domain"]
        require(actual_domain == self.domain, "deployer.domain does not match the logged-in cluster; refusing deployment")
        nodes = self.get("nodes")["items"]
        require(nodes and all(any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"]) and not n.get("spec", {}).get("unschedulable") for n in nodes), "Every node must be Ready and schedulable")
        operators = self.get("clusteroperators")["items"]
        require(operators and all({c["type"]: c["status"] for c in op["status"]["conditions"]}.get("Available") == "True" and
                                 {c["type"]: c["status"] for c in op["status"]["conditions"]}.get("Degraded") == "False" for op in operators), "Cluster operators must be Available and not Degraded")
        if self.components["rhtpaOperator"]["enabled"]:
            self.rhtpa_cpu_request = self.select_rhtpa_cpu_request(
                nodes, (self.get("pods", all_namespaces=True) or {}).get("items", []))
        if self.components["keycloak"]["enabled"]:
            oauth = self.get("oauth", "cluster")
            require(not any(urlparse(p.get("openID", {}).get("issuer", "")).hostname == "sso." + self.domain for p in oauth["spec"].get("identityProviders", [])),
                    "The cluster already uses this Keycloak issuer for login; select the runbook's bring-your-own Keycloak configuration")
        # Preserve existing ODF. Never create a second StorageCluster by assumption.
        classes = self.get("storageclass")["items"]
        require(sum(sc["metadata"].get("annotations", {}).get("storageclass.kubernetes.io/is-default-class") == "true" for sc in classes) == 1,
                "The unchanged demo charts require exactly one default StorageClass")
        needs_odf = self.components["quay"]["enabled"] or (self.components["rhtpaOperator"]["enabled"] and self.components["rhtpaOperator"].get("storage", {}).get("type", "s3") == "s3")
        if needs_odf:
            crd = self.get("crd", "storageclusters.ocs.openshift.io")
            clusters = self.get("storagecluster", namespace="openshift-storage")["items"] if crd else []
            if clusters:
                require(len(clusters) == 1 and clusters[0].get("status", {}).get("phase") == "Ready", "Existing ODF must have one Ready StorageCluster; refusing another installation")
                ceph = self.get("cephcluster", namespace="openshift-storage")["items"]
                require(ceph and all(c.get("status", {}).get("ceph", {}).get("health") == "HEALTH_OK" for c in ceph), "Existing Ceph is not HEALTH_OK")
                noobaa = self.get("noobaa", "noobaa", "openshift-storage")
                require(noobaa and noobaa.get("status", {}).get("phase") == "Ready", "Existing NooBaa is not Ready")
                if self.components["odf"]["enabled"]:
                    # A standalone cluster provisioner may already own ODF. Do not adopt it.
                    self.apps = [app for app in self.apps if app["metadata"]["name"] != "ssc-odf"]
                print("Reuse healthy existing ODF; no StorageCluster or storage defaults will be changed.")
                quay = self.components["quay"]
                if quay["enabled"] and quay["objectStorageManaged"]:
                    backingstore = self.get("backingstore", "noobaa-default-backing-store", "openshift-storage")
                    require(backingstore and backingstore.get("status", {}).get("phase") == "Ready",
                            "Managed Quay storage requires a Ready NooBaa default BackingStore")
                    self.skip_quay_noobaa_preflight = True
                    print(f"Reuse Ready NooBaa BackingStore ({backingstore.get('spec', {}).get('type')}); skip the chart's pvPool-only patch Job while keeping Quay object storage enabled.")
            else:
                require(self.components["odf"]["enabled"], "ODF is required; explicitly enable its existing chart or provision it first")
                version = self.get("clusterversion", "version")["status"]["desired"]["version"]
                require(self.components["odf"]["channel"] == "stable-" + ".".join(version.split(".")[:2]), "ODF channel must match this cluster's OpenShift minor")
                require(self.get("storageclass", self.components["odf"]["storageClassName"]), "ODF block StorageClass is missing")

    def runtime_secrets(self, check_only=False):
        if not check_only:
            self.wait(f"Namespace {self.ns}", lambda: self.get("namespace", self.ns))
        af = self.config["deployment"]["artifactory"]
        ai_value = os.environ.get(self.ai_key)
        self.secret("ai-agent-secret", self.ns, {self.ai_key: ai_value} if ai_value else None, (self.ai_key,), check_only=check_only)
        if self.components["rhtpaOperator"]["enabled"]:
            realm = self.components["keycloak"]["realm"]
            self.secret("tpa-secret", self.ns, {
                "bombastic_api_url": f"https://server-{self.components['rhtpaOperator']['namespace']}.{self.domain}",
                "oidc_issuer_url": f"https://sso.{self.domain}/realms/{realm}",
                "oidc_client_id": "tpa-cli", "oidc_client_secret": self.config["credentials"]["tpaCliClientSecret"]}, check_only=check_only)
        settings = ET.Element("settings", xmlns="http://maven.apache.org/SETTINGS/1.0.0")
        server = ET.SubElement(ET.SubElement(settings, "servers"), "server")
        for key, value in {"id": "artifactory", "username": "admin", "password": af["adminPassword"]}.items():
            ET.SubElement(server, key).text = value
        mirror = ET.SubElement(ET.SubElement(settings, "mirrors"), "mirror")
        for key, value in {"id": "artifactory", "url": "http://artifactory.artifactory.svc.cluster.local:8082/artifactory/maven", "mirrorOf": "*"}.items():
            ET.SubElement(mirror, key).text = value
        desired = ET.tostring(settings, encoding="unicode")
        existing = self.get("secret", "maven-settings", self.ns)
        if existing:
            data = self.secret("maven-settings", self.ns, required=("settings.xml",))
            try:
                require(ET.canonicalize(data["settings.xml"], strip_text=True) == ET.canonicalize(desired, strip_text=True),
                        "Existing maven-settings differs from the runbook mirror/credential; review it explicitly")
            except ET.ParseError:
                raise DeploymentError("Existing maven-settings is not valid XML") from None
        elif not check_only:
            self.secret("maven-settings", self.ns, {"settings.xml": desired})

    def runtime_ready(self):
        """Observe generated workloads, not just the custom resources Argo submitted."""
        namespaces = {component["namespace"] for name, component in self.components.items()
                      if name in ("keycloak", "keycloakClients", "gitlab", "quay", "rhtas", "rhtpaOperator", "orchestrator")
                      and component["enabled"]}
        for namespace in sorted(namespaces):
            def ready(namespace=namespace):
                workloads = self.get("deployments,statefulsets", namespace=namespace)["items"]
                if not workloads:
                    return False
                for workload in workloads:
                    replicas = workload["spec"].get("replicas", 1)
                    status = workload.get("status", {})
                    if replicas == 0 or workload.get("metadata", {}).get("deletionTimestamp"):
                        continue
                    if (status.get("observedGeneration", 0) < workload["metadata"]["generation"]
                            or status.get("readyReplicas", 0) != replicas
                            or status.get("updatedReplicas", 0) != replicas):
                        return False
                    if workload["kind"] == "StatefulSet" and (not status.get("currentRevision") or status.get("currentRevision") != status.get("updateRevision")):
                        return False
                return any(w["spec"].get("replicas", 1) > 0 and not w.get("metadata", {}).get("deletionTimestamp") for w in workloads)
            self.wait(f"generated workloads in {namespace}", ready)
        self.verify_rhtpa()

    def verify_artifactory(self):
        af = self.config["deployment"]["artifactory"]
        auth = base64.b64encode(("admin:" + af["adminPassword"]).encode()).decode()

        def artifactory_ready():
            repositories = self.route_json(Request(f"https://artifactory-artifactory.{self.domain}/artifactory/api/repositories",
                                                   headers={"Authorization": "Basic " + auth}))
            if repositories is None:
                return False
            require(isinstance(repositories, list), "Artifactory repository API returned an unexpected response")
            keys = {repo.get("key") for repo in repositories}
            require({"maven", "lightwell", "lightwell-validated", "lightwell-remediated", "maven-central"} <= keys,
                    "Artifactory is initialized but missing the runbook Maven repositories; reapplying the first-boot ConfigMap will not repair it")
            return True
        self.wait("authenticated Artifactory Maven repository configuration", artifactory_ready)

    def verify_rhtpa(self):
        if self.components["rhtpaOperator"]["enabled"]:
            data = self.secret("tpa-secret", self.ns, required=("bombastic_api_url", "oidc_issuer_url", "oidc_client_id", "oidc_client_secret"))

            def rhtpa_ready():
                discovery = self.route_json(Request(data["oidc_issuer_url"] + "/.well-known/openid-configuration"))
                if not discovery:
                    return False
                endpoint = discovery.get("token_endpoint", "")
                require(safe_url(endpoint) and urlparse(endpoint).netloc == urlparse(data["oidc_issuer_url"]).netloc,
                        "OIDC token endpoint must use the configured HTTPS issuer host")
                auth = base64.b64encode((data["oidc_client_id"] + ":" + data["oidc_client_secret"]).encode()).decode()
                token = self.route_json(Request(endpoint, data=urlencode({"grant_type": "client_credentials"}).encode(),
                                                headers={"Authorization": "Basic " + auth}))
                if not token:
                    return False
                require(token.get("access_token"), "OIDC token response has no access_token")
                return self.route_json(Request(data["bombastic_api_url"] + "/api/v2/sbom?limit=1",
                                               headers={"Authorization": "Bearer " + token["access_token"]})) is not None
            self.wait("OIDC credentials and authenticated RHTPA API", rhtpa_ready)

    @staticmethod
    def http_json(request):
        try:
            # Never forward Authorization headers through an unexpected route redirect.
            with build_opener(NoRedirect).open(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            require(not 300 <= error.code < 400, "Runtime API redirected unexpectedly; verify its canonical Route URL (credentials were not forwarded)")
            require(error.code not in (400, 401, 403), "Runtime API rejected configured credentials; reconcile them deliberately")
            return None
        except (URLError, ssl.SSLError) as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise RouteTLSVerificationError("Route TLS verification failed; configure trusted roots with SSL_CERT_FILE (do not bypass verification); cluster-owned routes can use their verified ingress certificate") from None
            if isinstance(reason, socket.gaierror):
                raise RouteResolutionError("Route DNS is not available on this workstation; use the verified in-cluster ingress tunnel") from None
            return None
        except (TimeoutError, ConnectionError):
            return None

    def route_json(self, request):
        """Use direct verified routing where available, then the API-tunneled ingress."""
        try:
            return self.http_json(request)
        except (RouteResolutionError, RouteTLSVerificationError):
            host = urlparse(request.full_url).hostname
            require(host and (host == self.domain or host.endswith("." + self.domain)),
                    "Only cluster-owned routes may use the in-cluster ingress tunnel")
            if self.route_forward is None or self.route_forward[0].poll() is not None:
                self.start_route_forward()
            process, local_port, context = self.route_forward
            connection = LocalTunnelHTTPSConnection(host, port=local_port, context=context, timeout=20)
            try:
                headers = dict(request.header_items())
                headers["Host"] = host
                parsed = urlparse(request.full_url)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request(request.get_method(), path, body=request.data, headers=headers)
                response = connection.getresponse()
                if 300 <= response.status < 400:
                    raise DeploymentError("Runtime API redirected unexpectedly; verify its canonical Route URL (credentials were not forwarded)")
                if response.status in (400, 401, 403):
                    raise DeploymentError("Runtime API rejected configured credentials; reconcile them deliberately")
                if not 200 <= response.status < 300:
                    return None
                return json.loads(response.read())
            except ssl.SSLCertVerificationError:
                raise DeploymentError("Route TLS verification failed against the cluster ingress certificate") from None
            except (OSError, http.client.HTTPException):
                return None
            finally:
                connection.close()

    def start_route_forward(self):
        """Create only a local API port-forward; no cluster object is created."""
        controller = json.loads(self.oc("get", "ingresscontroller", "default", "-n", "openshift-ingress-operator", "-o", "json"))
        cert_name = controller.get("spec", {}).get("defaultCertificate", {}).get("name", "router-certs-default")
        cert_data = self.oc("-n", "openshift-ingress", "get", "secret", cert_name,
                            "-o", r"jsonpath={.data.tls\.crt}")
        require(cert_data, "Cannot read the public default ingress certificate for verified route probes")
        cert = base64.b64decode(cert_data).decode()
        context = ssl.create_default_context(cadata=cert)
        pods = json.loads(self.oc("-n", "openshift-ingress", "get", "pods", "-l",
                                  "ingresscontroller.operator.openshift.io/deployment-ingresscontroller=default", "-o", "json"))["items"]
        ready = [pod for pod in pods if any(condition.get("type") == "Ready" and condition.get("status") == "True"
                                            for condition in pod.get("status", {}).get("conditions", []))]
        require(ready, "No Ready default ingress router pod is available for local route probing")
        for pod in ready:
            with socket.socket() as sock:
                sock.bind(("localhost", 0))
                local_port = sock.getsockname()[1]
            process = subprocess.Popen(["oc", "-n", "openshift-ingress", "port-forward",
                                         "pod/" + pod["metadata"]["name"], f"{local_port}:443"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 20
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    with socket.create_connection(("localhost", local_port), timeout=0.2):
                        self.route_forward = (process, local_port, context)
                        return
                except OSError:
                    time.sleep(0.2)
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise DeploymentError("Could not open a temporary local port-forward to the default ingress routers")

    def close(self):
        if self.route_forward:
            process = self.route_forward[0]
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            self.route_forward = None

    def load_remediation_data(self):
        loader = self.source / self.config["gitops"].get("tektonPath", "pipelines") / "ops/rhtpa-load-lightwell-data.sh"
        command = ["bash", str(loader), "--namespace", self.ns]
        require(getattr(self, "ingress_ca_pem", None),
                "Cannot verify remediation routes without the cluster ingress CA")
        require(self.route_forward and self.route_forward[0].poll() is None,
                "Cannot access remediation routes without the verified ingress tunnel")
        secret = self.get("secret", "tpa-secret", self.ns)
        require(secret and secret.get("data", {}).get("bombastic_api_url")
                and secret.get("data", {}).get("oidc_issuer_url"),
                "RHTPA API URL or OIDC issuer is missing from tpa-secret")
        urls = [base64.b64decode(secret["data"][key], validate=True).decode("utf-8")
                for key in ("bombastic_api_url", "oidc_issuer_url")]
        hosts = [urlparse(value).hostname for value in urls]
        require(all(safe_url(value) and host for value, host in zip(urls, hosts)),
                "RHTPA remediation endpoints must use verified HTTPS routes")
        curl = shutil.which("curl")
        require(curl, "curl is required to load remediation data")
        print("Checking whether remediation data is already loaded.")
        with tempfile.TemporaryDirectory(prefix="ssc-demo-ingress-ca-") as directory:
            ca_bundle = Path(directory) / "ca-bundle.crt"
            ca_bundle.write_text(self.ingress_ca_pem, encoding="ascii")
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            curl_wrapper = bin_dir / "curl"
            connect_args = " ".join(shlex.quote(f"{host}:443:127.0.0.1:{self.route_forward[1]}")
                                     for host in hosts)
            curl_wrapper.write_text(
                "#!/bin/sh\nexec " + shlex.quote(curl) + " --cacert " + shlex.quote(str(ca_bundle))
                + " --connect-to " + connect_args.replace(" ", " --connect-to ") + ' "$@"\n',
                encoding="ascii")
            curl_wrapper.chmod(0o700)
            no_proxy = ",".join(filter(None, [os.environ.get("NO_PROXY"), os.environ.get("no_proxy"), *hosts]))
            environment = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                               CURL_CA_BUNDLE=str(ca_bundle), NO_PROXY=no_proxy, no_proxy=no_proxy)
            try:
                verification = subprocess.run([*command, "--verify"], text=True, capture_output=True,
                                              check=False, timeout=self.args.timeout, env=environment)
            except (OSError, subprocess.TimeoutExpired):
                raise DeploymentError("RHTPA remediation-data verification could not complete; upload was not attempted") from None
            if verification.returncode == 0:
                print("Reuse verified remediation data; upload skipped.")
                return
            if "Verification FAILED -- see [FAIL] lines above." not in verification.stdout:
                raise DeploymentError("RHTPA remediation-data verification failed before a complete data check; upload was not attempted")
            print("Remediation data is incomplete; loading and verifying through the runbook helper.")
            run(command, timeout=self.args.timeout, env=environment)

    def deploy(self):
        require(self.args.allow_demo_cluster_admin, "--apply requires --allow-demo-cluster-admin for the runbook's controller grant")
        self.cluster_checks()
        if self.components["rhtpaOperator"]["enabled"] and self.rhtpa_cpu_request:
            rhtpa_app = next((app for app in self.apps if app["metadata"]["name"] == "ssc-rhtpa-operator"), None)
            if rhtpa_app:
                self.protect_rhtpa_cpu_requests(rhtpa_app)
        if self.skip_quay_noobaa_preflight:
            quay_app = next((app for app in self.apps if app["metadata"]["name"] == "ssc-quay"), None)
            require(quay_app, "Quay is enabled but its Application was not rendered")
            quay_values = quay_app["spec"]["source"]["helm"]["valuesObject"]["quay-registry"]
            require(quay_values["quay"]["component"]["objectstorage"]["managed"] is True,
                    "Refusing to disable Quay object storage while skipping the NooBaa helper")
            quay_values["noobaa"]["enabled"] = False
        if self.get("namespace", self.ns):
            print(f"Reuse Namespace {self.ns}")
        else:
            self.apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": self.ns}}])
            print(f"Created Namespace {self.ns}")
        require(os.environ.get(self.ai_key) or self.get("secret", "ai-agent-secret", self.ns),
                f"Set {self.ai_key} or provide an existing ai-agent-secret before deployment")
        for name, data in self.artifactory_secrets.items():
            self.secret(name, "artifactory", data, check_only=True)
        self.secret("ai-agent-secret", self.ns,
                    {self.ai_key: os.environ[self.ai_key]} if os.environ.get(self.ai_key) else None,
                    (self.ai_key,), check_only=True)
        # Check every existing runtime input BEFORE any chart Job can update clients.
        # A missing namespace/Secret is safe here; creation happens only after bootstrap.
        # This validates TPA credentials and the Maven mirror without creating Secrets.
        self.runtime_secrets(check_only=True)
        if self.components["keycloakClients"]["enabled"]:
            issuer = f"https://sso.{self.domain}/realms/{self.components['keycloak']['realm']}"
            discovery = self.route_json(Request(issuer + "/.well-known/openid-configuration"))
            require(discovery, "Bring-your-own Keycloak must be reachable before provisioning")
            endpoint = discovery.get("token_endpoint", "")
            require(safe_url(endpoint) and urlparse(endpoint).netloc == urlparse(issuer).netloc,
                    "Bring-your-own OIDC token endpoint must use the configured HTTPS issuer host")
            auth = base64.b64encode(("tpa-cli:" + self.config["credentials"]["tpaCliClientSecret"]).encode()).decode()
            token = self.route_json(Request(endpoint, data=urlencode({"grant_type": "client_credentials"}).encode(),
                                            headers={"Authorization": "Basic " + auth}))
            require(token and token.get("access_token"), "Bring-your-own tpa-cli client must already match private credentials; reconcile it deliberately before provisioning")
        environment = dict(os.environ, SSC_PLATFORM_ARGOCD_NAMESPACE=self.argo_ns,
                           SSC_WAIT_TIMEOUT=f"{self.args.timeout}s")
        helper = str(ROOT / "cluster/ssc-demo-cluster.sh")
        print("Preparing GitOps prerequisites (explicit demo-admin approval supplied).")
        run(["bash", helper, "--bootstrap-gitops"], timeout=self.args.timeout * 6, env=environment)
        run(["bash", helper, "--prepare-gitops", "--allow-demo-cluster-admin"], timeout=self.args.timeout * 6, env=environment)
        self.ensure_ingress_ca_configmaps()
        self.apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "artifactory"}}])
        for name, data in self.artifactory_secrets.items():
            self.secret(name, "artifactory", data)
        self.apply(self.extras)  # Intentionally excludes every placeholder Secret.
        self.ensure_app(self.af_app)
        self.wait_app(self.af_app)
        self.verify_artifactory()
        env_config = self.components["appEnvironments"]
        env_namespaces = {env["namespace"] for env in env_config.get("environments", [])} if env_config["enabled"] else set()
        environments = {app["metadata"]["name"] for app in self.apps
                        if app["spec"].get("destination", {}).get("namespace") in env_namespaces
                        and app["spec"].get("source", {}).get("path") == env_config["chartPath"]}
        platform_apps = [app for app in self.apps if app["metadata"]["name"] not in environments]
        # A Helm annotation alone does not serialize separately-created Applications.
        # The coordinator enforces this dependency order and stops before launching later,
        # potentially resource-heavy components if an earlier stage is not healthy.
        for app in sorted(platform_apps, key=lambda item: int(item.get("metadata", {}).get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0"))):
            print(f"Deploy wave {app.get('metadata', {}).get('annotations', {}).get('argocd.argoproj.io/sync-wave', '0')}: {app['metadata']['name']}")
            self.ensure_app(app)
            self.wait_sync(app)
            if app["metadata"]["name"] == "ssc-rhtpa-operator" and self.rhtpa_cpu_request:
                self.patch_rhtpa_cpu_requests()
                if self.components["rhtpaOperator"].get("serverCaConfigMap", "ssc-router-ca"):
                    self.configure_rhtpa_server_ca()
        self.runtime_secrets()
        # Some component Applications have ordinary Jobs that intentionally wait
        # for resources from later Applications. All syncs are submitted first.
        for app in platform_apps:
            self.wait_app(app)
        self.runtime_ready()
        # These Apps intentionally await the first image publication/promotion.
        for app in self.apps:
            if app["metadata"]["name"] in environments:
                self.ensure_app(app)
        for name, keys in {"scm-auth-secret": ("username", "token"), "scm-auth-gitops-secret": ("username", "token"),
                           "gitlab-webhook-secret": ("secretToken",)}.items():
            if self.components["gitlab"]["enabled"]:
                self.secret(name, self.ns, required=keys)
        if self.components["quay"]["enabled"]:
            self.secret("quay-push", self.ns, required=(".dockerconfigjson",))
        for category in ("unit", "integration", "functional"):
            require(self.get("pipeline", "agentic-test-generation-" + category, self.ns), f"Missing {category} test-generation Pipeline")
        if self.components["rhtpaOperator"]["enabled"] and not self.args.skip_data:
            self.load_remediation_data()
        elif self.args.skip_data:
            print("Remediation data loading and verification explicitly skipped (--skip-data).")
        if self.args.smoke_test:
            self.smoke_test()
        print("Platform Applications are Synced and Healthy; test-generation prerequisites are present.")
        if environments:
            print("Not yet verified: app environments need published image tags/promotions: " + ", ".join(sorted(environments)))
        print("Trigger via GitLab issue comments /generate-tests-unit, /generate-tests-integration, /generate-tests-functional.")
        if not self.args.smoke_test:
            print("No smoke PipelineRun was started. Use --smoke-test or runbook section 11 to build/scan and populate CVE issues.")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--init-values", action="store_true", help="Create a new mode-600 private override; never overwrite")
    mode.add_argument("--plan", action="store_true", help="Offline configuration/render checks (default)")
    mode.add_argument("--apply", action="store_true", help="Fetch configured source and deploy; mutates cluster resources")
    mode.add_argument("--cleanup-plan", action="store_true", help="Read-only cluster cleanup inventory; no deletion option")
    result.add_argument("--values", type=Path, required=True, help="Durable private YAML outside the checkout")
    result.add_argument("--source-dir", type=Path, default=ROOT, help="Checkout to inspect for offline plan only")
    result.add_argument("--repo-url", help="Required by --init-values")
    result.add_argument("--revision", help="Required by --init-values; remote branch/tag")
    result.add_argument("--domain", help="Required by --init-values; cluster apps subdomain")
    result.add_argument("--allow-demo-cluster-admin", action="store_true", help="Acknowledge the existing runbook's controller grant")
    result.add_argument("--timeout", type=int, default=3600, help="Seconds per readiness stage (default: 3600)")
    result.add_argument("--skip-data", action="store_true", help="Explicitly omit remediation dataset loading/verification")
    result.add_argument("--smoke-test", action="store_true", help="Also start/wait for the runbook build/scan PipelineRun (creates image/issues)")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    deployment = None
    try:
        require(args.timeout > 0, "--timeout must be positive")
        if args.init_values:
            init_values(args)
            return 0
        deployment = Deployment(args)
        if args.cleanup_plan:
            print(json.dumps(deployment.cleanup_plan(), indent=2))
        elif not args.apply:
            deployment.prepare_source(args.source_dir.resolve())
            deployment.show_plan()
        else:
            require(args.allow_demo_cluster_admin, "--apply requires --allow-demo-cluster-admin")
            remote = deployment.override.get("gitops", {})
            require(safe_url(remote.get("repoUrl", "")) and remote.get("revision"), "Private values must explicitly select gitops.repoUrl and revision")
            with tempfile.TemporaryDirectory(prefix="ssc-demo-source-") as directory:
                source = Path(directory) / "source"
                run(["git", "clone", "--quiet", "--depth", "1", "--branch", remote["revision"], remote["repoUrl"], str(source)], timeout=180)
                deployment.prepare_source(source)
                deployment.source_revision = run(["git", "-C", str(source), "rev-parse", "HEAD"]).strip()
                deployment.deploy()
        return 0
    except (DeploymentError, KeyError, TypeError, ValueError, AttributeError, OSError, yaml.YAMLError) as error:
        # Do not stringify parser/KeyError exceptions, which can contain private YAML values.
        print("ERROR: " + (str(error) if isinstance(error, DeploymentError) else "Invalid configuration or command response; contents withheld"), file=sys.stderr)
        return 1
    finally:
        if deployment:
            deployment.close()


if __name__ == "__main__":
    sys.exit(main())
