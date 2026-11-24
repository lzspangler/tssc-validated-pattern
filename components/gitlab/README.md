# gitlab

A self-contained GitLab CE (one pod, plus Postgres and Redis) and the Ansible
Job that seeds it with everything the demo expects to find.

Adapted from `redhat-ads-tech/ocp-app-platform-demo-helm` v1.4.2 (`gitlab`),
which is what the reference cluster runs.

## What the seeding Job does

`templates/cm-gitlab-init.yaml` renders an Ansible playbook; `job-gitlab-init`
runs it once GitLab is up. In order:

1. Waits for the pod, then for `/api/v4/projects` to answer 200.
2. Mints a root personal access token by running `./bin/rails console` inside
   the GitLab pod, and stores it as the Secret `root-user-personal-token`.
   There is no API for this — a fresh GitLab has no token to authenticate the
   creation of the first token with.
3. Turns on `import_sources` (needed for step 5) and
   `allow_local_requests_from_web_hooks_and_services` (the webhook target is a
   Route on this same cluster, which GitLab otherwise refuses to call).
4. Creates the `development` group.
5. Imports `spring-boot-example-application` from GitHub, then waits for
   `import_status: finished` — `POST /projects` returns 201 as soon as the
   project row exists, long before the clone lands.
6. Creates the CVE severity labels, so the issues
   `open-cve-issues` files land on a readable board instead of auto-creating
   grey labels.
7. Commits the `.tekton/` Pipelines-as-Code definitions into the
   application repository and creates the PaC `Repository` CR — then
   registers **both** GitLab webhooks itself (PaC does not reliably create
   its own on this GitLab version):
   * the **PaC webhook** (push + merge-request + note events) against the
     PaC controller Route, carrying the `gitlab-pac-webhook` token;
   * the **issue-comment webhook** (Note Hooks only — `push_events` and
     `merge_requests_events` are set false explicitly on every create and
     update, so no push or MR event ever reaches the EventListener)
     against the `agentic-issue-commands` EventListener Route
     (`pipelines/triggers/`), carrying the `gitlab-webhook-secret` token.
     This is the only path by which a `/remediate` or `/generate-tests-*`
     comment on an **issue** starts a pipeline — PaC matches push, MR and
     MR-comment events only.
8. Mints a **project** access token per application repository and writes
   each into the pipeline namespace under its own Secret
   (`scm-auth-secret`, `scm-auth-lightwell-secret`).
9. Starts the **install-time smoke** (`demo-smoke-<component>` PipelineRun,
   one per application, from the gitops entry's `rewriteImageRepository`
   routing): it waits for the first `ci-build-publish` run the `.tekton`
   commit in step 7 triggered, then promotes that image through the gitops
   repo's dev/qa/prod values files once each — so every environment
   Application lands Healthy instead of sitting in ImagePullBackOff on the
   initial `latest` tag (see `pipelines/pipelines/demo-smoke.yaml`).

Steps 5–9 are additions. Upstream imports repositories but has no concept of
labels on them, no webhooks, and no credential hand-off; on the reference
cluster all of that was done by hand.

## Credential handling

No token in this chart is committed, and none is templated into git.

* **`gitlab-pac-token-<repo>`** (pipeline namespace, one per application
  repository) is the project access token PaC reads that repository and
  posts statuses with. One per project is not gold-plating: a GitLab
  project-bot user is a member of exactly one project, so a shared Secret
  would leave every Repository CR after the first authenticating with the
  wrong project's token. **`gitlab-pac-webhook`** is the shared secret PaC
  validates the webhook `X-Gitlab-Token` against (matched to the Repository
  CR's `webhook_secret`), and **`gitlab-webhook-secret`** is the equivalent
  for the EventListener's gitlab interceptor. The Job reuses each Secret if
  it already exists, and otherwise generates one — so consumers and GitLab
  always agree, and re-running the Job does not invalidate a webhook
  already in use.
* **`scm-auth-secret`** / **`scm-auth-lightwell-secret`** (pipeline
  namespace, keys `username`/`token`) are per-application-repository GitLab
  project access tokens at Maintainer level so `open-pr` can push a branch.
  The EventListener's cel interceptors map each event's project to its own
  Secret (see `pipelines/triggers/agentic-issue-triggers.yaml`). A project
  token's value is returned only once, at creation, so the Job skips the
  whole block if the Secret already holds a real token; if it does not, it
  revokes any stale token of the same name (unrecoverable, and they would
  otherwise pile up) and issues a fresh one.

Writing into another namespace needs rights there:
`templates/rbac-pipeline-ns.yaml` grants the `gitlab` ServiceAccount access to
Secrets in `pipeline.namespace`, and nothing else.

## Removed from upstream

| Removed | Why |
|---|---|
| Vault writes (root PAT, DevSpaces OAuth credentials) | ssc-demo has no Vault; the only consumer of a GitLab credential is the pipeline, which reads a Secret. |
| `rolebinding-vault.yaml` | Bound `edit` into the Vault namespace for the above. Replaced by the narrow Secrets-only Role in the pipeline namespace. |
| DevSpaces OAuth application + doorkeeper token-expiry patch | No DevSpaces. |
| `job-gitlab-template.yaml`, `cm-gitlab-templates.yaml` | Imported RHDH software templates. No Developer Hub here. |
| `gitlab.templates`, `quay.*`, `orchestrator.*` values | Consumed only by the above. |

## Changed

* **Route timeout 300 s.** The import in step 5 is a server-side clone held
  open on the API connection, and the pipeline's `git push` of a remediation
  branch goes through the same Route. Both run past HAProxy's 30 s default on
  a cold cluster, and surface as an opaque "failed to respond". This Route is
  created directly rather than generated from an Ingress, so the annotation
  sits on it and is not reconciled away.
* **`Replace=true` on the seeding Job.** A Job's pod template is immutable, so
  without it the first edit to the playbook fails the sync with "field is
  immutable" while the cluster quietly keeps the old seeding.
* **Hostnames derive from `cluster.subdomain`** rather than being passed in
  per-cluster.
* **Namespace references are templated.** Upstream hardcoded `gitlab` in the
  Postgres/Redis service names and in one `oc -n gitlab` call.

## Values you must set

| Value | Notes |
|---|---|
| `cluster.subdomain` | apps subdomain, no leading dot |
| `gitlab.rootPassword` | not committed; supply at install time |
| `pipeline.namespace` | where `scm-auth-secret` / `gitlab-webhook-secret` are written, and what the webhook URL is derived from |

Everything else — the group, the repository, the labels, the webhook, the
token — is in `values.yaml` under `gitlab.groups`.
