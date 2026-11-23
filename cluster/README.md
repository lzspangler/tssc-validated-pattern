# Cluster Helpers

Adapted from `ssc-demo-fp` commits `17b758e` and `7712272` for this repository's
existing `openshift-gitops` instance and three test-generation pipelines.
The [runbook](../automation/gitops/RUNBOOK.md) remains authoritative for
deployment, credentials, CA trust, Argo CD permissions, and component choices.
Its manual deployment path, existing charts/defaults, and GitLab issue/push
triggers remain available and unchanged. The full-deployment coordinator below
is optional and opt-in; running the cluster helper without flags is still
read-only. No helper changes the default StorageClass.

## Read-Only Preflight

Log in to the intended cluster separately, then run once GitOps is installed:

```bash
bash cluster/ssc-demo-cluster.sh --preflight
```

Checks include OpenShift version (when constrained), node readiness, minimum
schedulable worker count, cluster operator health, the existing Argo CD instance,
and PVC storage. ODF/NooBaa/Ceph readiness is required by default, matching the
default Quay and RHTPA configuration. This is a prerequisite snapshot, not proof
that credentials, capacity, egress, operator catalogs, or the demo are ready.
The command fails on missing resources, insufficient permissions, or failed checks.
It requires an Available Argo CD instance, so it is not the first operation on
a fresh cluster without GitOps; use the explicit bootstrap action below first.

```bash
SSC_EXPECTED_OCP_VERSION=4.21. \
SSC_MIN_READY_WORKERS=4 \
SSC_STORAGE_CLASS=ocs-external-storagecluster-cephfs \
  bash cluster/ssc-demo-cluster.sh --preflight
```

`SSC_STORAGE_CLASS` checks an explicit class without changing the cluster default.
Otherwise exactly one default class is required. Set `SSC_STORAGE_MODE=install-odf`
when ODF will be installed through the runbook; its readiness checks are deferred,
not satisfied. Verify the ODF channel and block class separately, then re-run in
`odf` mode after installation. Use `SSC_STORAGE_MODE=filesystem` only with the
runbook's RHTPA filesystem fallback and Quay disabled. This mode does not make
those Helm overrides for you or verify that a class supports the required access
modes. `--help` lists the ODF resource-name and Argo CD overrides.

## Explicit GitOps Bootstrap And Preparation

These standalone actions require Bash, `oc`, and `jq`, a separate cluster login,
and passing cluster-health checks. Unlike preflight, they can mutate resources:

```bash
bash cluster/ssc-demo-cluster.sh --bootstrap-gitops
bash cluster/ssc-demo-cluster.sh --prepare-gitops --allow-demo-cluster-admin
```

`--bootstrap-gitops` reuses an existing GitOps Subscription without changing its
channel or Argo CD instance. If absent, it installs only the Subscription using
the existing shared global OperatorGroup in `openshift-operators`; ambiguous or
unmanaged Argo ownership blocks installation. It waits for the installed CSV,
Established Argo CD CRD, and selected Argo CD instance to become Available.
Bootstrap does not grant admin rights or resize the controller.

`--prepare-gitops` deliberately grants the selected controller `cluster-admin`
through a deterministic binding and sets memory requests to `2Gi`, limits to
`8Gi`, then waits for the StatefulSet resources and rollout. It is demo-only and
requires `--allow-demo-cluster-admin` before any client calls; conflicting
bindings fail rather than being replaced.

`SSC_PLATFORM_ARGOCD_NAMESPACE` and `SSC_PLATFORM_ARGOCD_NAME` both default to
`openshift-gitops`. `SSC_GITOPS_CHANNEL` selects the channel only for a new
Subscription (otherwise the catalog's `defaultChannel` is used).
`SSC_WAIT_TIMEOUT` is a positive integer duration with `s`, `m`, or `h` suffix,
default `15m` per bootstrap/preparation stage. Health checks retain the optional
`SSC_EXPECTED_OCP_VERSION` prefix and `SSC_MIN_READY_WORKERS` (default `3`).

## Optional Full Deployment

`ssc-demo-deploy.py` coordinates the existing runbook: GitOps bootstrap and
explicit demo preparation, standalone Artifactory, bootstrap Applications,
runtime credentials, readiness checks, and remediation data load/verification.
It does not replace the manual path, change AI configuration, or start a smoke
build unless requested. Run examples from the repository root.

### Local Setup And Private Values

Use Python **3.10+** in an isolated virtual environment. Install the dependencies
from `cluster/requirements.txt` (currently PyYAML), not into system Python:

```bash
python3 -m venv "$HOME/.venvs/ssc-demo"
export SSC_PYTHON="$HOME/.venvs/ssc-demo/bin/python"
"$SSC_PYTHON" -m pip install -r cluster/requirements.txt
umask 077
mkdir -p "$HOME/.config/ssc-demo"
chmod 700 "$HOME/.config/ssc-demo"
export SSC_VALUES="$HOME/.config/ssc-demo/values.yaml"
"$SSC_PYTHON" -B cluster/ssc-demo-deploy.py --init-values \
  --values "$SSC_VALUES" \
  --repo-url 'https://github.com/<you>/ssc-demo.git' \
  --revision '<remote-branch-or-tag>' \
  --domain 'apps.<your-cluster-domain>'
```

Replace the angle-bracket placeholders before running. The parent must already
exist, be private (`700`), and be durable and outside the repository, not `/tmp`.
Initialization creates a mode-`600` file exclusively and refuses overwrite. It
requires an explicit credential-free HTTPS remote URL, remote branch/tag, and
apps subdomain; it does not contact that remote or discover the domain. It
generates the six `credentials` values plus five `deployment.artifactory` values:
`adminPassword`, `masterKey`, `joinKey`, `postgresPassword`, and `appPassword`.
Preserve this file for every rerun; never commit it or log its contents.

Do not independently regenerate manual-path and coordinator credentials
mid-install. Manual Artifactory installation uses its own Secret creation steps;
switching to the coordinator requires recovering those exact existing values
into `deployment.artifactory`, not running fresh random generators. Existing
Secrets and Application specs must match the selected inputs; mismatches fail
for deliberate reconciliation, without credential rotation. This is resumable
convergence: reruns inspect live resources rather than trust a local checkpoint.

### Source Checks And Apply

**Orchestrator is disabled by default by the coordinator.** To opt in, set
`components.orchestrator.enabled: true` in private values and use a source that
contains both `orchestration/remediate_and_promote_gitlab.json` and
`orchestration/remediate_next_gitlab.json`. The coordinator checks the selected
source before cluster mutation and fails if either workflow is missing. This
demo does not deploy Orchestrator because another project will replace it.

```bash
"$SSC_PYTHON" -B cluster/ssc-demo-deploy.py --plan --values "$SSC_VALUES"
# Optional: inspect a different local checkout instead of ROOT (the script's repo root).
"$SSC_PYTHON" -B cluster/ssc-demo-deploy.py --plan --values "$SSC_VALUES" \
  --source-dir /path/to/intended/local/checkout
```

Plan is also the default when no mode is specified. It performs local YAML and
`helm template` checks only, with no `oc`, cluster access, or network access. Helm
must be installed. A successful local plan does not verify the remote revision.

Before apply, review runbook component/storage choices and log in separately.
The coordinator reads `AI_PROVIDER` from the selected source's
`pipelines/config/ai-agent-config.yaml` (or `gitops.tektonPath` equivalent).
Provide the matching `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` through the environment,
or an existing `ai-agent-secret` containing the matching key. The Secret must not
override `AI_PROVIDER`; an environment key differing from the existing Secret
fails rather than rotating it. Other AI providers need manual configuration.
Do not put API keys in commands, shell history, or logs.

```bash
"$SSC_PYTHON" -B cluster/ssc-demo-deploy.py --apply --values "$SSC_VALUES" \
  --allow-demo-cluster-admin
```

Apply needs `git`, Helm, Bash, `oc`, `jq`, and the existing loader's `curl` and
`python3`. It always freshly clones the configured HTTPS remote branch/tag into
temporary source storage, validates that source, then checks cluster identity,
health, storage, and credentials before cluster mutation. `--source-dir` is
plan-only and cannot substitute local changes for that fetch. Apply requires the
explicit demo-admin acknowledgment; it bootstraps/reuses GitOps and invokes the
privilege/memory preparation described above. `--timeout` is positive seconds
per readiness stage, default `3600`; waits are bounded, not a total-install SLA.

Healthy existing ODF is reused without adopting it into a second installation
or changing the default StorageClass. If ODF is needed and absent, explicitly
enable `components.odf.enabled` with a channel matching the cluster's OpenShift
minor and the appropriate existing **block** `storageClassName` (runbook §0/§4).
The unchanged charts require exactly one default StorageClass. The coordinator
does not silently select the filesystem/no-Quay fallback.
It requires `pipelineNamespace: tssc-app-ci` because the current Tekton RBAC has
hardcoded subjects; another namespace requires deliberate RBAC reconciliation.

The coordinator preserves RHTPA's upstream `1` CPU request for both server and
importer when current schedulable worker headroom can fit them. Otherwise it uses
`components.rhtpaOperator.scheduling.fallbackCpuRequest` (default `100m`), after
confirming both pods can fit. The preferred and fallback values can be changed in
the private values file. Only those two CPU requests are patched; memory requests,
CPU limits, and ODF resources are not changed. Argo is configured to ignore only
those two RHTPA CR fields so self-heal does not undo the scheduling override.

For clusters with a private ingress CA, the coordinator reads only the public
`tls.crt` from `openshift-ingress-operator/router-ca` and creates an
`ssc-router-ca` ConfigMap in the enabled RHTPA namespaces. It refuses to
overwrite a ConfigMap with different data. The Keycloak hook uses `curl
--cacert`; the RHTPA server receives the bundle through `SSL_CERT_FILE`. TLS
verification stays enabled.
Workstation `/etc/hosts` entries affect only workstation route resolution and do
not propagate into pods.

The manual Helm path defaults `rhtpaPrerequisites.caConfigMap` to empty, so its
Keycloak hook does not require a ConfigMap on clusters with publicly trusted
ingress. A manual private-CA deployment must create a ConfigMap containing
`ca-bundle.crt` in the RHTPA namespace and set
`components.rhtpaPrerequisites.caConfigMap` to its name. The coordinator sets
this value explicitly when it creates/reuses its ingress CA ConfigMap.

Artifactory is prepared and awaited first, excluding placeholder Secret stubs.
Then bootstrap Applications are created/reused one at a time in configured
sync-wave order, waiting for each Argo sync operation to finish before starting
the next. Some existing Jobs intentionally wait for resources from later
Applications, so health gates run only after all Applications have synced. The
coordinator enforces this order itself; Application annotations alone do not
serialize independently managed Applications. It stops before starting later
components if an earlier Application fails or any pod is Unschedulable. Existing runtime
credentials are checked before any provisioning Job can change a client; a
bring-your-own Keycloak must already accept the supplied `tpa-cli` credentials.
Missing runtime Secrets are then initialized. Non-environment Applications must
be Synced and Healthy with a successful completed sync operation; Git-sourced
Applications must report the commit fetched for this deployment, not stale
health from an older revision. Generated workloads, authenticated Artifactory
repository configuration, and OIDC/RHTPA API access are also gated, even with
`--skip-data`. Seeded credentials/test-generation Pipelines are checked. Credential-bearing
subprocess output is withheld, including errors; never enable credential logging
to diagnose a failure. This is not a transactional install or automatic rollback.

With RHTPA enabled, the coordinator reuses
`pipelines/ops/rhtpa-load-lightwell-data.sh` to load and verify remediation data.
It runs `--verify` first and skips uploads when the expected data is already
present. Missing or incomplete data triggers a load and verification. A partial
load can re-upload documents and create new import versions; the newest `modified`
record wins. `--skip-data` explicitly skips both loading and verification and
logs that skip; remediation data remains unverified. The coordinator does not
pass `--insecure`: remote fetches, runtime probes, and loader HTTPS calls retain
TLS verification and fail on trust issues. When workstation DNS cannot resolve
cluster routes, runtime probes open a temporary port-forward to a Ready ingress
pod and validate the route against the public ingress certificate; this creates
no cluster resources. The process is closed on exit. Configure trust and
reachability for the data loader (`CURL_CA_BUNDLE` when needed). Authorization
headers are not forwarded through redirects.
Readiness is an observed snapshot, not a guarantee of later API availability.
Existing chart TLS/Route behavior is not changed.

`--smoke-test` is opt-in: add it to apply to create and wait for the §11
`agentic-cve-analysis` PipelineRun, which builds/pushes an image, uploads an SBOM,
and creates CVE issues. It requires GitLab, Quay, and RHTPA. Without the flag no
smoke PipelineRun is started. Reruns reuse the latest successful or in-progress
smoke run with the same fetched source revision and run inputs; a failed run
gets a new attempt. Application environments are explicitly reported
as awaiting their first published image tags/promotions, not certified Healthy;
even a smoke build is not proof of dev/qa/prod promotion. Existing GitLab
`/generate-tests-*`, `/remediate`, and push-to-`main` triggers remain unchanged.

### Rerunning After Failure

Rerun the same apply command with the same private values. There is no reset step
or local progress file to maintain:

- Existing GitOps installation, controller grant/sizing, matching Secrets, and
  unchanged manifests are reused instead of recreated or reapplied.
- Applications already synced to the selected commit are not force-refreshed.
  Active syncs are awaited; failed syncs get a new non-pruning attempt.
- Matching RHTPA CPU and CA settings are not patched again, avoiding a new
  server rollout just because a later step failed.
- Verified remediation data and matching successful smoke runs are skipped.
- Cluster health, credentials, workload readiness, and authenticated APIs are
  still checked every time. A previous success does not hide a current failure.

Changed credentials or conflicting Application specs still require explicit
reconciliation, not an automatic overwrite. GitOps chart Jobs retain their own
Argo lifecycle; a genuinely new source revision may cause them to run again.

## Cleanup Inventory

Start with a read-only inventory after logging into the intended cluster:

```bash
"$SSC_PYTHON" -B cluster/ssc-demo-deploy.py --cleanup-plan --values "$SSC_VALUES"
```

This checks that the logged-in cluster's apps domain matches the private values
and prints a JSON report with the API endpoint, resource identities/UIDs,
Application cascade finalizers and Argo-reported children, relevant namespaces,
PVCs/bucket claims in reviewable namespaces, and associated cluster-scoped RBAC/SCCs.
Operator Subscriptions are listed as preserved. It does not query live Secrets,
print private Helm values, render charts, clone remote sources, or require a
successful/full deployment. Missing GitOps/bucket CRDs and absent demo namespaces
are handled as a partial installation.

**This is an inventory, not a teardown executor. There is no delete flag.**
`KEEP` marks protected/shared infrastructure; `REVIEW` marks unresolved ownership,
not permission to delete. GitOps, ODF, operators, Keycloak, and Orchestrator stay
protected even when the demo configuration enables them. No namespace, PVC,
bucket, Secret, Application, or finalizer is deleted or patched.

The current deployment does not record which chart-managed resources were
newly created versus adopted. Labels and Argo tracking cannot establish that.
Argo's resource lists can be stale/incomplete, and generated/operator-managed
resources or unlabeled leftovers may not appear. This report must not be piped
into a generic delete command.

Before implementing a fresh-reset executor, resolve:

- Explicit ownership of existing resources, including cluster-scoped privileges.
- Whether demo database, artifact, image, SBOM and bucket data may be destroyed;
  inspect the live storage reclaim policies rather than assume data is retained.
- How to stop reconciliation without cascading into reused resources, and how
  to remove only demo-owned descendants using UID-checked targets.
- Which shared changes remain in place: controller grants/sizing, TektonConfig,
  console configuration, signing keys, and external Keycloak/service data.

Private values must remain outside the repository and survive any later reset.
Do not use the older name-based namespace-deletion recipe on a shared cluster.

## Demo Actions

After cluster login:

```bash
bash cluster/ssc-demo-actions.sh status
bash cluster/ssc-demo-actions.sh gitlab-info
```

Status reports Argo CD Applications, Tekton resources, and required secret names
without printing credentials. Missing resources or access errors are not hidden.
`gitlab-info` reports the demo URL and where to find the root password; it never
reads that password. `SSC_DOMAIN` overrides domain discovery for this command.

### Offline Rendering

Only Bash and `jq` are needed. Neither `oc` nor a kubeconfig is used by `--render`
or `--help`. JSON is accepted directly by Kubernetes and safely encodes parameter
values without interpolating them into YAML.

```bash
REPO_URL=https://gitlab.example.com/development/spring-boot-example-application.git \
  bash cluster/ssc-demo-actions.sh start-test-generation unit --render
```

Replace `unit` with `integration` or `functional` to select the other pipelines.
Runs bind a new `2Gi` workspace PVC and the `maven-settings` Secret. Cloning is
unauthenticated by default, matching the issue triggers for the public demo
repository. For a private repository, set `GIT_AUTH_SECRET` to an existing clone
Secret in a format supported by `pipelines/tasks/git-clone.yaml`; the SCM API
token Secret is separate and is not assumed to be a clone credential.

`REVISION` and `BASE_BRANCH` default to `main`, matching the issue triggers
(the Pipeline definitions themselves default the base branch to `master`).
The SCM host is derived from the credential-free HTTPS `REPO_URL`, not from a
reference cluster. Userinfo, query strings, and fragments are rejected to avoid
embedding URL credentials in manifests. Commit verification stays disabled,
as in the issue triggers.
Unit runs have a one-hour timeout; integration and functional runs have two hours.
Functional runs pin the four app-lifecycle tasks to the `pipeline` service account,
matching the trigger's RBAC contract. Pipeline definitions and AI configuration
are unchanged; the configured AI backend and agent-image default still apply.

### Starting a Run

**Without `--render`, `start-test-generation` creates a live PipelineRun.** It
checks login, the selected Pipeline, and required Secret names first. The run
can create branches and MRs/PRs in the application repository. Functional runs
also deploy and clean up an ephemeral application. Complete the runbook and
review the rendered manifest before starting a run:

```bash
REPO_URL=https://gitlab.example.com/development/spring-boot-example-application.git \
  bash cluster/ssc-demo-actions.sh start-test-generation functional
```

For retry-safe submission, also set `RUN_NAME`, for example
`RUN_NAME=demo-functional-01`. Repeating that command reuses an existing matching
successful/in-progress run without creating another PVC, branch, or MR. A run
with different inputs is rejected. After a failed run, inspect its TaskRuns and
choose a new name to start a fresh attempt. Without `RUN_NAME`, each explicit
`start-test-generation` invocation intentionally starts a new run.

`--help` lists image, SCM secret, issue, Maven settings, PVC size, and PVC class
overrides. `SSC_PIPELINE_NAMESPACE` defaults to `tssc-app-ci`, and
`SSC_PLATFORM_ARGOCD_NAMESPACE` defaults to `openshift-gitops`. A namespace
override only changes the helper's target: this repo has hardcoded RBAC subjects
in the Tekton manifests, so edit/install matching RBAC per the runbook before
using another namespace. No `tkn` installation is required, and no credentials
are generated or stored locally.

## Local Validation

```bash
bash -n cluster/ssc-demo-cluster.sh cluster/ssc-demo-actions.sh pipelines/ops/rhtpa-load-lightwell-data.sh
shellcheck cluster/ssc-demo-cluster.sh cluster/ssc-demo-actions.sh pipelines/ops/rhtpa-load-lightwell-data.sh
"$HOME/.venvs/ssc-demo/bin/python" -B cluster/ssc-demo-deploy.py --plan --values "$SSC_VALUES"
"$HOME/.venvs/ssc-demo/bin/python" -B -m unittest discover -s cluster/tests -v
```

Use the dependency-equipped venv above. The compact test file checks only
credential preservation, limited mutations, and rerun behavior without cluster
access. There is no fake-cluster framework. Syntax/lint, offline rendering, and
the local plan are the routine checks; an actual demo deployment is the primary
integration validation. Local checks do not prove cluster readiness.
