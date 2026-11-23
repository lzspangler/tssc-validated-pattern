{{/*
Common labels.
*/}}
{{- define "ops.labels" -}}
app.kubernetes.io/name: ops
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: tssc-validated-pattern
{{- end }}
