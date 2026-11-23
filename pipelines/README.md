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

Three Argo CD Applications (rendered by the `bootstrap` chart) point at
subdirectories of this directory:

| Application | Wave | Source path | What it applies |
|---|---|---|---|
| `tekton-tasks` | 4 | `pipelines/tasks` | All 33 Task CRDs |
| `tekton-config` | 4 | `pipelines/config` | `ai-agent-config` + agent-skill ConfigMaps |
| `tekton-pipelines` | 5 | `pipelines/pipelines` | All 11 Pipeline CRDs |

Each uses `directory: recurse: false` — Argo reads the directory as a
flat list of YAML files and applies each one directly.

At runtime, a PipelineRun (started by PaC, `tkn`, or another pipeline's
`finally` task) resolves its Pipeline's `taskRef` entries against the
Task CRDs these Applications installed, and the AI tasks read their
configuration from the ConfigMaps via `envFrom` and volume mounts.

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
