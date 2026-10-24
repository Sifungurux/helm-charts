{{- define "colibri.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "colibri.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains (include "colibri.name" .) .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "colibri.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "colibri.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "colibri.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "colibri.selectorLabels" -}}
app.kubernetes.io/name: {{ include "colibri.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "colibri.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
{{- end }}

{{- define "colibri.claimName" -}}
{{- default (printf "%s-models" (include "colibri.fullname" .)) .Values.persistence.existingClaim }}
{{- end }}

{{/* Changes whenever the download Job's spec would change (Jobs are immutable). */}}
{{- define "colibri.downloadHash" -}}
{{- $in := dict "model" .Values.model "dl" .Values.modelDownload "sc" .Values.securityContext "psc" .Values.podSecurityContext "ns" .Values.nodeSelector "tol" .Values.tolerations "claim" (include "colibri.claimName" .) }}
{{- toJson $in | sha256sum | trunc 8 }}
{{- end }}

{{- define "colibri.modelPath" -}}
{{- printf "/model/%s" .Values.model.id }}
{{- end }}
