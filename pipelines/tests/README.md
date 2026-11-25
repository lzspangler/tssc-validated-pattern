# Tekton integration tests

The shared runner starts each case on an explicitly selected Kubernetes context.
The `tssc-bump-maven` case uses a test Pipeline to create a small Maven project,
run the repository Task, and check that the dependency version changes from
`3.14.0` to `3.15.0`.

## Run against Kind

Requirements are a working Docker or Podman runtime, `make`, and network access
to download Kind, kubectl, Tekton, and test images.

```sh
make kind-integration-install-tools
make kind-integration-bootstrap
make kind-integration-test
make kind-integration-down
```

The bootstrap creates the `ssc-demo-integration` Kind cluster, installs the pinned
upstream Tekton Pipelines release, and restores the previous active kubectl
context. The tests use Kind's default `standard` StorageClass for workspace PVCs.
GitHub Actions runs `make kind-integration-test` to run all cases on every PR.

Use `TEST` to run a single case on Kind.

```sh
make kind-integration-test TEST=tasks/tssc-bump-maven
```

The cluster name, Kind provider, node image, and Tekton version can be changed
through Make variables.

```sh
make kind-integration-bootstrap KIND_CLUSTER_NAME=ssc-demo-local KIND_PROVIDER=docker
make kind-integration-test KIND_CLUSTER_NAME=ssc-demo-local
make kind-integration-down KIND_CLUSTER_NAME=ssc-demo-local
```

Kind supports Docker, Podman, and nerdctl. To use Podman, select it when
creating and deleting the cluster:

```sh
make kind-integration-bootstrap KIND_PROVIDER=podman
make kind-integration-test
make kind-integration-down KIND_PROVIDER=podman
```

Podman installations may need additional host setup for Kind. The bootstrap
does not switch the active context. To make the default cluster active
manually, run:

```sh
kubectl config use-context kind-ssc-demo-integration
```

## Run against another Kubernetes cluster

The target cluster needs Tekton Pipelines and a StorageClass for cases that
request workspace PVCs.

```sh
make integration-test TEST=tasks/tssc-bump-maven KUBECTL_CONTEXT=my-kubernetes-context
make integration-test-all KUBECTL_CONTEXT=my-kubernetes-context
```

The runner creates a namespace for each case and deletes it when the case
finishes. To use a named test namespace, pass `INTEGRATION_NAMESPACE=<name>`.
With that setting, the runner deletes the run and leaves the namespace and its
applied manifests in place.

## Add a test case

Create a directory under `tasks/<name>` or `pipelines/<name>` here. For multiple
cases for the same resource, give each one a subdirectory such as
`tasks/<name>/<scenario>`.

- In `resources.txt`, list the repository-relative paths of the manifests the
  case needs, one per line in the order they should be applied.
- Put test fixtures and any test Pipeline or Task definitions in `.yaml` files
  alongside `resources.txt`. Use namespaced resources and let the runner set
  the namespace.
- Put one Tekton `TaskRun` or `PipelineRun` in `run.yaml`. Use
  `metadata.generateName` so each invocation gets a new run. Put assertions in
  the run's Task or Pipeline so a failed assertion fails the test.

The shared runner discovers each `run.yaml`, applies the listed resources and
case manifests, then waits up to ten minutes for the run. On failure it prints
the run status and logs from its Pods. The suite runs cases in sequence and
returns a failure if any case fails.
