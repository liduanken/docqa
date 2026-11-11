{{- define "docqa.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "docqa.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "docqa.selectorLabels" -}}
app.kubernetes.io/name: {{ include "docqa.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "docqa.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{ include "docqa.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
platform.example.com/team: {{ .Values.team | quote }}
{{- end -}}

{{- define "docqa.secretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-secrets" (include "docqa.fullname" .)) -}}
{{- end -}}

{{/* PromQL label selector for this release's scrape job. */}}
{{- define "docqa.promSelector" -}}
job="{{ include "docqa.fullname" . }}",namespace="{{ .Release.Namespace }}"
{{- end -}}
