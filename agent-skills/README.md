# Agent skills

These are the **authoring copies** of the SKILL.md files the AI coding
agents (aider, Claude Code) use as their methodology playbooks. They live
here for readability — you can open them in an editor, link to them from
pull requests, and review them like any other source file.

**The copies the demo actually runs are in the ConfigMaps**, not here:
`pipelines/config/agent-skills-*.yaml`. Argo CD syncs those ConfigMaps
into the `tssc-app-ci` namespace, and the Tekton tasks mount them read-only
into the agent pods at `/tekton/home/agent-skills/<skill-name>/`.

## Why ConfigMaps instead of a git checkout?

The coding agents (aider in particular) need their skill files on the
**local filesystem** of the pod, not behind a git URL. Aider runs
headless inside a Tekton step — it has no git credentials, no network
access to GitHub, and no way to clone a repository mid-task. The only way
to get the skill content into the pod is through a Kubernetes volume,
and the only way to get it through a volume (without a PVC or an
init-container with credentials) is a ConfigMap.

So the pattern is:

```
agent-skills/<name>/SKILL.md          ← authoring copy (this directory)
        ↓ (manually mirrored)
pipelines/config/agent-skills-<name>.yaml  ← ConfigMap (what runs)
        ↓ (Argo CD syncs)
ConfigMap in tssc-app-ci namespace
        ↓ (Tekton task volumeMount)
/tekton/home/agent-skills/<name>/SKILL.md  ← what the agent reads
```

When you edit a skill, update **both** copies — or edit the ConfigMap
version and copy back:

```sh
cp pipelines/config/agent-skills-maven-remediation.yaml agent-skills/maven-remediation/
```

## What each skill teaches

See the `SKILL.md` in each subdirectory for its own description. The
ConfigMap equivalents are in `pipelines/config/`.
