#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash cluster/ssc-demo-actions.sh status
  bash cluster/ssc-demo-actions.sh gitlab-info
  bash cluster/ssc-demo-actions.sh start-test-generation <unit|functional|integration> [--render]

--render prints PipelineRun JSON offline; it never invokes oc. Without --render,
start-test-generation creates a run that can publish a branch and MR/PR.

Environment:
  SSC_PIPELINE_NAMESPACE   Default: tssc-app-ci (see namespace caveat in README)
  SSC_PLATFORM_ARGOCD_NAMESPACE  Default: openshift-gitops
  SSC_DOMAIN               Optional ingress domain for gitlab-info
  REPO_URL                 Required credential-free HTTPS source URL for a run
  REVISION / BASE_BRANCH   Defaults: main / main (matching issue triggers)
  SUBDIRECTORY             Default: source
  SCM_PROVIDER             gitlab (default) or github
  SCM_SECRET_NAME          Default: scm-auth-secret
  GIT_AUTH_SECRET          Optional private-repository clone secret; omitted by default
  MAVEN_SETTINGS_SECRET    Default: maven-settings
  AGENT_IMAGE              Optional agent-image parameter override
  ISSUE_IID                Optional issue reference for the MR/PR
  RUN_NAME                 Optional stable PipelineRun name; reruns reuse it
  WORKSPACE_SIZE           Default: 2Gi
  WORKSPACE_STORAGE_CLASS  Optional workspace PVC StorageClass

Commit verification remains disabled, matching the issue triggers. Deployment,
credentials, CA trust, and privileged cluster setup remain in the runbook.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
NS="${SSC_PIPELINE_NAMESPACE:-tssc-app-ci}"
ARGO_NS="${SSC_PLATFORM_ARGOCD_NAMESPACE:-openshift-gitops}"

render_run() {
  [[ -n "${REPO_URL:-}" ]] || fail 'Set REPO_URL to a credential-free HTTPS repository URL'
  [[ "$REPO_URL" =~ ^https://[^/@?#[:space:]]+/[^?#[:space:]]+$ ]] || fail 'REPO_URL must be a credential-free HTTPS URL without a query or fragment'
  case "${SCM_PROVIDER:-gitlab}" in
    gitlab|github) ;;
    *) fail 'SCM_PROVIDER must be gitlab or github' ;;
  esac
  command -v jq >/dev/null || fail 'jq is required'
  if [[ -n "${RUN_NAME:-}" ]]; then
    [[ ${#RUN_NAME} -le 63 && "$RUN_NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
      fail 'RUN_NAME must be a lowercase DNS label of at most 63 characters'
  fi
  local host="${REPO_URL#https://}"
  host="${host%%/*}"
  # JSON encoding keeps quotes/newlines in parameter values out of the manifest structure.
  jq -n --arg category "$CATEGORY" --arg namespace "$NS" \
    --arg url "$REPO_URL" --arg host "$host" --arg revision "${REVISION:-main}" \
    --arg base "${BASE_BRANCH:-main}" --arg subdir "${SUBDIRECTORY:-source}" \
    --arg provider "${SCM_PROVIDER:-gitlab}" --arg scm "${SCM_SECRET_NAME:-scm-auth-secret}" \
    --arg auth "${GIT_AUTH_SECRET:-}" --arg maven "${MAVEN_SETTINGS_SECRET:-maven-settings}" \
    --arg image "${AGENT_IMAGE:-}" --arg issue "${ISSUE_IID:-}" \
    --arg size "${WORKSPACE_SIZE:-2Gi}" --arg class "${WORKSPACE_STORAGE_CLASS:-}" --arg name "${RUN_NAME:-}" '
    ("agentic-test-generation-" + $category) as $pipeline |
    {
      apiVersion: "tekton.dev/v1", kind: "PipelineRun",
      metadata: {generateName: ($pipeline + "-"), namespace: $namespace,
        labels: {"app.kubernetes.io/part-of": "maven-build-ci", "tekton.dev/pipeline": $pipeline}},
      spec: {
        pipelineRef: {name: $pipeline},
        timeouts: {pipeline: (if $category == "unit" then "1h" else "2h" end)},
        params: ([
          {name: "git-url", value: $url}, {name: "git-host", value: $host},
          {name: "revision", value: $revision}, {name: "base-branch", value: $base},
          {name: "subdirectory", value: $subdir}, {name: "scm-provider", value: $provider},
          {name: "scm-secret-name", value: $scm}, {name: "issue-iid", value: $issue},
          {name: "verify-commit", value: "false"}]
          + if $image == "" then [] else [{name: "agent-image", value: $image}] end),
        workspaces: ([
          {name: "workspace", volumeClaimTemplate: {spec: ({accessModes: ["ReadWriteOnce"],
            resources: {requests: {storage: $size}}}
            + if $class == "" then {} else {storageClassName: $class} end)}},
          {name: "maven-settings", secret: {secretName: $maven}}]
          + if $auth == "" then [] else [{name: "git-auth", secret: {secretName: $auth}}] end)
      }
    } | if $category == "functional" then
      .spec.taskRunSpecs = (["deploy-app-under-test", "ai-generate-tests-functional",
        "reset-app-under-test", "cleanup-app-under-test"] |
        map({pipelineTaskName: ., serviceAccountName: "pipeline"}))
    else . end | if $name == "" then . else .metadata.name = $name | del(.metadata.generateName) end'
}

ACTION="${1:---help}"
case "$ACTION" in
  -h|--help) usage; exit 0 ;;
  status|gitlab-info) [[ $# == 1 ]] || { usage >&2; exit 2; } ;;
  start-test-generation)
    CATEGORY="${2:-}"
    case "$CATEGORY" in
      unit|functional|integration) ;;
      *) usage >&2; exit 2 ;;
    esac
    [[ $# == 2 || ( $# == 3 && "$3" == --render ) ]] || { usage >&2; exit 2; }
    RUN="$(render_run)"
    if [[ "${3:-}" == --render ]]; then
      printf '%s\n' "$RUN"
      exit 0
    fi
    ;;
  *) usage >&2; exit 2 ;;
esac

command -v oc >/dev/null || fail 'oc is required'
oc whoami >/dev/null || fail 'Log in to the intended cluster before running this action'
case "$ACTION" in
  status)
    oc -n "$ARGO_NS" get applications.argoproj.io \
      -o 'custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
    oc -n "$NS" get tasks,pipelines,pipelineruns
    # Names only: never print secret data or retrieve credential values.
    oc -n "$NS" get secret "${SCM_SECRET_NAME:-scm-auth-secret}" ai-agent-secret \
      "${MAVEN_SETTINGS_SECRET:-maven-settings}" -o name
    ;;
  gitlab-info)
    domain="${SSC_DOMAIN:-$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}')}"
    printf 'GitLab URL: https://gitlab-gitlab.%s\nUsername: root\n' "$domain"
    printf 'Password source: gitlabRootPassword in your private runbook values file\n'
    printf 'Pipeline namespace: %s\n' "$NS"
    ;;
  start-test-generation)
    if [[ -n "${RUN_NAME:-}" ]]; then
      existing="$(oc -n "$NS" get pipelinerun "$RUN_NAME" --ignore-not-found -o json)"
      if [[ -n "$existing" ]]; then
        jq -e --argjson desired "$RUN" '.spec | contains($desired.spec)' <<< "$existing" >/dev/null ||
          fail "Existing PipelineRun $RUN_NAME has different inputs; choose a new RUN_NAME"
        state="$(jq -r '[.status.conditions[]? | select(.type == "Succeeded")][0].status // "Unknown"' <<< "$existing")"
        [[ "$state" != False ]] || fail "PipelineRun $RUN_NAME failed; inspect its TaskRuns and choose a new RUN_NAME to retry"
        printf 'Reusing PipelineRun %s/%s (Succeeded=%s); no new run created.\n' "$NS" "$RUN_NAME" "$state"
        exit 0
      fi
    fi
    oc -n "$NS" get pipeline "agentic-test-generation-$CATEGORY" -o name >/dev/null
    oc -n "$NS" get secret ai-agent-secret "${MAVEN_SETTINGS_SECRET:-maven-settings}" \
      "${SCM_SECRET_NAME:-scm-auth-secret}" -o name >/dev/null
    if [[ -n "${GIT_AUTH_SECRET:-}" ]]; then
      oc -n "$NS" get secret "$GIT_AUTH_SECRET" -o name >/dev/null
    fi
    printf '%s\n' "$RUN" | oc -n "$NS" create -f -
    ;;
esac
