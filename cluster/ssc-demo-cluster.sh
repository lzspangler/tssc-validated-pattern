#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash cluster/ssc-demo-cluster.sh [--preflight | --bootstrap-gitops |
       --prepare-gitops --allow-demo-cluster-admin]

Default/--preflight: read-only checks; no resources are changed.
--bootstrap-gitops: install a missing GitOps Subscription only, using the existing
  global OperatorGroup in openshift-operators. Never change an existing operator
  channel or Argo CD instance. Wait for the operator and selected Argo CD instance.
--prepare-gitops: DEMO ONLY. Requires --allow-demo-cluster-admin before any client
  calls. Grant the selected controller cluster-admin with a deterministic binding
  and set controller memory requests=2Gi, limits=8Gi; wait for StatefulSet rollout.
  Bootstrap does NOT perform these privileged/sizing changes.
Log in separately before running. All actions check login and cluster health.

Environment:
  SSC_EXPECTED_OCP_VERSION  Optional version prefix, e.g. 4.21. (no default pin)
  SSC_MIN_READY_WORKERS     Minimum Ready, schedulable workers (default: 3)
  SSC_PLATFORM_ARGOCD_NAMESPACE  Argo CD namespace (default: openshift-gitops)
  SSC_PLATFORM_ARGOCD_NAME  Argo CD instance (default: openshift-gitops)
  SSC_GITOPS_CHANNEL       Bootstrap channel (default: catalog defaultChannel)
  SSC_WAIT_TIMEOUT         Per-stage bootstrap/preparation timeout (default: 15m)
                           Positive integer duration with s, m, or h suffix
  SSC_STORAGE_MODE         odf (default), install-odf, or filesystem
  SSC_STORAGE_CLASS        PVC StorageClass (default: discover the default class)
  SSC_STORAGE_NAMESPACE    ODF namespace (default: openshift-storage)
  SSC_STORAGE_CLUSTER_NAME ODF StorageCluster (default: ocs-storagecluster)
  SSC_NOOBAA_NAME           NooBaa instance (default: noobaa)

install-odf checks filesystem storage but defers ODF readiness until installation.
filesystem requires the runbook's filesystem RHTPA / disabled Quay overrides.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

ACTION="${1:---preflight}"
case "$ACTION" in
  -h|--help) usage; exit 0 ;;
  --preflight) [[ $# -le 1 ]] || { usage >&2; exit 2; } ;;
  --bootstrap-gitops) [[ $# -eq 1 ]] || { usage >&2; exit 2; } ;;
  --prepare-gitops)
    [[ $# -eq 2 && "$2" == --allow-demo-cluster-admin ]] ||
      fail '--prepare-gitops requires --allow-demo-cluster-admin (demo-only cluster-admin grant)'
    ;;
  *) usage >&2; exit 2 ;;
esac

if [[ "$ACTION" != --preflight ]]; then
  WAIT_TIMEOUT="${SSC_WAIT_TIMEOUT:-15m}"
  [[ "$WAIT_TIMEOUT" =~ ^([0-9]{1,6})(s|m|h)$ ]] ||
    fail 'SSC_WAIT_TIMEOUT must be a positive integer duration with s, m, or h suffix'
  WAIT_SECONDS=$((10#${BASH_REMATCH[1]}))
  case "${BASH_REMATCH[2]}" in
    m) WAIT_SECONDS=$((WAIT_SECONDS * 60)) ;;
    h) WAIT_SECONDS=$((WAIT_SECONDS * 3600)) ;;
  esac
  (( WAIT_SECONDS > 0 )) || fail 'SSC_WAIT_TIMEOUT must be positive'
fi

# Bound each bootstrap/preparation API request as well as the overall wait.
client() { oc --request-timeout="$WAIT_TIMEOUT" "$@"; }

health_client() {
  if [[ "$ACTION" == --preflight ]]; then oc "$@"; else client "$@"; fi
}

wait_json() {
  local description="$1" filter="$2" deadline remaining data
  shift 2
  deadline=$((SECONDS + WAIT_SECONDS))
  while :; do
    remaining=$((deadline - SECONDS))
    (( remaining > 0 )) || fail "Timed out waiting for $description ($WAIT_TIMEOUT)"
    data="$(oc --request-timeout="${remaining}s" "$@" -o json)" ||
      fail "Unable to query $description"
    if [[ -n "$data" ]] && jq -e "$filter" <<< "$data" >/dev/null; then
      WAIT_RESULT="$data"
      return
    fi
    remaining=$((deadline - SECONDS))
    (( remaining > 0 )) || fail "Timed out waiting for $description ($WAIT_TIMEOUT)"
    if (( remaining > 2 )); then sleep 2; else sleep "$remaining"; fi
  done
}

bootstrap_gitops() {
  local subscriptions count subscription_ns subscription_name crds csvs deployments groups packages channel
  subscriptions="$(client get subscriptions.operators.coreos.com -A -o json)"
  subscriptions="$(jq '[.items[] | select(.spec.name == "openshift-gitops-operator")]' <<< "$subscriptions")"
  count="$(jq 'length' <<< "$subscriptions")"
  (( count <= 1 )) || fail 'Multiple openshift-gitops-operator Subscriptions found; resolve duplicates before bootstrap'
  if (( count == 0 )); then
    crds="$(client get customresourcedefinitions -o json)"
    jq -e '[.items[] | select(.metadata.name == "argocds.argoproj.io" or
      .metadata.name == "applications.argoproj.io" or .metadata.name == "applicationsets.argoproj.io")] | length == 0' \
      <<< "$crds" >/dev/null || fail 'Argo CRDs exist without a GitOps Subscription; identify their owner before installing a competing operator'
    csvs="$(client get clusterserviceversions.operators.coreos.com -A -o json)"
    jq -e '[.items[] | select(any(.spec.customresourcedefinitions.owned[]?;
      .name == "argocds.argoproj.io" or .name == "applications.argoproj.io"))] | length == 0' \
      <<< "$csvs" >/dev/null || fail 'An Argo operator CSV exists without a GitOps Subscription; resolve ownership before bootstrap'
    deployments="$(client get deployments -A -o json)"
    jq -e '[.items[] | select(.metadata.labels["app.kubernetes.io/part-of"] == "argocd" or
      (.metadata.name | test("argocd|gitops-operator")))] | length == 0' \
      <<< "$deployments" >/dev/null || fail 'An unmanaged Argo/GitOps deployment exists; identify its owner before bootstrap'
    groups="$(client -n openshift-operators get operatorgroups.operators.coreos.com -o json)"
    jq -e '.items | length == 1 and all(.[];
      ((.spec.targetNamespaces // []) | length == 0) and .spec.selector == null and
      (.status.namespaces == [""]))' <<< "$groups" >/dev/null ||
      fail 'Expected one usable shared global OperatorGroup in openshift-operators; do not add another OperatorGroup'
    packages="$(client -n openshift-marketplace get packagemanifests -o json)"
    packages="$(jq '[.items[] | select(.metadata.name == "openshift-gitops-operator" and
      .status.catalogSource == "redhat-operators" and .status.catalogSourceNamespace == "openshift-marketplace")]' <<< "$packages")"
    [[ "$(jq 'length' <<< "$packages")" == 1 ]] || fail 'Expected openshift-gitops-operator from redhat-operators in openshift-marketplace'
    channel="${SSC_GITOPS_CHANNEL:-$(jq -r '.[0].status.defaultChannel // empty' <<< "$packages")}"
    if [[ -z "$channel" ]] || ! jq -e --arg channel "$channel" \
      'any(.[0].status.channels[]?; .name == $channel)' <<< "$packages" >/dev/null; then
      fail 'Requested/default GitOps channel is not available in redhat-operators'
    fi
    subscription_ns=openshift-operators
    subscription_name=openshift-gitops-operator
    # No startingCSV pin: OLM resolves the selected channel from the live catalog.
    jq -n --arg channel "$channel" '{apiVersion:"operators.coreos.com/v1alpha1", kind:"Subscription",
      metadata:{name:"openshift-gitops-operator",namespace:"openshift-operators"},
      spec:{name:"openshift-gitops-operator",channel:$channel,source:"redhat-operators",
      sourceNamespace:"openshift-marketplace",installPlanApproval:"Automatic"}}' | client apply -f -
  else
    subscription_ns="$(jq -er '.[0].metadata.namespace' <<< "$subscriptions")"
    subscription_name="$(jq -er '.[0].metadata.name' <<< "$subscriptions")"
    printf 'Reusing GitOps Subscription %s/%s without changes.\n' "$subscription_ns" "$subscription_name"
  fi
  wait_json 'GitOps Subscription installedCSV' '.status.installedCSV | type == "string" and length > 0' \
    -n "$subscription_ns" get subscription "$subscription_name" --ignore-not-found
  local installed_csv
  installed_csv="$(jq -er '.status.installedCSV' <<< "$WAIT_RESULT")"
  wait_json 'GitOps CSV Succeeded' '.status.phase == "Succeeded"' \
    -n "$subscription_ns" get clusterserviceversion "$installed_csv" --ignore-not-found
  wait_json 'Argo CD CRD Established' 'any(.status.conditions[]?; .type == "Established" and .status == "True")' \
    get customresourcedefinition argocds.argoproj.io --ignore-not-found
  wait_json "Argo CD $ARGO_NS/$ARGO_NAME existence and Available" '.status.phase == "Available"' \
    -n "$ARGO_NS" get argocd "$ARGO_NAME" --ignore-not-found
  printf 'GitOps bootstrap ready. No demo cluster-admin or controller sizing changes were made.\n'
}

prepare_gitops() {
  local argo controller sa binding existing desired
  wait_json "Argo CD $ARGO_NS/$ARGO_NAME Available before preparation" '.status.phase == "Available"' \
    -n "$ARGO_NS" get argocd "$ARGO_NAME" --ignore-not-found
  argo="$WAIT_RESULT"
  controller="$(client -n "$ARGO_NS" get statefulset "$ARGO_NAME-application-controller" -o json)"
  jq -e --arg name "$ARGO_NAME" --arg uid "$(jq -er '.metadata.uid' <<< "$argo")" \
    'any(.metadata.ownerReferences[]?; .kind == "ArgoCD" and .name == $name and .uid == $uid) and
    any(.spec.template.spec.containers[]?; .name == "argocd-application-controller")' \
    <<< "$controller" >/dev/null || fail 'Controller StatefulSet must belong to the selected Argo CD instance'
  sa="$(jq -er '.spec.template.spec.serviceAccountName | select(type == "string" and length > 0)' <<< "$controller")"
  client -n "$ARGO_NS" get serviceaccount "$sa" -o name >/dev/null
  binding="ssc-demo-$ARGO_NS-$ARGO_NAME-controller-cluster-admin"
  desired="$(jq -n --arg binding "$binding" --arg ns "$ARGO_NS" --arg sa "$sa" \
    '{apiVersion:"rbac.authorization.k8s.io/v1",kind:"ClusterRoleBinding",
      metadata:{name:$binding,labels:{"app.kubernetes.io/managed-by":"ssc-demo-cluster","ssc-demo-only":"true"}},
      roleRef:{apiGroup:"rbac.authorization.k8s.io",kind:"ClusterRole",name:"cluster-admin"},
      subjects:[{kind:"ServiceAccount",name:$sa,namespace:$ns}]}')"
  existing="$(client get clusterrolebinding "$binding" --ignore-not-found -o json)"
  if [[ -n "$existing" ]]; then
    jq -e --argjson desired "$desired" '.roleRef == $desired.roleRef and .subjects == $desired.subjects' \
      <<< "$existing" >/dev/null || fail "Conflicting ClusterRoleBinding $binding; inspect it before preparation"
  else
    printf '%s\n' "$desired" | client apply -f -
  fi
  if ! jq -e '.spec.controller.resources.requests.memory == "2Gi" and
    .spec.controller.resources.limits.memory == "8Gi"' <<< "$argo" >/dev/null; then
    client -n "$ARGO_NS" patch argocd "$ARGO_NAME" --type=merge \
      -p '{"spec":{"controller":{"resources":{"requests":{"memory":"2Gi"},"limits":{"memory":"8Gi"}}}}}'
  fi
  # Argo phase may still be Available while the operator reconciles the old pod.
  wait_json 'controller StatefulSet resources and rollout ready' '
    any(.spec.template.spec.containers[]?; .name == "argocd-application-controller" and
      .resources.requests.memory == "2Gi" and .resources.limits.memory == "8Gi") and
    (.spec.replicas > 0) and (.status.observedGeneration >= .metadata.generation) and
    (.status.readyReplicas == .spec.replicas) and (.status.updatedReplicas == .spec.replicas) and
    (.status.currentRevision != null and .status.currentRevision == .status.updateRevision)' \
    -n "$ARGO_NS" get statefulset "$ARGO_NAME-application-controller" --ignore-not-found
  printf 'Demo GitOps preparation ready: controller cluster-admin granted; memory requests=2Gi, limits=8Gi.\n'
}

MIN_WORKERS="${SSC_MIN_READY_WORKERS:-3}"
STORAGE_MODE="${SSC_STORAGE_MODE:-odf}"
[[ "$MIN_WORKERS" =~ ^[0-9]+$ ]] || fail 'SSC_MIN_READY_WORKERS must be a nonnegative integer'
case "$STORAGE_MODE" in
  odf|install-odf|filesystem) ;;
  *) fail 'SSC_STORAGE_MODE must be odf, install-odf, or filesystem' ;;
esac
command -v oc >/dev/null || fail 'oc is required'
command -v jq >/dev/null || fail 'jq is required'
health_client whoami >/dev/null || fail 'Log in to the intended cluster before running preflight'

version="$(health_client get clusterversion version -o json | jq -er '.status.desired.version')"
[[ "$version" == "${SSC_EXPECTED_OCP_VERSION:-}"* ]] || fail "Unexpected OpenShift version: $version"
printf 'OpenShift: %s\n' "$version"

nodes="$(health_client get nodes -o json)"
jq -e '.items | length > 0 and all(.[];
  any(.status.conditions[]?; .type == "Ready" and .status == "True"))' \
  <<< "$nodes" >/dev/null || fail 'One or more nodes are not Ready'
ready_workers="$(jq '[.items[] | select(.metadata.labels | has("node-role.kubernetes.io/worker"))
  | select(.spec.unschedulable != true)] | length' <<< "$nodes")"
(( ready_workers >= 10#$MIN_WORKERS )) || fail "Expected $MIN_WORKERS Ready, schedulable workers; found $ready_workers"
health_client get clusteroperators -o json | jq -e '.items | length > 0 and all(.[];
  any(.status.conditions[]?; .type == "Available" and .status == "True") and
  any(.status.conditions[]?; .type == "Degraded" and .status == "False"))' \
  >/dev/null || fail 'Cluster operators must be Available and not Degraded'

ARGO_NS="${SSC_PLATFORM_ARGOCD_NAMESPACE:-openshift-gitops}"
ARGO_NAME="${SSC_PLATFORM_ARGOCD_NAME:-openshift-gitops}"
case "$ACTION" in
  --bootstrap-gitops) bootstrap_gitops; exit 0 ;;
  --prepare-gitops) prepare_gitops; exit 0 ;;
esac

oc -n "${SSC_PLATFORM_ARGOCD_NAMESPACE:-openshift-gitops}" get argocd \
  "${SSC_PLATFORM_ARGOCD_NAME:-openshift-gitops}" -o json | jq -e \
  '.status.phase == "Available"' >/dev/null || fail 'The existing Argo CD instance must be Available'

if [[ -n "${SSC_STORAGE_CLASS:-}" ]]; then
  oc get storageclass "$SSC_STORAGE_CLASS" -o name
else
  oc get storageclass -o json | jq -e '
    [.items[] | select(.metadata.annotations["storageclass.kubernetes.io/is-default-class"] == "true"
      or .metadata.annotations["storageclass.beta.kubernetes.io/is-default-class"] == "true")]
    | length == 1' >/dev/null || fail 'Expected one default StorageClass; set SSC_STORAGE_CLASS explicitly otherwise'
fi

if [[ "$STORAGE_MODE" == odf ]]; then
  storage_ns="${SSC_STORAGE_NAMESPACE:-openshift-storage}"
  oc -n "$storage_ns" get storagecluster "${SSC_STORAGE_CLUSTER_NAME:-ocs-storagecluster}" \
    -o json | jq -e '.status.phase == "Ready"' >/dev/null || fail 'ODF StorageCluster is not Ready'
  oc -n "$storage_ns" get cephcluster -o json | jq -e \
    '.items | length > 0 and all(.[]; .status.ceph.health == "HEALTH_OK")' \
    >/dev/null || fail 'Ceph must report HEALTH_OK'
  oc -n "$storage_ns" get noobaa "${SSC_NOOBAA_NAME:-noobaa}" -o json | jq -e \
    '.status.phase == "Ready"' >/dev/null || fail 'NooBaa is not Ready'
else
  printf 'ODF readiness skipped (%s). Apply the matching runbook overrides before deployment.\n' "$STORAGE_MODE"
fi
printf 'Preflight passed. No cluster resources were changed.\n'
