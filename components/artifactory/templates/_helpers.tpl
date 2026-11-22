{{/*
Common labels.
*/}}
{{- define "artifactory.labels" -}}
app.kubernetes.io/name: artifactory
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: tssc-validated-pattern
{{- end }}
