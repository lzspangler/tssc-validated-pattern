# TSSC Validated Pattern

Automated remediation of application CVEs, as an RHDP field-sourced-content
deployment: analysis of a containerized Java project finds vulnerable CVEs
and recommends a fix; issue-tracker work items are opened automatically; CVEs
are remediated by an AI agent; regression tests are generated; images are
promoted through gitops to dev, qa and production.

Patched CVE packages are sourced from Red Hat Lightwell
(https://www.redhat.com/en/lightwell) and upstream (Maven Central):

| Repository | URL |
| --- | --- |
| Java Validated | https://packages.redhat.com/lightwell/public-lightwell-demo/java/validated/ |
| Java Remediated | https://packages.redhat.com/lightwell/public-lightwell-demo/java/remediated/ |
| Java OSV Advisories | https://packages.redhat.com/api/pulp-content/public-lightwell-demo/osv/java/remediated/ |

## Layout (field-sourced content format)

This repository follows the [field-sourced-content-template](https://github.com/rhpds/field-sourced-content-template)
layout — the same structure as [sabre1041/x2a-fsc](https://github.com/sabre1041/x2a-fsc):

```
bootstrap/     Helm chart rendering one Argo CD Application per component
               (the RHDP deployer points Argo at path `bootstrap`)
components/    One Helm chart per component, sourced from this same repo
pipelines/     The Tekton layer: Tasks, Pipelines, the AI/agent-skills
               config, container images and the Lightwell seed data
agent-skills/  SKILL.md playbooks the AI agent tasks load
RUNBOOK.md     What the deployer must provide, and how to verify the result
```

## What the target cluster already provides

The install assumes the cluster ships, and installs none of:

* **OpenShift GitOps** (Argo CD in `openshift-gitops`)
* **Keycloak** — the `keycloak-clients` component provisions only the
  tpa-* roles/scopes/clients into it
* **ODF object storage** — NooBaa backs Quay and RHTPA's s3 mode
* **Vault** — or set `vault.deploy: true` (the default) to have the install
  bring its own single-node Vault; either way every credential is *generated
  into* Vault at install time
  (`vault-secrets` component) and pulled back out by External Secrets
  Operator (`external-secrets` component); nothing is hand-created and no
  credential is a Helm value
* **an AI API key** for the `gpt-oss-120b-a5b` model (RHDP order form /
  deployer-injected, or pre-loaded in Vault)

## Ordering

Order **Field Sourced Content - OpenShift (Dedicated Clusters)** from RHDP
with this repository (or a fork), revision `main`, path `bootstrap`. The
deployer injects `deployer.domain`; everything else derives from it. See
[RUNBOOK.md](RUNBOOK.md) for the full walk-through, and
[bootstrap/README.md](bootstrap/README.md) for why each component is shaped
the way it is.
