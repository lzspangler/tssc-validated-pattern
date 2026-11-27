# TSSC validated pattern — the bootstrap chart

Everything the demo needs, other than the pipelines themselves, installed by
Argo CD from one root chart. This repository follows the
[field-sourced-content-template](https://github.com/rhpds/field-sourced-content-template)
layout (see [sabre1041/x2a-fsc](https://github.com/sabre1041/x2a-fsc)): the
RHDP deployer points a single Argo CD Application at `bootstrap/` here, the
chart renders one child Application per component, and each child sources
`components/<name>` from the same repository.

| Component | Where the chart comes from | What it gives the demo |
|---|---|---|
| `components/external-secrets` | this repo | External Secrets Operator + the Vault `ClusterSecretStore` |
| `components/vault-secrets` | this repo | generates every credential into the cluster's existing Vault |
| `components/pipelines` | this repo | OpenShift Pipelines operator, PaC, the cluster-defaults Job |
| `pipelines/` (repo root) | this repo, applied as plain YAML | the demo's 33 Tasks, 11 Pipelines, `ai-agent-config` |
| `components/keycloak-clients` | this repo | tpa-* clients/roles/scopes in the cluster's EXISTING Keycloak |
| `components/artifactory` | this repo (extras) + `charts.jfrog.io` (chart) | the Maven mirror every build resolves through |
| `components/gitlab` | vendored from `redhat-ads-tech/ocp-app-platform-demo-helm` v1.4.2, heavily reworked | the application repository, the CVE issue board, the SCM/PaC credentials |
| `components/quay` | vendored from the same, plus the robot-account wiring | the registry images are published to, and the `quay-push` credential |
| `components/rhtas` | vendored from the same | Fulcio / Rekor / CTlog / TSA / TUF for keyless signing |
| `components/rhtpa-prerequisites` | vendored from the same | RHTPA's Postgres, OIDC secret and S3 bucket |
| `components/rhtpa-operator` | RHTPA operator (`TrustedProfileAnalyzer`, `rhtpa.io/v1`), channel `stable-v3` | SBOM ingest, vulnerability analysis, backport lookup — the only RHTPA install path, required for Exploit Intelligence |
| `components/orchestrator` | this repo | Automation Orchestrator + the two remediation workflows |

## What the cluster already provides (and this chart does NOT install)

* **OpenShift GitOps** — the deployer's root Application lands in
  `openshift-gitops`.
* **Keycloak** — `components/keycloak-clients` provisions only the demo's
  clients into `keycloak.realm`; it never touches existing clients, users or
  identity-provider config. The OIDC issuer is derived as
  `https://sso.<domain>/realms/<realm>`; override `keycloak.issuerUrl` when
  the platform Keycloak is published elsewhere.
* **ODF / NooBaa** — RHTPA's s3 mode and Quay's managed object storage both
  raise `ObjectBucketClaim`s against `openshift-storage`.
* **Vault** — see *Credentials* below.
* **the AI API key** for `gpt-oss-120b-a5b` — deployer-injected
  (`ai.apiKey`) or pre-loaded in Vault.

## Credentials: generated into Vault, pulled out by ESO

No credential is a Helm value, a `REPLACE_ME` placeholder or a manual `oc
create secret`. Two components own the whole lifecycle:

* **`vault-secrets`** (wave 1) authenticates to the cluster's Vault with the
  platform token (Secret `vault-token` in the Vault namespace) and generates
  each credential at `secret/tssc-validated-pattern/<name>` only if absent —
  re-running never rotates a credential a running workload depends on. The
  one upsert is the AI API key, which is externally supplied with an expiry.
* **`external-secrets`** (wave 0) installs ESO from `redhat-operators`
  (`openshift-external-secrets-operator`) and creates the `vault-cluster`
  `ClusterSecretStore` against the same Vault. Every component renders
  `ExternalSecret` manifests against that store, keeping the Secret names
  and keys the workloads already read (`tssc-gitlab-root-password`,
  `tssc-quay-admin`, `tssc-orchestrator-db`, `oidc-tpa-cli`, `tpa-secret`,
  `ai-agent-secret`, `artifactory-*`).

The load-bearing one is `tpa-cli-client-secret`: the SAME Vault entry backs
the Keycloak client (`keycloak-clients`), RHTPA (`oidc-tpa-cli`) and the
pipelines (`tpa-secret`), so the three cannot drift — a mismatch would
surface only as RHTPA 401s. Two components substitute Vault values at
runtime where a Helm render cannot reach: `orchestrator`'s postgres init
SQL carries a `__DB_PASSWORD__` placeholder filled from the Secret, and
`keycloak-clients`' config.json carries `__ENV:TPA_CLI_CLIENT_SECRET__`.

The `maven-settings` Secret is harvested rather than generated: the
`artifactory` component runs a sync Job that builds `settings.xml` from the
same Vault-backed admin password and writes it into the pipeline namespace.

## The Tekton layer and Pipelines as Code

The operator and the resources it runs are installed separately, because
they are separate problems: `components/pipelines` subscribes to the
operator, and the demo's own Tekton resources are plain YAML applied
straight from `pipelines/` at the repo root. Four Applications:

| Application | Source | Wave |
|---|---|---|
| `tekton-tasks` | `pipelines/tasks/` — 33 Tasks | 4 |
| `tekton-config` | `pipelines/config/` — `ai-agent-config` + agent-skills | 4 |
| `tekton-pipelines` | `pipelines/pipelines/` — 11 Pipelines | 5 |
| `tekton-triggers` | `pipelines/triggers/` — issue-comment EventListener | 5 |

CI triggering is split in two. Push, merge-request and MR-comment events go
through **Pipelines as Code**: the GitLab seeding job commits `.tekton/`
PipelineRuns (rendered per cluster from
`components/gitlab/templates/cm-pac-pipelines.yaml`) into the application
repository, mints a PaC project token per application repo
(`gitlab-pac-token-<repo>`), creates the
PaC `Repository` CR, and registers the PaC webhook. Issue-comment slash
commands (`/remediate`, `/generate-tests-*`) go through the
**`agentic-issue-commands` EventListener**, because PaC never matches a
comment on an issue; the seeding job points a Note Hook webhook at its
Route with the `gitlab-webhook-secret` token. The committed PipelineRuns
reference the cluster-installed Pipelines by `pipelineRef`, so the pipeline
definitions stay under Argo's selfHeal while the triggering lives in the
repository it watches.

**`pipelineNamespace` is effectively fixed at `tssc-app-ci`** — RBAC under
`pipelines/tasks/` hardcodes it, and a plain-YAML Application cannot
template it. The bootstrap render fails with an explanation if you change
it without those files.

## Where this came from

The reference cluster this reproduces is not built from this repository. Its
platform comes from `redhat-ads-tech/ocp-app-platform-demo-helm` at tag
`v1.4.2` — a Red Hat workshop environment that also ships Developer Hub,
Vault, DevSpaces, Quay and an orchestrator. The charts here are that
upstream with everything the demo does not use removed, the per-cluster
values collapsed onto a single `deployer.domain`, and the field-content
conventions added: `demo.redhat.com/application` labels on every child
Application and `demo.redhat.com/userinfo` ConfigMaps where components have
something an operator should read. Per-component deviations are documented
at the top of each chart's `values.yaml`.

## Install

The RHDP deployer does it — see **[`RUNBOOK.md`](../RUNBOOK.md)**. To render
by hand (for inspection or a non-RHDP cluster with the same prerequisites):

```sh
helm template tssc bootstrap --set deployer.domain=apps.<cluster-domain> \
  | oc apply -f -
```

The waves then run themselves: external-secrets (0) → vault-secrets (1) →
pipelines operator (2) → Tekton tasks + config (4) → Tekton pipelines (5) →
keycloak-clients (5) → artifactory (6/7) → gitlab (8) → quay + rhtas +
rhtpa-prerequisites + orchestrator (10) → rhtpa-operator (15) → app
environments (16). A wave does not start until the previous one is Healthy.

The Tekton Applications usually fail their first sync or two — the
operator's CRDs are still being installed when wave 4 is reached. That is
what the 30-attempt retry and `SkipDryRunOnMissingResource` are for.

## Storage

RHTPA stores SBOMs and advisory documents in object storage: the
prerequisites chart creates an `ObjectBucketClaim` against
`openshift-storage.noobaa.io`, and RHTPA reads the resulting Secret. The
cluster ships ODF, so that is the only mode wired here.

Quay wants a NooBaa bucket too, raised by its operator from the managed
`objectstorage` component. Quay's Application also ships a pre-flight Job
that raises the default BackingStore's memory from ODF's 256Mi to 1Gi —
below ~400Mi the agent pod is rejected and no bucket is ever provisioned.
See `components/quay/README.md`.

The prerequisites chart still carries RHTPA's `filesystem` fallback
(`storage.type: filesystem`) for clusters without ODF, but it mounts one RWO
PVC into both the server and importer Deployments — it works for a
single-node-ish demo and is fragile anywhere else. S3 is the path to prefer.

## Timeouts

Two routes need HAProxy's `timeout server` raised from its 30 s default, for
reasons documented where they are set:

* **RHTPA** (`bootstrap/values.yaml`, `components.rhtpaOperator.routeTimeout`)
  — `/purl/recommend` cannot complete inside 30 s at any batch size. RHTPA's
  Route is generated from an Ingress by the ingress-to-route controller and
  carries an `ownerReference`, so annotating the Route directly is reverted;
  the annotation goes on the Ingress through the chart's
  `ingress.additionalAnnotations`.
* **GitLab** (`components/gitlab/templates/rt-gitlab.yaml`) — the seeding
  job's repository import is a server-side clone of a GitHub repository held
  open on the API connection, and the pipelines push remediation branches
  through the same Route. This Route is created directly, so the annotation
  sits on it.

Artifactory has the same problem and solves it differently — the pipeline
talks to its in-cluster Service and skips the router entirely.

## Things to know before you edit

* **Every Application has `selfHeal: true`.** An `oc edit` or `oc annotate`
  against a managed resource is reverted on the next reconcile. Change the
  chart. The two deliberate exceptions are the cluster-specific fields the
  cluster-defaults Job owns (the gitops-cd-* param defaults and
  `ai-agent-config`'s AI_MODEL/AI_BASE_URL), which the corresponding
  Applications ignore via `ignoreDifferences`.
* **The GitLab, Quay and vault-secrets Jobs are `Replace=true`.** A Job's
  pod template is immutable, so editing a playbook would otherwise fail the
  sync with "field is immutable" and silently keep the old seeding. Replace
  is also how you re-seed: change the ConfigMap and sync. The corollary is
  that those playbooks re-run on every chart edit, so every API call in them
  has to tolerate its own previous success — and the vault-secrets
  generator must never rotate an existing credential.
* **`quay-admin-token` is the only copy of a token that cannot be
  reissued.** Quay's `/api/v1/user/initialize` works exactly once, while
  the user table is empty. The seeding job stores the token it returns in
  that Secret; nothing renders it from the chart, so Argo does not track it
  and will not prune it. Delete it on a live registry and the job can no
  longer authenticate.
* **The Tekton Applications sync plain YAML from `pipelines/`**, so nothing
  about them is templated — editing a Task or Pipeline there changes what
  the cluster runs on the next reconcile, with no Helm values in between.
  The flip side is that `selfHeal` will revert an `oc edit task ...`, and
  anything that needs to vary per cluster (the hardcoded pipeline namespace,
  the stale RHTAS defaults on the pipelines) has to be fixed in the file.
* **The RHTPA CRD accepts any spec shape** (`x-kubernetes-preserve-unknown-
  fields`, no property schema), so a misspelt key is not rejected — it
  silently has no effect. And the chart's empty-object scaffolding
  (`migrateDatabase`, `createDatabase`) must not be tidied away: the
  templates `deepCopy` those keys and the render aborts if they are absent.
  See `components/rhtpa-operator/README.md`.
