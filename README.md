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


## Provisioning via Red Hat Demo Platform Field Source Content

1. Order the **Field Sourced Content - OpenShift (Dedicated Clusters)** CI from [Red Hat Demo Platform](https://catalog.demo.redhat.com/catalog/babylon-catalog-prod?item=babylon-catalog-prod/published.ocp-field-asset.prod&utm_source=webapp&utm_medium=share-link).
2. Select the following options:
* Cloud Provider: CNV
* Cluster Size: Multinode
* OpenShift Worker Count: 4
* OpenShift Worker memory: 32Gi
* OpenShift Worker CPU: 16
* Existing GitOps Repo
  * GitOps Repo: https://github.com/project-lightpath/tssc-validated-pattern (or a fork)
  * GitOps Revision: main
  * GitOps Path: bootstrap
* Select "Enable LiteMaaS/MaaS API Keys (for AI/LLM demos)" (under "AI/LLM Configuration")
  * Model: gpt-oss-120b-a5b
  * Expiry: 30d


## Quick Start  

1. Confirm demo installation is finished/complete by checking the ArgoCD applications:  

```
$ oc get application -A
NAMESPACE          NAME                       SYNC STATUS   HEALTH STATUS
openshift-gitops   artifactory                Synced        Healthy
openshift-gitops   artifactory-chart          Synced        Healthy
openshift-gitops   external-secrets           Synced        Healthy
openshift-gitops   field-content              Synced        Healthy
openshift-gitops   gitlab                     Synced        Healthy
openshift-gitops   keycloak-clients           Synced        Healthy
openshift-gitops   lightwell-java-demo-dev    Synced        Healthy
openshift-gitops   lightwell-java-demo-prod   Synced        Healthy
openshift-gitops   lightwell-java-demo-qa     Synced        Healthy
openshift-gitops   ops                        Synced        Healthy
openshift-gitops   orchestrator               Synced        Healthy
openshift-gitops   pipelines                  Synced        Healthy
openshift-gitops   quay                       Synced        Healthy
openshift-gitops   rhtas                      Synced        Healthy
openshift-gitops   rhtpa-operator             Synced        Healthy
openshift-gitops   rhtpa-prerequisites        Synced        Healthy
openshift-gitops   spring-boot-example-dev    Synced        Healthy
openshift-gitops   spring-boot-example-prod   Synced        Healthy
openshift-gitops   spring-boot-example-qa     Synced        Healthy
openshift-gitops   tekton-config              Synced        Healthy
openshift-gitops   tekton-pipelines           Synced        Healthy
openshift-gitops   tekton-tasks               Synced        Healthy
openshift-gitops   tekton-triggers            Synced        Healthy
openshift-gitops   vault                      Synced        Healthy
openshift-gitops   vault-secrets              Synced        Healthy
```


2. To manually run the agentic-cve-analysis pipeline for both example applications (spring-boot-example-application and lightwell-java-demo-app). This can be done at any point to reset the demo with fresh issues (old issues must be closed before new ones will be created for the same app/CVE):

```

export CLUSTER_DOMAIN=<cluster domain>

cat > /tmp/ws.yaml <<'EOF'
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 5Gi
EOF

tkn pipeline start agentic-cve-analysis -n tssc-app-ci \
  --use-param-defaults \
  --prefix-name cve-analysis-spring-boot- \
  --pipeline-timeout 3h \
  -p component-name=spring-boot-example-application \
  -p git-url=https://gitlab-gitlab.apps.$CLUSTER_DOMAIN/development/spring-boot-example-application.git \
  -p revision=main \
  -p output-image=quay.apps.$CLUSTER_DOMAIN/tssc/spring-boot-example-application:analysis \
  -p git-host=gitlab-gitlab.apps.$CLUSTER_DOMAIN \
  -p scm-provider=gitlab \
  -p scm-secret-name=scm-auth-secret \
  -w name=workspace,volumeClaimTemplateFile=/tmp/ws.yaml \
  -w name=maven-settings,secret=maven-settings \
  -w name=git-auth,secret=scm-auth-secret

tkn pipeline start agentic-cve-analysis -n tssc-app-ci \
  --use-param-defaults \
  --prefix-name cve-analysis-lightwell- \
  --pipeline-timeout 3h \
  -p component-name=lightwell-java-demo-app \
  -p git-url=https://gitlab-gitlab.apps.$CLUSTER_DOMAIN/development/lightwell-java-demo-app.git \
  -p revision=main \
  -p output-image=quay.apps.$CLUSTER_DOMAIN/tssc/lightwell-java-demo-app:analysis \
  -p git-host=gitlab-gitlab.apps.$CLUSTER_DOMAIN \
  -p scm-provider=gitlab \
  -p scm-secret-name=scm-auth-lightwell-secret \
  -w name=workspace,volumeClaimTemplateFile=/tmp/ws.yaml \
  -w name=maven-settings,secret=maven-settings \
  -w name=git-auth,secret=scm-auth-lightwell-secret
```

2. Retrieve credentials for the demo components. All credentials are generated during the demo install. Most are stored in Vault and propogated to target namespaces via the External Secrets Operator. Exceptions are Ansible Orchestator, where the operator generates and manages the credential; and RHTPA, where access is handled by Keycloak.

```
# GitLab root user
oc get secret tssc-gitlab-root-password -n gitlab -o jsonpath='{.data.password}' | base64 -d; echo

# Automation Orchestrator admin user
oc get secret automationorchestrator-demo-initial-admin-password \
  -n automation-orchestrator-operator-system \
  -o jsonpath='{.data.password}' | base64 -d; echo

# Artifactory admin user (trim off 'admin@*=')
oc get secret artifactory-admin -n artifactory -o jsonpath='{.data.password}' | base64 -d; echo

# RHTPA console (the "demo" Keycloak user)
oc get secret tssc-rhtpa-ui-password -n keycloak -o jsonpath='{.data.password}' | base64 -d; echo

# Vault root token (paste into the Vault UI's token method)
oc get secret vault-token -n vault -o jsonpath='{.data.token}' | base64 -d; echo
```

3. Access UI components:  

Retrieve routes and use above credentials to login:  
```
# GitLab
oc -n gitlab get route

# Automation Orchestrator
oc -n automation-orchestrator-operator-system get route

# Artifactory
oc -n artifactory get route

# RHTPA
oc -n trusted-profile-analyzer get route
```

Optional: To login to the Vault UI (intentionally not exposed through a route):  

```
# Terminal 1: forward the Vault service to your laptop
oc -n vault port-forward svc/vault 8200:8200

# Terminal 2 (or a browser): open the UI
open http://localhost:8200

# Token 
oc -n vault get secret vault-token -o jsonpath='{.data.token}' | base64 -d
```

