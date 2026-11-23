# Container images

Dockerfiles for the AI agent runtime images the pipelines use. These are
**build-time artifacts** — they are NOT synced by Argo CD and are NOT
referenced by any Application. Build and push them to a registry the
cluster can pull from before the first pipeline run.

## What each image is for

| Image | Used by | Purpose |
|---|---|---|
| `ai-python/` | `ai-analyze-cves` task | Python runtime for the CVE analysis step (calls the LLM to select must-fix CVEs from the RHTPA remediation report) |
| `ai-agent-maven-aider/` | `ai-remediate-dependency`, test-generation tasks, `start-gitops-cd-dev` | JDK 17 + Maven + aider (litellm-backed coding agent) — the image that actually edits the pom.xml and generates tests |
| `ai-agent-maven-claude/` | same tasks (when `AI_AGENT=claude-code`) | JDK 17 + Maven + Claude Code — the Anthropic alternative to aider |

## Build and push

```sh
# Pick the flavor matching your AI_AGENT (aider is the default):
podman build --platform linux/amd64 -t quay.io/<org>/ai-agent-maven-aider:v1.0.0 images/ai-agent-maven-aider
podman push quay.io/<org>/ai-agent-maven-aider:v1.0.0

# CVE-analysis runtime:
podman build --platform linux/amd64 -t quay.io/<org>/ai-python:v1.0.0 images/ai-python
podman push quay.io/<org>/ai-python:v1.0.0
```

Then set the `agent-image` and `ai-python-image` params (or edit the
defaults in `pipelines/tasks/`) to point at your registry. The demo ships
with `quay.io/sscdemo/…` as the pre-built reference.

**Note**: build with `--platform linux/amd64` even on an arm64 Mac — the
target cluster is x86_64.
