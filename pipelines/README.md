# The Tekton layer

This directory holds the demo's Tasks, Pipelines, and AI configuration —
the functional core that scans for CVEs, remediates them, and promotes
images through environments. It lives at the repo root (not under
`components/`) because it is **plain YAML applied directly by Argo CD**,
not a Helm chart.

## Directory structure

```
pipelines/
  tasks/       33 Tekton Task definitions — the building blocks
               (git-clone, maven, buildah, AI agents, RHTPA upload, ...)
  pipelines/   11 Tekton Pipeline definitions — the workflows that compose
               the tasks (ci-build-publish, agentic-cve-analysis, ...)
  triggers/    The issue-comment EventListener — the /remediate and
               /generate-tests-* slash commands on GitLab issues
  config/      AI backend configuration (ai-agent-config) and agent-skill
               ConfigMaps — mounted into task pods at runtime
```

## Why separate from components/

The repo uses two different Argo CD Application patterns:

| Pattern | Directory | How Argo reads it | Used for |
|---|---|---|---|
| **Helm chart** | `bootstrap/` + `components/` | Argo renders the chart with Helm (values, templates, helpers), then applies the result | Infrastructure components (GitLab, Quay, RHTPA, Keycloak, Vault, ...) |
| **Plain YAML directory** | `pipelines/` | Argo applies the files directly — what's in git is byte-for-byte what runs | The Tekton layer (Tasks, Pipelines, ConfigMaps) |

The split is deliberate:

- **What you see is what runs.** Editing a Task in `pipelines/tasks/`
  changes what the cluster executes on the next reconcile — no Helm
  templating in between, no wondering whether the render produced the
  right output.
- **Nothing to parameterize.** A Task's script doesn't need Helm values;
  it needs to be literal YAML. Wrapping it in a chart would add
  indirection with no benefit.
- **The x2a-fsc layout** (which this repo follows) defines `bootstrap/`
  and `components/` for the Helm layer; the Tekton resources don't fit
  that pattern because they're the application layer the components
  serve, not installable infrastructure.

## How it's wired in

Four Argo CD Applications (rendered by the `bootstrap` chart) point at
subdirectories of this directory:

| Application | Wave | Source path | What it applies |
|---|---|---|---|
| `tekton-tasks` | 4 | `pipelines/tasks` | All 33 Task CRDs |
| `tekton-config` | 4 | `pipelines/config` | `ai-agent-config` + agent-skill ConfigMaps |
| `tekton-pipelines` | 5 | `pipelines/pipelines` | All 11 Pipeline CRDs |
| `tekton-triggers` | 5 | `pipelines/triggers` | The issue-comment EventListener |

Each uses `directory: recurse: false` — Argo reads the directory as a
flat list of YAML files and applies each one directly.

At runtime, a PipelineRun (started by PaC, the EventListener, `tkn`, or
another pipeline's `finally` task) resolves its Pipeline's `taskRef`
entries against the Task CRDs these Applications installed, and the AI
tasks read their configuration from the ConfigMaps via `envFrom` and
volume mounts.

## The trigger fabric: PaC + EventListener

CI events reach the Pipelines through TWO mechanisms, split by what each
one can match:

| Event | Consumer | Wiring |
|---|---|---|
| Push to default branch | Pipelines as Code | `.tekton/push-ci.yaml` committed in the app repo + Repository CR |
| Merge request (opened) | — nothing runs | Merge-gating is off; remediation MRs merge without a verify pipeline |
| MR comment (`/generate-tests-unit`) | Pipelines as Code | `.tekton/generate-tests-comment.yaml` committed in the app repo |
| **Issue comment** (`/remediate`, `/generate-tests-*`) | **agentic-issue-commands EventListener** | `pipelines/triggers/` + Note Hook webhook |

The split exists because PaC only understands push, MR and MR-comment
webhooks — it never matches a comment on an *issue*, and the issue-comment
slash commands are the demo's interactive entry point for remediation and
test generation. The GitLab seeding job
(`components/gitlab/templates/cm-gitlab-init.yaml`) registers both webhooks
on each application repository: the PaC webhook against the PaC controller
Route, and a Note Hook against the EventListener's Route, each with its
own shared-secret token (`gitlab-pac-webhook` / `gitlab-webhook-secret`).
The EventListener's cel filters drop everything that is not an issue
comment starting with a command, so the two consumers never overlap.

## Alternative layouts (and why they were not chosen)

- **Put everything under `components/tekton/`** — would require wrapping
  the YAML in a Helm chart or using Argo's `helm` source with an empty
  chart. Adds indirection, no benefit.
- **Keep the tasks/pipelines in the application repos** — the pipelines
  reference tasks that are shared across multiple apps, and the PaC
  PipelineRuns already live in the app repos. Keeping the definitions
  centralized here means one place to edit, one sync to converge.

For detailed documentation of each pipeline, see the individual YAML
files — each has a comment header explaining what it does and how it's
triggered.
