{{/*
Common labels.
*/}}
{{- define "vault.labels" -}}
app.kubernetes.io/name: vault
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: tssc-validated-pattern
{{- end }}
