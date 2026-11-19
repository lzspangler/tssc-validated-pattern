{{/*
Common labels.
*/}}
{{- define "external-secrets.labels" -}}
app.kubernetes.io/name: external-secrets
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: tssc-validated-pattern
{{- end }}
