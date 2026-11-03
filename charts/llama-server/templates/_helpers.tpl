{{- define "llama-server.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "llama-server.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains (include "llama-server.name" .) .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "llama-server.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "llama-server.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "llama-server.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "llama-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "llama-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "llama-server.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (required "set image.digest (or image.tag): the image workflow tags builds <upstream-sha>-<repo-sha>" .Values.image.tag) }}
{{- end }}
{{- end }}

{{- define "llama-server.claimName" -}}
{{- default (printf "%s-models" (include "llama-server.fullname" .)) .Values.persistence.existingClaim }}
{{- end }}

{{- define "llama-server.modelDir" -}}
{{- printf "/model/%s" .Values.model.id }}
{{- end }}

{{- define "llama-server.modelPath" -}}
{{- printf "%s/%s" (include "llama-server.modelDir" .) .Values.model.file }}
{{- end }}

{{/* Written last by the download Job; the server's init container waits for it.
Its content names the exact model, so a release sharing the claim waits for the
same file and revision. */}}
{{- define "llama-server.marker" -}}
{{- printf "%s/.%s.ready" (include "llama-server.modelDir" .) .Values.model.file }}
{{- end }}

{{- define "llama-server.markerContent" -}}
{{- printf "%s@%s/%s" .Values.model.repo .Values.model.revision .Values.model.file }}
{{- end }}

{{/* Changes whenever the download Job's spec would change (Jobs are immutable). */}}
{{- define "llama-server.downloadHash" -}}
{{- $in := dict "model" .Values.model "dl" .Values.modelDownload "sc" .Values.securityContext "psc" .Values.podSecurityContext "ns" .Values.nodeSelector "tol" .Values.tolerations "aff" .Values.affinity "claim" (include "llama-server.claimName" .) }}
{{- toJson $in | sha256sum | trunc 8 }}
{{- end }}
