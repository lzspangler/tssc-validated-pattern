# Runbook — the TSSC validated pattern on a field-sourced-content cluster

This is the ordered walk-through for standing the demo up on a cluster that
ships OpenShift GitOps, Keycloak, ODF storage, Vault and an AI API key for
`gpt-oss-120b-a5b`. There is deliberately almost nothing to do by hand: the
RHDP deployer points one Argo CD Application at `bootstrap/` in this repo,
the bootstrap chart renders one Application per component, and **every
credential is generated into Vault and pulled back out by External Secrets
Operator** — no passwords are typed, pasted or committed at any point.

`bootstrap/README.md` explains *why* each component is shaped the way it is;
this file is the ordered list of what to check.

## 0. What the deployer must provide

When ordering **Field Sourced Content - OpenShift (Dedicated Clusters)** from
RHDP:

* **Existing GitOps repo**: this repository (or a fork), revision `main`,
  path `bootstrap`.
* The deployer injects `deployer.domain` (the cluster's apps subdomain).
* The AI/LLM option provides the `gpt-oss-120b-a5b` key; it lands in
  `ai.apiKey` and is stored into Vault by the `vault-secrets` component. If
  your ordering path does not inject it, pre-load Vault instead (see §1).

Cluster-side prerequisites to confirm before ordering:

> **Argo CD controller memory is handled by the install**: the stock 2Gi
> limit OOM-kills the controller during this bootstrap (verified on
> cluster-dl265), so the `pipelines` component's platform-config Job
> merge-patches it to 8Gi at wave 3-4, before the heavy waves. Only the
> first, light waves (vault, external-secrets) run at the stock limit. If
> you would rather not wait even that long, patch it by hand before
> ordering:
> ```sh
> oc -n openshift-gitops patch argocd openshift-gitops --type=merge \
>   -p '{"spec":{"controller":{"resources":{"requests":{"memory":"2Gi"},"limits":{"memory":"8Gi"}}}}}'
> ```

```sh
oc whoami                                                        # cluster-admin
oc -n openshift-gitops get pods | grep application-controller     # Running
oc get storagecluster -A                                         # ODF present
oc get oauth cluster -o jsonpath='{range .spec.identityProviders[*]}{.name}{" "}{.type}{"\n"}{end}'
```

The last three matter because the demo *uses* the cluster's own Keycloak and
Vault rather than installing either:

* **Vault**: `vault.deploy` defaults to **true** — the `vault` component
  installs a single-node Vault (self-unsealing, file storage) and its setup
  Job publishes the root token as Secret `vault-token` in the Vault
  namespace, which is exactly what `vault-secrets` and the
  `ClusterSecretStore` authenticate with. On a cluster that ships its own
  Vault, set `vault.deploy: false` and point `vault.address` /
  `vault.tokenSecretName` at it (the token needs read/write on the KV v2
  mount `secret/`).
* **Keycloak**: the demo provisions its clients into `keycloak.realm`
  (default `backstage`) and derives the OIDC issuer as
  `https://sso.<domain>/realms/<realm>`. If the platform Keycloak lives at a
  different host or serves a different realm, set `keycloak.issuerUrl`
  (and `components.keycloakClients.keycloakUrl`) in the bootstrap values,
  and make sure Secret `keycloak-initial-admin` (operator-generated) is
  readable in its namespace.

## 1. What the install does with credentials

Nothing is a Helm value. Two components own the whole credential lifecycle:

* **`vault-secrets`** (wave 1) — a re-runnable Job that, for every
  credential, checks `secret/tssc-validated-pattern/<name>` in Vault and
  generates it only if absent: `gitlab-root-password`,
  `quay-admin-password`, `tpa-db-password`, `orchestrator-db-password`,
  `tpa-cli-client-secret`, `artifactory-admin-password`,
  `artifactory-keys`, `artifactory-postgres`, and `ai-api-key`. The AI key
  is the one upsert: a deployer-injected key (30-day expiry) refreshes the
  entry on every order; if none is injected, a pre-loaded key is left alone.
* **`external-secrets`** (wave 0) — the External Secrets Operator from
  `redhat-operators` plus a `ClusterSecretStore` (`vault-cluster`) that
  fronts the cluster's Vault with the same platform token. Every component
  renders `ExternalSecret` manifests against it, so the Secrets the
  workloads read (`tssc-gitlab-root-password`, `tssc-quay-admin`,
  `tssc-orchestrator-db`, `oidc-tpa-cli`, `tpa-secret`, `ai-agent-secret`,
  `artifactory-*`) are materialized, never rendered.

One credential is *harvested* rather than generated: the
`maven-settings` Secret (the settings.xml every build mirrors through
Artifactory) is written into the pipeline namespace by a Job in the
`artifactory` component, from the same Vault-backed admin password.

## 2. What lands, in wave order

```
 0  vault                single-node Vault + the vault-token Secret
 1  external-secrets     ESO operator + the Vault ClusterSecretStore
 1  vault-secrets        credential generation into Vault
 2  pipelines            OpenShift Pipelines operator (+ PaC)
 4  tekton-tasks, tekton-config, (5) tekton-pipelines
 5  keycloak-clients     tpa-* clients in the cluster's existing Keycloak
 6  artifactory          SCC/Route/repo-bootstrap + Vault-backed secrets
 7  artifactory-chart    the JFrog chart itself (from charts.jfrog.io)
 8  gitlab               GitLab + seeding (slow: imports two GitHub repos)
10  quay, rhtas, rhtpa-prerequisites, orchestrator
15  rhtpa-operator       RHTPA 3.x via the operator
16  spring-boot-example-{dev,qa,prod}
```

A wave only starts once the previous one is Healthy, so one Application
stuck in `Progressing` blocks everything behind it.

## 3. Watch it converge

```sh
watch "oc -n openshift-gitops get applications -l app.kubernetes.io/part-of=tssc-validated-pattern \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'"
```

Expected behaviour while you wait:

* **The Tekton Applications fail their first sync or two** — wave 4 is
  reached before the operator finishes installing its CRDs. The 30-attempt
  retry and `SkipDryRunOnMissingResource` exist for exactly this.
* **GitLab sits in `Progressing` for 10–20 minutes.** The seeding job does a
  server-side clone of two GitHub repositories, then mints the SCM/PaC
  tokens, commits the `.tekton/` PaC definitions into the application repo
  and creates the PaC Repository CR.
* **Quay takes ~15 minutes** and its `quay-noobaa` Job runs first, patching
  ODF's default BackingStore from 256Mi to 1Gi.
* **Orchestrator sits in `Progressing` for 10–15 minutes** while the
  platform boots and the setup Job imports the two workflows.
* **The three `spring-boot-example-*` Applications sit `Degraded`
  (ImagePullBackOff)** until the first `ci-build-publish` run promotes a
  real image tag. Wave 16 is last on purpose; this blocks nothing.

If something is stuck, the Application's `.status.operationState.message`
and then the Job logs are where the answer is:

```sh
oc -n vault           logs job/vault-secrets-generate
oc -n gitlab          logs job/initialize-gitlab
oc -n quay-registry   logs job/quay-noobaa
oc -n artifactory     logs job/artifactory-maven-settings
oc -n automation-orchestrator-operator-system logs job/orchestrator-setup
```

## 4. Verify

```sh
# Platform
oc -n openshift-gitops get applications -l app.kubernetes.io/part-of=tssc-validated-pattern
oc -n quay-registry get quayregistry quay
oc -n trusted-artifact-signer get securesign securesign

# Credentials — all Vault-sourced, none hand-made
oc -n tssc-app-ci get secret scm-auth-secret scm-auth-gitops-secret \
    quay-push gitlab-pac-token tpa-secret ai-agent-secret maven-settings
oc -n tssc-app-ci get externalsecrets
oc -n tssc-app-ci logs job/tekton-cluster-defaults   # "already correct for <domain>"

# Tekton layer
oc -n tssc-app-ci get tasks | wc -l                  # 33 + header
oc -n tssc-app-ci get pipelines                      # 11 (see pipelines/README.md)
oc -n tssc-app-ci get repositories.pipelinesascode   # spring-boot-example-application
oc -n tssc-app-ci get secret ai-agent-secret \
  -o jsonpath='{.data}' | python3 -c 'import sys,json;print(sorted(json.load(sys.stdin)))'
# must print exactly ['OPENAI_API_KEY'] -- an AI_PROVIDER key here would
# silently override the ConfigMap

# GitLab: the seeded application repo carries the PaC definitions
oc -n tssc-app-ci get repository spring-boot-example-application -o jsonpath='{.spec.url}{"\n"}'
```

Then log into GitLab (`https://gitlab-gitlab.<domain>`, root — the password
is in Vault at `secret/tssc-validated-pattern/gitlab-root-password`) and
confirm `development/spring-boot-example-application` exists with the CVE
labels, a `.tekton/` directory, and **webhooks created by PaC** (push +
merge request events, pointing at the Pipelines-as-Code route).

## 5. Verify the Lightwell remediation data in RHTPA

Without this the demo still runs, but there is no backport for the AI to
recommend:

The `components/ops` component runs the `rhtpa-load-lightwell-data` Job
automatically at wave 16 (after RHTPA is healthy). Check its output:

```sh
oc -n tssc-app-ci logs job/rhtpa-load-lightwell-data
# ...should end "OK: Lightwell data loaded and verified"
```

The Job authenticates via the Vault-backed `ops-tpa-credentials` Secret and
runs the same load + verify logic the manual script used. If it fails, its
log names the specific OSV record or SBOM that was rejected.

## 6. Smoke test — everything now flows through Pipelines as Code

Push any commit to `main` in the GitLab application repo (or merge an MR).
That single event drives the whole chain:

1. **PaC** matches `.tekton/push-ci.yaml` (`on-event: [push]`,
   `on-target-branch: [main]`) and starts `ci-build-publish`: build → image
   to `quay.<domain>/tssc/…` → SBOM → RHTPA upload → finally task starts
   `gitops-cd-dev`, which promotes the tag into the gitops repo — which is
   what turns `spring-boot-example-dev` healthy.
2. **Open an MR** and PaC runs `.tekton/pull-request-verify.yaml`
   (clone + `mvn verify`); the outcome is reported back to the MR as a
   commit status, and `only_allow_merge_if_pipeline_succeeds` on the project
   makes a red run block the merge.
3. **Comment `/generate-tests-unit` on an open MR** (tech preview) and PaC
   starts `agentic-test-generation-unit` for the MR's head.

The operator-invoked pipelines are started by hand (they were never
webhook-driven in this layout):

```sh
cat > /tmp/ws.yaml <<'EOF'
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 5Gi
EOF

tkn -n tssc-app-ci pipeline start agentic-cve-analysis \
  -p component-name=spring-boot-example-application \
  -p git-url=https://gitlab-gitlab.$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')/development/spring-boot-example-application.git \
  -p revision=main \
  -p output-image=quay.$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')/tssc/spring-boot-example-application:smoke \
  -w name=workspace,volumeClaimTemplateFile=/tmp/ws.yaml \
  -w name=maven-settings,secret=maven-settings \
  -w name=git-auth,secret=scm-auth-secret \
  --showlog
```

Success looks like: the image appears in Quay, the SBOM uploads to RHTPA,
GitLab issues open with the recommended Lightwell backport. Start
`agentic-cve-remediation` the same way (`-p base-branch=main`) once an issue
exists.

## Known rough edges

* **`pipelineNamespace` is effectively fixed at `tssc-app-ci`** — RBAC under
  `pipelines/tasks/` and the PaC wiring hardcode it; the bootstrap render
  fails with an explanation if you change it.
* **Everything has `selfHeal: true`.** An `oc edit` against any managed
  resource is reverted on the next reconcile — change the chart, or for the
  Tekton layer the file under `pipelines/`, and push. The two exceptions are
  exactly the cluster-specific fields the cluster-defaults Job owns
  (gitops-cd-* param defaults, `ai-agent-config`'s AI_MODEL/AI_BASE_URL).
* **`quay-admin-token` cannot be reissued.** Quay's
  `/api/v1/user/initialize` works once, while the user table is empty.
  Nothing renders that Secret, so Argo will not prune it — but if you delete
  it, the seeding job can no longer authenticate.
* **RHTAS defaults in the pipelines are stale** (`rekor-url`, `tuf-mirror`,
  `oidc-issuer` name namespaces this install does not create). They are
  inert while `verify-commit: 'false'`, the default. Fix them in the
  pipeline files before turning commit verification on.
* **MR-comment command matching is a PaC tech preview** — the
  `/generate-tests-unit` on-comment trigger is the pattern; extend it to the
  other test-generation pipelines by copying
  `.tekton/generate-tests-comment.yaml` with a new regex and pipelineRef.

## Tearing it down

Delete the RHDP order (or the root Application). To clean up by hand:

```sh
helm template tssc bootstrap --set deployer.domain=<domain> \
  | oc delete -f - --ignore-not-found
oc delete ns gitlab quay-registry trusted-artifact-signer \
             trusted-profile-analyzer artifactory tssc-app-ci \
             automation-orchestrator-operator-system external-secrets-operator \
             spring-boot-example-dev spring-boot-example-qa spring-boot-example-prod \
             --ignore-not-found
# Optionally scrub the generated credentials:
#   vault kv delete secret/tssc-validated-pattern/<name>  (per entry)
```

Deleting the Applications leaves the operator Subscriptions in
`openshift-operators`, the artifactory SCC and the ODF BackingStore patch in
place; all are harmless and reapplied by a reinstall. The Vault entries
survive too — that is deliberate, so a re-order on the same cluster reuses
rather than rotates them.
