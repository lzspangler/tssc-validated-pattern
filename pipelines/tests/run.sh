#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

tests_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$tests_dir/../.." && pwd)"

kubectl_on_context() {
  kubectl --context "$KUBECTL_CONTEXT" --request-timeout=30s "$@"
}

kubectl_in_case() {
  kubectl_on_context --namespace "$namespace" "$@"
}

show_failure_details() {
  local selector pod
  kubectl_in_case describe "$run" || true
  case "$run" in
    pipelinerun.tekton.dev/*) selector="tekton.dev/pipelineRun=${run##*/}" ;;
    taskrun.tekton.dev/*) selector="tekton.dev/taskRun=${run##*/}" ;;
    *) return 0 ;;
  esac
  kubectl_in_case get taskruns,pods --selector "$selector" --output=wide || true
  while IFS= read -r pod; do
    kubectl_in_case logs "$pod" --all-containers=true || true
  done < <(kubectl_in_case get pods --selector "$selector" --output=name)
}

finish_case() {
  local status=$1
  if (( status != 0 )) && [[ -n "$run" ]]; then
    show_failure_details >&2
  fi

  if [[ -n "$namespace" && -z "${INTEGRATION_NAMESPACE:-}" ]]; then
    kubectl_on_context delete namespace "$namespace" --ignore-not-found --wait=false || status=1
  elif [[ -n "$run" ]]; then
    kubectl_in_case delete "$run" --ignore-not-found --timeout=60s || status=1
  fi
  exit "$status"
}

wait_for_run() {
  local status deadline=$((SECONDS + 600))
  while (( SECONDS < deadline )); do
    status="$(kubectl_in_case get "$run" \
      --output=jsonpath='{.status.conditions[?(@.type=="Succeeded")].status}')"
    case "$status" in
      True) return 0 ;;
      False)
        printf '%s failed.\n' "$run" >&2
        return 1
        ;;
    esac
    sleep 5
  done

  printf 'Timed out waiting for %s.\n' "$run" >&2
  return 1
}

run_case() {
  local case_dir=$1 manifest
  local test_case=${case_dir#"$tests_dir"/}
  if [[ ! -f "$case_dir/resources.txt" ]]; then
    printf 'Missing manifest list for %s: %s/resources.txt\n' "$test_case" "$case_dir" >&2
    exit 2
  fi

  namespace=${INTEGRATION_NAMESPACE:-}
  run=
  trap 'finish_case "$?"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  if [[ -n "$namespace" ]]; then
    kubectl_on_context create namespace "$namespace" --dry-run=client --output=yaml \
      | kubectl_on_context apply --filename -
  else
    namespace="$(kubectl_on_context create --filename - --output=jsonpath='{.metadata.name}' <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  generateName: tekton-integration-
EOF
)"
  fi
  printf '\nRunning %s in namespace %s on context %s.\n' "$test_case" "$namespace" "$KUBECTL_CONTEXT"
  while IFS= read -r manifest || [[ -n "$manifest" ]]; do
    if [[ -n "$manifest" && "$manifest" != \#* ]]; then
      kubectl_in_case apply --filename "$repo_root/$manifest"
    fi
  done < "$case_dir/resources.txt"
  for manifest in "$case_dir"/*.yaml; do
    if [[ "$manifest" != "$case_dir/run.yaml" ]]; then
      kubectl_in_case apply --filename "$manifest"
    fi
  done

  run="$(kubectl_in_case create --filename "$case_dir/run.yaml" --output=name)"
  case "$run" in
    taskrun.tekton.dev/* | pipelinerun.tekton.dev/*) ;;
    *)
      printf '%s/run.yaml must define a Tekton TaskRun or PipelineRun.\n' "$test_case" >&2
      exit 2
      ;;
  esac
  printf 'Waiting for %s.\n' "$run"
  wait_for_run
  printf '%s integration test passed.\n' "$test_case"
}

run_all() {
  local run_files run_file test_case result=0
  run_files=("$tests_dir"/{tasks,pipelines}/*/run.yaml "$tests_dir"/{tasks,pipelines}/*/*/run.yaml)
  if [[ -z "${run_files[0]:-}" ]]; then
    printf 'No integration tests found in %s.\n' "$tests_dir" >&2
    return 2
  fi
  for run_file in "${run_files[@]}"; do
    test_case=${run_file#"$tests_dir"/}
    # A separate shell keeps each case's error handling and cleanup independent.
    bash "$tests_dir/run.sh" "${test_case%/run.yaml}" || result=1
  done
  return "$result"
}

if [[ -z "${KUBECTL_CONTEXT:-}" ]]; then
  printf 'Set KUBECTL_CONTEXT to the Kubernetes context to test.\n' >&2
  exit 2
fi
if ! command -v kubectl >/dev/null 2>&1; then
  printf 'Required command not found: kubectl\n' >&2
  exit 1
fi

selection=${1:---all}
if [[ "$selection" == --all ]]; then
  run_all
elif [[ "$selection" =~ ^(tasks|pipelines)/[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?$ ]] \
  && [[ -f "$tests_dir/$selection/run.yaml" ]]; then
  run_case "$tests_dir/$selection"
else
  printf 'Unknown integration test: %s. Use TEST=tasks/<name> or TEST=pipelines/<name>.\n' "$selection" >&2
  exit 2
fi
