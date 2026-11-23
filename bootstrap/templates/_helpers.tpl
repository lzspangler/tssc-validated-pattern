{{/*
Labels for every Argo CD Application this app-of-apps chart creates.

`demo.redhat.com/application` is the label the RHDP field-content platform
watches for health monitoring, and `demo.redhat.com/userinfo` (on ConfigMaps
the components render) is how URLs and credentials are passed back to the
order — see the field-sourced-content-template README.
*/}}
{{- define "bootstrap.labels" -}}
demo.redhat.com/application: "tssc-validated-pattern"
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: tssc-validated-pattern
{{- end -}}

{{/*
The syncPolicy every child Application shares. The generous retry exists
because several components create custom resources whose CRDs are still being
installed by OLM when the first sync runs; without the retry those
Applications fail once and stay failed.
*/}}
{{- define "bootstrap.syncPolicy" -}}
syncPolicy:
  automated:
    prune: true
    selfHeal: true
  retry:
    limit: 30
    backoff:
      duration: 5s
      factor: 2
      maxDuration: 2m
  syncOptions:
    - CreateNamespace=true
    - RespectIgnoreDifferences=true
    - SkipDryRunOnMissingResource=true
{{- end -}}

{{/*
Common Application metadata. Usage:

  {{- include "bootstrap.metadata" (dict "name" "gitlab" "namespace" .Values.argocd.namespace "syncWave" "8" "root" .) }}
*/}}
{{- define "bootstrap.metadata" -}}
metadata:
  name: {{ .name }}
  namespace: {{ .namespace }}
  labels:
    {{- include "bootstrap.labels" .root | nindent 4 }}
  annotations:
    argocd.argoproj.io/sync-wave: {{ .syncWave | quote }}
  finalizers:
    - resources-finalizer.argocd.argoproj.io
{{- end -}}

{{/*
Derive the Keycloak OIDC issuer. The target cluster ships its own Keycloak;
if its route is not https://sso.<domain>, set keycloak.issuerUrl explicitly.
*/}}
{{- define "bootstrap.issuer" -}}
{{- if .Values.keycloak.issuerUrl -}}
{{ .Values.keycloak.issuerUrl }}
{{- else -}}
{{ printf "https://sso.%s/realms/%s" .Values.deployer.domain .Values.keycloak.realm }}
{{- end -}}
{{- end -}}

{{/*
In-cluster Vault address. The target cluster ships its own Vault; override
vault.address if it is not the conventional in-cluster Service.
*/}}
{{- define "bootstrap.vaultAddress" -}}
{{- if .Values.vault.address -}}
{{ .Values.vault.address }}
{{- else -}}
{{ printf "http://vault.%s.svc.cluster.local:8200" .Values.vault.namespace }}
{{- end -}}
{{- end -}}

{{/*
AI backend values. The field-content deployer injects the RHDP order's
LLM key as litemaas.* (the x2a-fsc convention); ai.* is this chart's own
spelling. ai.* wins when both are set, litemaas.* is the fallback.
*/}}
{{- define "bootstrap.aiApiKey" -}}
{{- .Values.ai.apiKey | default ((.Values.litemaas | default dict).apiKey) | default "" -}}
{{- end -}}

{{- define "bootstrap.aiApiUrl" -}}
{{- .Values.ai.apiUrl | default ((.Values.litemaas | default dict).apiUrl) | default "" -}}
{{- end -}}

{{- define "bootstrap.aiModel" -}}
{{- .Values.ai.model | default ((.Values.litemaas | default dict).model) | default "gpt-oss-120b-a5b" -}}
{{- end -}}
