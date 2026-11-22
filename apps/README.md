# Example applications

Each directory under `apps/` is one example application, with two
subdirectories:

```
apps/<name>/
  application/    the source code (Java/Maven + Containerfile)
  gitops/         the deployment Helm chart (environments/dev|qa|prod)
```

The GitLab seeding Job (`components/gitlab`) clones THIS repository and
pushes each subdirectory's files as the initial commit to the corresponding
GitLab project — no external GitHub dependency. To add a new example
application:

1. Create `apps/<new-name>/application/` with the source code
2. Create `apps/<new-name>/gitops/` with the deployment chart
3. Add two entries to `components/gitlab/values.yaml` under
   `gitlab.groups[0].repo`:

```yaml
- name: <new-name>-application
  source: apps/<new-name>/application
  defaultBranch: main
  # ... labels, pac, accessToken as needed

- name: <new-name>-gitops
  source: apps/<new-name>/gitops
  defaultBranch: main
  # ... rewriteImageRepository, accessToken as needed
```

4. If the application should have its own Argo CD environments, add entries
   to `components.appEnvironments.environments` in `bootstrap/values.yaml`
